#!/usr/bin/env python3
"""Worker de inyección de claves SSH en contenedores LXC (corre en el host PVE).

El backend cloud-api no puede escribir archivos dentro de un LXC via la API
REST (no existe /files para lxc). Este worker acepta {ct, key} autenticado y
ejecuta `pct push` localmente como root.

Tambien acepta {ct} en /tmpfs: inyecta un service systemd (tmpfs-limits.service)
que remonta /dev/shm y /run/user/0 a tamanios proporcionales a la RAM asignada
al container (lee /proc/meminfo, que en un LXC reporta la RAM del container).
/run y /run/lock no se pueden remontar desde el LXC unprivileged (PVE los
monta desde el host) y quedan host-sized: es solo techo visual, el cgroup
memory.max ya limita la RAM real.
"""
import http.server, json, os, subprocess, sys

TOKEN = os.environ.get("INJECT_TOKEN")
HOST = os.environ.get("INJECT_BIND", "0.0.0.0")   # ej. la IP del bridge interno del host
PORT = int(os.environ.get("INJECT_PORT", "8090"))
# unicas fuentes permitidas: el CT del cloudapi y el propio host
ALLOWED = set((os.environ.get("INJECT_ALLOW", "") or "").split(",")) - {""}

# --- tmpfs-limits: script + service que se inyectan a cada LXC nuevo ---
TMPFS_SCRIPT = """#!/bin/bash
# Acota los tmpfs estaticos a la RAM asignada al container (nubecita).
# /proc/meminfo en un LXC PVE reporta la RAM del container (mem=), no la del host.
# El remount simple devuelve EBUSY en LXC no-privilegiados, por eso se hace
# umount + mount nuevo (verificado en 6136/6141).
total_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
[ -z "$total_kb" ] && exit 0
total_mb=$(( total_kb / 1024 ))
[ "$total_mb" -lt 128 ] && total_mb=128
shm=$(( total_mb / 2 ))
usr=$(( total_mb / 10 ))
[ "$usr" -lt 8 ] && usr=8
umount /dev/shm 2>/dev/null
mount -t tmpfs -o size=${shm}M,mode=1777 tmpfs /dev/shm 2>/dev/null
if [ -d /run/user/0 ]; then
  umount /run/user/0 2>/dev/null
  mount -t tmpfs -o size=${usr}M,mode=700 tmpfs /run/user/0 2>/dev/null
fi
exit 0
"""

TMPFS_SERVICE = """[Unit]
Description=Acotar tmpfs a la RAM asignada al container (nubecita)
After=local-fs.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tmpfs-limits.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
"""


def gpu_health():
    """Estado electrico de cada GPU NVIDIA del host: addr -> {alive, power}.

    Senal univoca de GPU MUERTA (colgada a nivel PCIe, p.ej. tras un crash
    del driver bajo vfio passthrough): el config space lee todo ff. Un device
    sano responde su vendor/device id reales aunque este en D3hot/D3cold o
    atado a vfio-pci. No requiere driver ni nvidia-smi: solo sysfs.
    """
    base = "/sys/bus/pci/devices"
    out = {}
    try:
        devs = sorted(os.listdir(base))
    except OSError:
        return out
    for dev in devs:
        try:
            cls = open(f"{base}/{dev}/class").read().strip()
            if not cls.startswith("0x03"):      # solo display controllers
                continue
            ven = open(f"{base}/{dev}/vendor").read().strip()
            if ven != "0x10de":                 # solo NVIDIA
                continue
            head = open(f"{base}/{dev}/config", "rb").read(4)
            alive = head != b"\xff\xff\xff\xff" and len(head) == 4
            try:
                power = open(f"{base}/{dev}/power_state").read().strip()
            except OSError:
                power = "?"
            out[dev] = {"alive": alive, "power": power}
        except OSError:
            out[dev] = {"alive": False, "power": "unreadable"}
    return out


def inject_tmpfs(vmid):
    """Empuja script+service al LXC y los habilita. Devuelve (ok, msg)."""
    base = f"/tmp/.cloud_tmpfs_{vmid}_{os.getpid()}"
    with open(base + ".sh", "w") as f:
        f.write(TMPFS_SCRIPT)
    os.chmod(base + ".sh", 0o755)
    with open(base + ".service", "w") as f:
        f.write(TMPFS_SERVICE)
    try:
        r = subprocess.run(["pct", "push", vmid, base + ".sh",
                            "/usr/local/sbin/tmpfs-limits.sh"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[:200] or "pct push script fallo"
        r = subprocess.run(["pct", "push", vmid, base + ".service",
                            "/etc/systemd/system/tmpfs-limits.service"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[:200] or "pct push service fallo"
        r = subprocess.run(["pct", "exec", vmid, "--", "/bin/bash", "-c",
                            "chmod +x /usr/local/sbin/tmpfs-limits.sh && "
                            "systemctl daemon-reload && "
                            "systemctl enable tmpfs-limits.service >/dev/null && "
                            "systemctl restart tmpfs-limits.service"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[:200] or "enable/start fallo"
        return True, "ok"
    finally:
        for p in (base + ".sh", base + ".service"):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    if not TOKEN:
        sys.exit("falta env INJECT_TOKEN")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._json(401, {"error": "bad token"})
            if self.path == "/gpu_health":
                # Interno: solo el cloudapi (CT 6003 en vmbr-cloud) o el propio
                # host pueden consultar el estado de GPUs. El worker escucha en
                # si no se configura INJECT_ALLOW, se rechaza todo (fail-closed)
                if self.client_address[0] not in ALLOWED:
                    return self._json(403, {"error": "gpu_health interno"})
                return self._json(200, gpu_health())
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except Exception:
                return self._json(400, {"error": "bad json"})

            if self.path == "/tmpfs":
                vmid = str(body.get("ct", "")).strip()
                if not vmid.isdigit():
                    return self._json(400, {"error": "ct requerido"})
                ok, msg = inject_tmpfs(vmid)
                return self._json(200 if ok else 500, {"ok": ok, "ct": vmid, "msg": msg})

            if self.path != "/inject":
                return self._json(404, {"error": "no route"})
            vmid = str(body.get("ct", "")).strip()
            key = (body.get("key") or "").strip()
            if not vmid.isdigit() or not key:
                return self._json(400, {"error": "ct/key requeridos"})
            for line in key.splitlines():
                l = line.strip()
                if l and not (l.startswith("ssh-") or l.startswith("ecdsa-") or l.startswith("sk-")):
                    return self._json(400, {"error": "linea de clave invalida"})
            tmp = f"/tmp/.cloud_ssh_{vmid}_{os.getpid()}"
            try:
                with open(tmp, "w") as f:
                    f.write(key + "\n")
                os.chmod(tmp, 0o600)
                r = subprocess.run(
                    ["pct", "push", vmid, tmp, "/root/.ssh/authorized_keys"],
                    capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    subprocess.run(["pct", "exec", vmid, "mkdir -p /root/.ssh"],
                                   capture_output=True, text=True, timeout=30)
                    r = subprocess.run(
                        ["pct", "push", vmid, tmp, "/root/.ssh/authorized_keys"],
                        capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    return self._json(500, {"error": r.stderr.strip()[:300] or "pct push fallo"})
                return self._json(200, {"ok": True, "ct": vmid})
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    srv = http.server.ThreadingHTTPServer((HOST, PORT), H)
    print(f"inject-worker listo en {HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
