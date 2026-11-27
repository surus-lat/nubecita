# Runbook: sumar un nodo Harvester a nubecita

El diseño es agnóstico del tamaño del nodo (plan `nubecita-escala-harvester.md` §9):
sumar un nodo es DATOS + probe + e2e, no código. `HarvesterProvider` ya está
en `app.py` con `IMPLEMENTED=False`; el flag se vira `True` solo tras el e2e
contra el nodo real (S2).

## 0. Antes de instalar (decidir con Moni)

- HW: cores, RAM, discos (Longhorn necesita disco APARTE del SO), GPUs, NICs.
- Red: R1 = trunk con VLAN X para 10.20.0.0/16 (recomendado) o R2 = puerto
  access + NIC dedicada. Confirmar topología del switch ANTES de instalar.
- IP de management fija en <mgmt-red>.

## 1. Instalar Harvester v1.9

- Bootear el ISO y arrancar el instalador con la config centralizada:
  `harvester-installer config-url=http://<mgmt-ip>:8085/harvester-config.yaml`
  (fuente en host: /root/harvester/install-config.yaml; ISO espejada en
  http://<mgmt-ip>:8085/harvester.iso, sha512 bf2e4a8d...38d193ba verificado
  contra el espejo LAN <mirror-ip>:8090). La config ya incluye
  la pubkey ed25519 DEDICADA de hermes para nodos Harvester
  (id_ed25519_harvester, revocable — NUNCA la clave del gateway SSH) y mode:
  create/role: all-in-one. Ajustar IP estatica de mgmt y hostname per nodo ANTES
  (editar el yaml en el host; el server lo re-sirve al vuelo). SSH de Harvester
  es solo soporte; la integracion va por API.
  Notas del validador v1.9 (encadenado, learnt 23/10): el yaml raiz exige
  `scheme_version: 1` y `token: <string>` (join token del cluster; el mismo
  para futuros nodos del cluster). Orden de validacion: token -> isoUrl ->
  mode -> red.
- Verificar: `ssh -i ~/.ssh/id_ed25519_harmes hv-node harvester@<mgmt-ip>`
  (host-side) y `kubectl get nodes` via Rancher UI.

## 2. Red comun (la LAN virtual del cliente)

- En PVE (host): `vmbr-cloud` VLAN-aware con la VLAN X (una `address` por
  línea en /etc/network/interfaces.d/vmbr-cloud; mover los .bak FUERA del
  directorio; `ifreload -a`; verificar `ip -br addr show vmbr-cloud` ANTES y
  despues — el gateway 10.20.N.1 y el NAT MASQUERADE 10.20.0.0/16->vmbr0 no
  se tocan).
- En Harvester (UI): VM Networks → crear red con VLAN ID X sobre el uplink
  correcto (genera la NetworkAttachmentDefinition).
- Probar L2: crear una VMI de prueba en esa red (UI), asignarle 10.20.2.250/24
  gw 10.20.2.1, y desde CT 6003: `bash -c '</dev/tcp/10.20.2.250/22'` y ping.

## 3. Service account + token

- UI Harvester → Accounts → crear service account rol mínimo (admin de VMs en
  namespaces tenant-*). Copiar el token ( bearerToken ).
- Guardarlo SOLO como env del unit cloudapi:
  `Environment=HARVESTER_TOKEN_<nodeid>=...` en
  /etc/systemd/system/cloudapi.service → `systemctl daemon-reload &&
  systemctl restart cloudapi`. Nunca en el repo ni en la DB.
- Probar: `curl -k -H "Authorization: Bearer <tok>"
  https://<mgmt-ip>/k8s/clusters/local/apis/kubevirt.io/v1` → 200.

## 4. Registro en nubecita (admin, por API)

```
PUT /admin/nodes/hv-1  { "capacity_cpu": N, "capacity_ram_mb": M,
                         "capacity_disk_gb": D, "gpus": G }
POST /admin/nodes/hv-1/probe
```
(la fila `hv-1` nace al registrar el endpoint/token_ref; el probe refresca
last_seen y capacidad reportada por el provider). Confirmar con
`GET /admin/nodes` que `assigned` arranca en 0 y `status: online`.

## 5. Criterios de aceptacion e2e (test)

1. `POST /vm` (cliente test) con el scheduler forzando hv-1 → VMI nace en el
   namespace `tenant-test`, running, badge intacto en la SPA.
2. La VMI queda en la LAN del cliente: `10.20.<subnet>.(vmid-6100+2)` visible,
   `gwssh host_id -c true` responde desde CT 6003.
3. Dos VMs del mismo cliente en nodos DISTINTOS (una en pve-1, otra en hv-1)
   se hacen ping por la 10.20.x (requisito duro del plan §3).
4. rsync/scp por el tunnel publico a la VM de Harvester.
5. `DELETE /vm` purga VMI + PVC + secret; la GPU vuelve a disponible al
   instante (si era GPU).
6. drain de hv-1 → creates nuevos van a pve-1; lo existente sigue corriendo.

## 6. GPU (S5)

- Activar el GPU addon / gpu-operator del nodo; verificar recurso publicado en
  `allocatable` (`nvidia.com/geforce-rtx-2080-ti` o fallback `nvidia.com/gpu`).
- Imagen template con nvidia-dkms (equivalente al template 9100 de PVE).
- `gpu:RTX 2080 Ti` = 6.5 cr/h en `PUT /admin/rates` (override por modelo).
- Validar `nvidia-smi` DENTRO del guest (en el host vfio siempre falla, es
  esperado) y el smoke de embeddings con `-b 8192 -ub 8192` en 11 GB de VRAM.
