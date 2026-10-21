<p align="center">
  <img src="assets/logo-nubecita.svg" alt="Logo nubecita" width="150" height="150">
</p>

<h1 align="center">nubecita</h1>

<p align="center">
  <img src="assets/logo-unrn.png" alt="Universidad Nacional de Río Negro (UNRN)" height="34">
  &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <strong>×</strong>
  &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
  <img src="assets/logo-surus.png" alt="Logo SURUS" height="34">
</p>

<p align="center">
  <strong>Nube self-service para comunidades académicas</strong><br>
  Un proyecto colaborativo de la <a href="https://www.unrn.edu.ar/">Universidad Nacional de Río Negro (UNRN)</a>
  y <a href="https://surus.lat/">SURUS</a>.
</p>

**nubecita** ([nubecita.surus.lat](https://nubecita.surus.lat)) es una plataforma web para que
cualquier persona registrada cree y administre su propia máquina virtual en una nube propia,
sin depender de un proveedor comercial: levantar, arrancar, detener y borrar VMs (contenedores
LXC o máquinas QEMU con GPU dedicada), conectarse por SSH, transferir archivos, pagar el uso con
un sistema de créditos y reportar problemas — todo desde el navegador o desde una API REST
documentada.

Nace de una necesidad concreta de grupos de investigación: acceder a GPUs para tareas de IA
(inferencia con llama.cpp, embeddings, entrenamiento liviano) sin depender de la nube de pago,
y dar a estudiantes e investigadores un entorno reproducible y económico.

---

## Qué hace

- **Self-service total**: creás tu VM desde un wizard en la web; queda lista en segundos y
  aparece en tu lista con su estado (`provisioning` → `running`).
- **VMs con GPU**: passthrough PCI real de GPUs NVIDIA sobre VMs QEMU con driver preinstalado
  (template con dkms). El backend mantiene un inventario de GPUs, excluye las que están caídas
  y reserva el modelo pedido al crear.
- **Contenedores LXC** para cargas sin GPU, muy baratos en créditos.
- **SSH seguro sin exponer puertos**: un único gateway SSH detrás de un túnel de Cloudflare;
  el dispatch por `host_id` salta con clave hasta tu VM. Sobre el mismo túnel andan
  `rsync`, `scp -O` y `git`.
- **Billing por créditos**: cada recurso (vCPU, RAM, disco, GPU) tiene una tasa por hora y solo
  se factura mientras tu VM está `running`. Saldo, costo por hora y horas restantes visibles en
  la UI y en `GET /me/credits`.
- **Servicios Nomad**: además de VMs, podés desplegar jobs docker (APIs, sitios web).
- **Tokens de API por usuario**: varios tokens con etiqueta, revocables uno por uno.
- **Ticketera de soporte** integrada (cliente ↔ admin) y documentación de la API dentro de la
  propia app, más Swagger en `/docs`.
- **Panel de administración**: usuarios y cuotas, estado del cluster en vivo, detección de
  *drift* entre el ledger y la realidad del hypervisor, y borrado con **garantía de destrucción
  real** (nunca se marca `deleted` si el recurso sigue vivo).

## Arquitectura

```
navegador ──HTTPS── Cloudflare Tunnel ──┐
                                        ├──► cloudapi (FastAPI + SQLite)
cliente ──SSH──── Cloudflare Tunnel ──► │       ├──► API REST de Proxmox VE (LXC/QEMU, PCI mappings)
        gateway sshd ── clave ──► tu VM ┘       ├──► Nomad (jobs docker)
                                                └──► Cloudflare API (DNS + reglas de ingress)

worker cloud-inject (en el host) ◄── HTTP local ──┘
   inyección de claves SSH y límites de tmpfs
```

- **Backend**: un solo módulo FastAPI (`app.py`) con SQLite como estado (usuarios, créditos,
  ledger de recursos, tickets). Las migraciones de esquema son idempotentes y corren al arrancar.
- **Frontend**: SPA vanilla-JS sin framework y sin build step (`web/`), con componentes
  Web Components ligeros y una isla React opcional (el wizard de creación) en `web-react/`.
- **Aprovisionamiento asíncrono**: la API responde en ~2 s y una tarea de fondo crea la VM,
  inyecta claves, configura el túnel y cierra el estado. Cualquier fallo hace rollback completo
  (stop + delete + liberación de la GPU).
- **Seguridad**: tokens almacenados como hash, secretos solo por variables de entorno del unit
  systemd, aislamiento por usuario en todos los endpoints y validación de cuota/saldo antes de
  provisionar.

## Qué hay en este repo

```
app.py                    backend FastAPI (API, billing, aprovisionamiento, Cloudflare)
web/index.html            SPA (vistas cliente y admin)
web/app-admin.js          bundle admin (carga diferida)
web-react/                isla React del wizard (build con Vite)
worker/inject_worker.py   worker HTTP en el host (claves SSH, límites de tmpfs)
gateway/                  scripts del gateway SSH (dispatch + salto a la VM)
ci/check.py               gate de CI (py_compile + node --check + bash -n)
test_tickets.py           tests e2e de la ticketera (DB aislada, no toca producción)
assets/                   logos institucionales (UNRN, SURUS)
```

No se versionan (ver `.gitignore`): la base de datos de runtime, `venv/`, `__pycache__/`,
secretos y backups del servidor.

## Configuración

El backend se configura por completo con variables de entorno (todas tienen un default razonable
salvo los tokens):

| Variable | Qué hace |
|---|---|
| `CLOUDAPI_DB` | ruta de la base SQLite |
| `PVE_HOST`, `PVE_NODE`, `PVE_USER`, `PVE_TOKEN_NAME`, `PVE_TOKEN_VAL` | API de Proxmox VE (host, nodo, usuario y token) |
| `ADMIN_TOKEN` | token del panel de administración |
| `INJECT_HOST`, `INJECT_TOKEN` | worker del host (inyección SSH / tmpfs) |
| `INJECT_BIND`, `INJECT_PORT`, `INJECT_ALLOW` | worker: bind, puerto y lista de IPs permitidas (fail-closed: vacío = rechaza todo) |
| `NOMAD_HOST`, `NOMAD_TOKEN` | cluster Nomad (opcional) |
| `CLOUD_DOMAIN` / `SSH_DOMAIN` | dominios públicos de la app y del gateway SSH |
| `GATEWAY_IP` | IP interna del gateway SSH (usada en las reglas del túnel) |
| `VM_NET_PREFIX` | prefijo de red de las VMs de clientes (default `10.20.1`) |
| `CF_TUNNEL_ID`, `CF_ACCOUNT_ID`, `CF_API_TOKEN_FILE` | Cloudflare Tunnel (ingress SSH por VM) |
| `GPU_TEMPLATE_VID`, `GPU_DISK_STORAGE` | template QEMU con driver NVIDIA y storage de discos GPU |
| `SEED_ADMIN_USER`, `SEED_ADMIN_PASSWORD` | crea el primer admin al arrancar una DB desde cero |
| `CREDIT_BILL_INTERVAL` | cada cuántos segundos se factura (default 60) |
| `SERVICE_KEY_PUB_FILE` | clave pública del servicio para la inyección SSH |

## Uso rápido (API)

```bash
# login -> token
TOK=$(curl -s https://nubecita.surus.lat/api/login \
      -H 'Content-Type: application/json' \
      -d '{"username":"USUARIO","password":"***"}' | jq -r .token)

# crear una VM (responde al instante con status=provisioning)
curl -s https://nubecita.surus.lat/vm -H "Authorization: Bearer $TOK" \
     -H 'Content-Type: application/json' \
     -d '{"name":"mi-vm","cpu":2,"ram_mb":2048,"disk_gb":8}'

# créditos y documentación interactiva
curl -s https://nubecita.surus.lat/me/credits -H "Authorization: Bearer $TOK"
# Swagger / OpenAPI:  https://nubecita.surus.lat/docs  ·  /openapi.json
```

> Necesitás enviar un `User-Agent` de navegador (por ejemplo `curl -A 'Mozilla/5.0'`): el borde
> de Cloudflare rechaza los clientes sin él.

SSH hacia tu VM (requiere [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)):

```sshconfig
# ~/.ssh/config
Host ssh.surus.lat
    HostName ssh.surus.lat
    User <host_id que muestra la UI>
    IdentityFile ~/.ssh/id_ed25519
    ProxyCommand cloudflared access ssh --hostname %h
```

Después: `ssh <host_id>@ssh.surus.lat` (también funciona `rsync -e ssh` y `git clone`).

## Contribuir

El flujo es **repo primero**: nunca se edita el servidor de producción a mano.

1. Sacá una rama, cambiá, commiteá y `git push`.
2. El hook `pre-receive` corre `ci/check.py` sobre el árbol del push (`py_compile` de todos los
   `.py`, `node --check` del JS inline y de los bundles, `bash -n` de los scripts del gateway).
   Un commit roto **rechaza el push** y la rama no se mueve.
3. El hook `post-receive` despliega el commit: backup del live → copia verificada por md5 →
   restart → health check → **rollback automático** si la app no levanta.

Antes de pushear, corré el gate local:

```bash
python3 ci/check.py
python3 test_tickets.py      # tests de la ticketera, con DB aislada
```

Los cambios de la documentación de la API dentro de la app viven en `renderApi()` de
`web/index.html`.

## Licencia

MIT (ver `LICENSE`). © 2026 Universidad Nacional de Río Negro & SURUS.
Los logos de `assets/` identifican la colaboración del proyecto: usalos solo con ese fin.

---

<details>
<summary>English summary</summary>

nubecita is an open-source self-service cloud for academic communities: create and manage LXC/QEMU
virtual machines — including NVIDIA GPU passthrough — on a Proxmox VE cluster, connect through an
SSH gateway behind a Cloudflare Tunnel, and pay for usage with an hourly credit system. One
FastAPI + SQLite backend, a dependency-free vanilla-JS SPA, async provisioning with full rollback,
and a CI/CD pipeline that gates every push and auto-rolls-back on a failed health check.
Contributions welcome: run `python3 ci/check.py` before pushing. MIT licensed.

</details>
