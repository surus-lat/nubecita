#!/usr/bin/env python3
"""
Cloud API — self-service backend con quota por cliente.

Servicios:
  - VMs (EC2-like) vía Proxmox REST API (token PVEAPIToken)
  - Servidores de contenedores (ECS-like) vía Nomad (Docker driver)

Autenticación:
  - Token de cliente (Bearer) generado por el admin (endpoint /admin/clients).
  - Cliente gestiona sus recursos dentro de su quota.

Quotas por cliente:
  - cpu_quota (cores), ram_quota_mb, disk_quota_gb, max_vms, max_containers
"""

import os
import json
import re
import uuid
import sqlite3
import secrets
import asyncio
import time
import string
import subprocess
import urllib.request
import urllib.error
from contextlib import contextmanager
from typing import Optional

import threading
import httpx
from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

# ─── Config ────────────────────────────────────────────────────────────
DB = os.environ.get("CLOUDAPI_DB", "/opt/cloudapi/cloud.db")
PVE_HOST = os.environ.get("PVE_HOST", "https://pve.example:8006")
PVE_NODE = os.environ.get("PVE_NODE", "proxmox")
PVE_USER = os.environ.get("PVE_USER", "root@pam")
PVE_TOKEN = os.environ.get("PVE_TOKEN_NAME", "cloud-api")
PVE_TOKEN_VAL = os.environ.get("PVE_TOKEN_VAL", "")
NOMAD_HOST = os.environ.get("NOMAD_HOST", "http://nomad.example:4646")
# worker de inyección SSH en el host (pct push, solo vía token)
INJECT_HOST = os.environ.get("INJECT_HOST", "http://inject.example:8090")
INJECT_TOKEN = os.environ.get("INJECT_TOKEN", "")

async def inject_ssh(vmid: int, ssh_key: str):
    """Escribe /root/.ssh/authorized_keys en el LXC vía el worker del host."""
    keys = [l for l in (ssh_key or "").splitlines() if l.strip()]
    sp = service_key_pub()
    if sp and sp not in keys:
        keys.append(sp)
    if not INJECT_TOKEN or not keys:
        return
    r = None
    last_err = None
    for attempt in range(4):  # el worker del host puede estar reiniciando (reboot PVE)
        try:
            async with httpx.AsyncClient(verify=False, timeout=90) as c:
                r = await c.post(f"{INJECT_HOST}/inject",
                                 headers={"Authorization": f"Bearer {INJECT_TOKEN}"},
                                 json={"ct": str(vmid), "key": "\n".join(keys)})
            break
        except httpx.HTTPError as e:
            last_err = e
            await asyncio.sleep(5 * (attempt + 1))
    if r is None:
        raise HTTPException(502, f"inject SSH: worker del host inalcanzable: {last_err}")
    if r.status_code >= 400:
        raise HTTPException(502, f"inject SSH falló: {r.text[:200]}")
    return r.json()


async def inject_tmpfs_service(vmid: int):
    """Inyecta tmpfs-limits.service al LXC vía el worker del host.

    systemd monta /dev/shm (50%), /run (20%), /run/user/0 (10%) leyendo
    /proc/meminfo ANTES de que lxcfs sirva el valor del container → los
    tmpfs quedan sized contra la RAM del HOST (63G de /dev/shm en una VM
    de 2GB). Es techo visual (el cgroup memory.max corta la RAM al límite
    asignado), pero se inyecta un service que remonta /dev/shm y /run/user/0
    a proporción de la RAM real del container en cada boot. /run y /run/lock
    no se pueden remontar desde el LXC unprivileged (PVE los monta desde el
    host, write-protected). Requiere el container corriendo (pct push)."""
    if not INJECT_TOKEN:
        return
    async with httpx.AsyncClient(verify=False, timeout=90) as c:
        r = await c.post(f"{INJECT_HOST}/tmpfs",
                         headers={"Authorization": f"Bearer {INJECT_TOKEN}"},
                         json={"ct": str(vmid)})
    if r.status_code >= 400:
        raise HTTPException(502, f"inject tmpfs falló: {r.text[:200]}")
    return r.json()

NOMAD_TOKEN = os.environ.get("NOMAD_TOKEN", "")  # opcional
VM_ID_BASE = 6100
SERVICE_KEY_PUB_FILE = os.environ.get("SERVICE_KEY_PUB_FILE", "/opt/sshgw/service.pub")
# GPU passthrough (solo QEMU; LXC no soporta hostpci)
GPU_VENDOR = "0x10de"          # NVIDIA
GPU_DISK_STORAGE = os.environ.get("GPU_DISK_STORAGE", "local-lvm")
GPU_TEMPLATE_VID = int(os.environ.get("GPU_TEMPLATE_VID", "9100"))  # template QEMU base para linked clones (9100 ya trae driver NVIDIA + dkms)
GPU_MAP_PREFIX = "cloud-gpu"   # IDs de mapping PCI del servicio (se distinguen de los manuales)

def service_key_pub() -> str:
    try:
        with open(SERVICE_KEY_PUB_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""   # rango para VMs de clientes
TEMPLATE = "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
BRIDGE = "vmbr-cloud"
CLOUD_DOMAIN = os.environ.get("CLOUD_DOMAIN", "nubecita.surus.lat")
SSH_DOMAIN = os.environ.get("SSH_DOMAIN", "ssh.surus.lat")
GATEWAY_IP = os.environ.get("GATEWAY_IP", "127.0.0.1")   # IP del CT cloudapi (gateway SSH)
VM_NET_PREFIX = os.environ.get("VM_NET_PREFIX", "10.20.1")  # VM N -> {prefix}.{N-VM_ID_BASE+2}
CLOUD_NET = os.environ.get("CLOUD_NET", f"{VM_NET_PREFIX}.0/24")  # subred para VMs
CLOUD_GW = os.environ.get("CLOUD_GW", f"{VM_NET_PREFIX}.1")

app = FastAPI(title="Cloud API", version="1.0")

# ─── Sistema de créditos (billing por recursos asignados, solo running) ───
# Costo por hora (créditos/h) por recurso asignado a la VM:
#   vCPU 0.1 · GB RAM 0.01 · GB disco 0.001 · GPU (RTX A5000) 9.5
# Una VM al tope del pool (8 vCPU / 32 GB / 40 GB + GPU A5000) = 10.66 cr/h
# → 213.2 créditos duran exactamente 20 horas.
BILL_INTERVAL = int(os.environ.get("CREDIT_BILL_INTERVAL", "60"))   # segundos
BILL_MAX_CATCHUP = 3600 * 2   # no facturar más de 2h de atraso por tick

DEFAULT_RATES = {"cpu": 0.1, "ram_gb": 0.01, "disk_gb": 0.001, "gpu": 9.5}
CREDIT_RATES: dict[str, float] = dict(DEFAULT_RATES)

def _credit_rates_init():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS credit_rates (
        rate_key TEXT PRIMARY KEY, value REAL NOT NULL)""")
    keys = [k for (k,) in conn.execute("SELECT rate_key FROM credit_rates").fetchall()]
    for k, v in DEFAULT_RATES.items():
        if k not in keys:
            conn.execute("INSERT INTO credit_rates (rate_key, value) VALUES (?,?)", (k, v))
    conn.execute("""CREATE TABLE IF NOT EXISTS credit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        delta REAL NOT NULL,
        reason TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS credit_billing (
        ref_key TEXT PRIMARY KEY,        -- 'vm:<vmid>'
        last_billed_at REAL NOT NULL)""")
    # saldo: columna credits en users (migración idempotente)
    users_exists = conn.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone()
    if users_exists:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "credits" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN credits REAL NOT NULL DEFAULT 0")
    else:
        # DB nueva (sin legacy clients/web_users): _migrate_users detecta la tabla
        # y la llena de los seeds; el CREATE aqui solo evita el crash de orden.
        conn.execute("""CREATE TABLE users (
          username TEXT PRIMARY KEY,
          role TEXT NOT NULL CHECK(role IN ('admin','client')),
          password_hash TEXT,
          token_hash TEXT,
          cpu_quota INTEGER NOT NULL DEFAULT 4,
          ram_quota_mb INTEGER NOT NULL DEFAULT 8192,
          disk_quota_gb INTEGER NOT NULL DEFAULT 40,
          max_vms INTEGER NOT NULL DEFAULT 2,
          max_containers INTEGER NOT NULL DEFAULT 3,
          ssh_key TEXT DEFAULT '',
          ssh_private_key TEXT DEFAULT '',
          subnet INTEGER,
          credits REAL NOT NULL DEFAULT 0,
          created_at TEXT DEFAULT (datetime('now')))""")
    conn.commit()
    CREDIT_RATES.update({r[0]: r[1] for r in conn.execute("SELECT rate_key, value FROM credit_rates").fetchall()})
    conn.close()

_credit_rates_init()

def vm_hourly_cost(cpu: int, ram_mb: int, disk_gb: int, gpu: int = 0) -> float:
    """Créditos/hora que cuesta una VM según lo asignado (tasas en CREDIT_RATES)."""
    return ((cpu or 0) * CREDIT_RATES["cpu"]
            + (ram_mb or 0) / 1024 * CREDIT_RATES["ram_gb"]
            + (disk_gb or 0) * CREDIT_RATES["disk_gb"]
            + (gpu or 0) * CREDIT_RATES["gpu"])

def _gpu_units(r) -> int:
    """GPU facturable de una fila de resources: 1 si tiene gpu_mapping activo.
    La tabla resources NO tiene columna 'gpu' (solo gpu_mapping/gpu_model): leer
    r["gpu"] daba siempre 0 y las VMs GPU se facturaban como si no tuvieran GPU
    (preset GPU: 0,6 cr/h en vez de 10,1 cr/h — bug reportado 06/10)."""
    try:
        return 1 if (r["gpu_mapping"] if "gpu_mapping" in r.keys() else None) else 0
    except Exception:
        return 0

def _user_credits(username: str) -> float:
    with db() as c:
        r = c.execute("SELECT COALESCE(credits,0) FROM users WHERE username=?", (username,)).fetchone()
    return float(r[0]) if r else 0.0

def _sanitename(name: str, fallback: str) -> str:
    """Nombre seguro para PVE (DNS name): minúsculas, [a-z0-9-], sin espacios.
    PVE rechaza 400 'invalid format - value does not look like a valid DNS name'
    si el name trae espacios/mayúsculas → rollback y la VM queda 'deleted' al instante."""
    n = re.sub(r"[^a-z0-9-]", "-", (name or "").strip().lower())
    n = re.sub(r"-{2,}", "-", n).strip("-")[:20]  # PVE: max 20 chars
    if not n:
        n = re.sub(r"-{2,}", "-", fallback).strip("-")[:20]
    return n

def _set_user_credits(username: str, amount: float) -> None:
    with db() as c:
        c.execute("UPDATE users SET credits=? WHERE username=?", (amount, username))

async def credit_billing_loop():
    """Factura solo VMs running cada BILL_INTERVAL; si el saldo llega a 0, detiene la VM.
    El estado (ref_key, last_billed_at) vive en credit_billing, fuera de la tabla resources
    (no se pisa con el trabajo de integración de GPUs)."""
    import logging
    log = logging.getLogger("credits")
    while True:
        await asyncio.sleep(BILL_INTERVAL)
        try:
            now = time.time()
            with db() as c:
                rows = c.execute("SELECT * FROM resources WHERE kind='vm' AND status='running'").fetchall()
            for r in rows:
                ref_key = f"vm:{r['ref_id']}"
                with db() as c:
                    last = c.execute("SELECT last_billed_at FROM credit_billing WHERE ref_key=?", (ref_key,)).fetchone()
                last_ts = last[0] if last else None
                if last_ts is None:
                    # primera factura: desde la creación (con piso de ahora - catchup)
                    try:
                        created = time.mktime(time.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        created = now - 1
                    last_ts = max(created, now - BILL_MAX_CATCHUP)
                dt = min(max(now - last_ts, 0.0), BILL_MAX_CATCHUP)
                cost = vm_hourly_cost(r["cpu"], r["ram_mb"], r["disk_gb"],
                                      _gpu_units(r)) * dt / 3600
                if cost <= 0:
                    continue
                username = r["client_id"]
                bal = _user_credits(username) - cost
                stopped = None
                with db() as c:
                    c.execute("INSERT INTO credit_billing (ref_key, last_billed_at) VALUES (?,?) "
                              "ON CONFLICT(ref_key) DO UPDATE SET last_billed_at=excluded.last_billed_at",
                              (ref_key, now))
                if bal <= 0:
                    bal = 0.0
                    stopped = r["ref_id"]
                    with db() as c:
                        c.execute("UPDATE resources SET status='stopped' WHERE kind='vm' AND ref_id=? AND status='running'",
                                  (r["ref_id"],))
                    asyncio.create_task(_resource_event_broadcast("vm", r["ref_id"], "stopped", r["client_id"]))
                _set_user_credits(username, bal)
                with db() as c:
                    c.execute("INSERT INTO credit_events (username, delta, reason) VALUES (?,?,?)",
                              (username, -cost, f"billing vm:{r['ref_id']} {int(dt)}s"))
                if stopped:
                    log.warning(f"saldo agotado para {username}: deteniendo VM {stopped}")
                    try:
                        await pve_post(f"/nodes/{PVE_NODE}/lxc/{int(r['ref_id'])}/status/stop", {})
                    except HTTPException as e:
                        log.error(f"no se pudo detener VM {stopped}: {e}")
        except Exception as e:
            import traceback
            log.error(f"credit_billing_loop: {e}\n{traceback.format_exc()}")

# ─── DB ────────────────────────────────────────────────────────────────
def _init_db():
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS clients (
      id TEXT PRIMARY KEY,
      token_hash TEXT NOT NULL,
      cpu_quota INTEGER NOT NULL DEFAULT 4,
      ram_quota_mb INTEGER NOT NULL DEFAULT 8192,
      disk_quota_gb INTEGER NOT NULL DEFAULT 40,
      max_vms INTEGER NOT NULL DEFAULT 2,
      max_containers INTEGER NOT NULL DEFAULT 3,
      ssh_key TEXT DEFAULT '',
      created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS resources (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      client_id TEXT NOT NULL REFERENCES clients(id),
      kind TEXT NOT NULL,            -- 'vm' | 'container'
      ref_id TEXT NOT NULL,          -- vmid o job id
      name TEXT,
      cpu INTEGER, ram_mb INTEGER, disk_gb INTEGER,
      status TEXT NOT NULL DEFAULT 'running',
      host_id TEXT,
      created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    # migración: host_id en bases anteriores
    cols = [r[1] for r in conn.execute("PRAGMA table_info(resources)").fetchall()]
    if "host_id" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN host_id TEXT")
    if "gpu_mapping" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN gpu_mapping TEXT")
    if "gpu_model" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN gpu_model TEXT")
    conn.commit()
    conn.close()

_init_db()
# migración: columnas ssh_private_key en clients (bases anteriores no la tienen)
import sqlite3 as _s
_c = _s.connect(DB)
_cols = [r[1] for r in _c.execute("PRAGMA table_info(clients)")]
if "ssh_private_key" not in _cols:
    _c.execute("ALTER TABLE clients ADD COLUMN ssh_private_key TEXT DEFAULT ''")
    _c.commit()
_c.close()

def _init_users():
    """Usuarios web (login username/password) + cliente 'test' seed."""
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS web_users (
      username TEXT PRIMARY KEY,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL,        -- 'admin' | 'client'
      token TEXT                 -- cliente: token bearer; admin: NULL (usa ADMIN_TOKEN)
    );
    """)
    import hashlib
    h = lambda p: hashlib.sha256(p.encode()).hexdigest()
    # seed: admin solo si se define explicitamente (SEED_ADMIN_USER/SEED_ADMIN_PASSWORD);
    # cliente demo "test" siempre (cota chica, para pruebas publicas)
    seed = []
    _su, _sp = os.environ.get("SEED_ADMIN_USER"), os.environ.get("SEED_ADMIN_PASSWORD")
    if _su and _sp:
        seed.append((_su, h(_sp), "admin", None))
    seed.append(("test", h("test"), "client", "test"))
    for u in seed:
        conn.execute("INSERT OR IGNORE INTO web_users (username,password_hash,role,token) VALUES (?,?,?,?)", u)
    # cliente 'test' alineado con su token (solo si no existe)
    conn.execute(
        "INSERT OR IGNORE INTO clients (id, token_hash, cpu_quota, ram_quota_mb, disk_quota_gb, max_vms, max_containers) "
        "VALUES (?,?,?,?,?,?,?)",
        ("test", hashlib.sha256(b"test").hexdigest(), 4, 8192, 40, 2, 3))
    conn.commit()
    conn.close()

_init_users()

# ─── Migración: tabla unificada users (usuario = cliente, 1:1) ───────────
def _migrate_user_tokens():
    """Multi-token: cada login web agrega un token sin revocar los anteriores.
    Idempotente: traza token_hash de users a user_tokens y lo limpia de users."""
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS user_tokens (
        username TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (username, token_hash))""")
    for uname, th in conn.execute("SELECT username, token_hash FROM users WHERE token_hash IS NOT NULL AND token_hash != ''"):
        conn.execute("INSERT OR IGNORE INTO user_tokens (username, token_hash) VALUES (?,?)", (uname, th))
    conn.execute("UPDATE users SET token_hash=NULL WHERE token_hash IS NOT NULL")
    # columnas label/last_used_at (idempotente)
    tcols = [r[1] for r in conn.execute("PRAGMA table_info(user_tokens)").fetchall()]
    if "label" not in tcols:
        conn.execute("ALTER TABLE user_tokens ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    if "last_used_at" not in tcols:
        conn.execute("ALTER TABLE user_tokens ADD COLUMN last_used_at TEXT")
    conn.commit()
    conn.close()

_migrate_user_tokens()

def _migrate_users():
    import hashlib, secrets
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if not [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]:
        conn.execute("""CREATE TABLE users (
          username TEXT PRIMARY KEY,
          role TEXT NOT NULL CHECK(role IN ('admin','client')),
          password_hash TEXT,
          token_hash TEXT,
          cpu_quota INTEGER NOT NULL DEFAULT 4,
          ram_quota_mb INTEGER NOT NULL DEFAULT 8192,
          disk_quota_gb INTEGER NOT NULL DEFAULT 40,
          max_vms INTEGER NOT NULL DEFAULT 2,
          max_containers INTEGER NOT NULL DEFAULT 3,
          ssh_key TEXT DEFAULT '',
          ssh_private_key TEXT DEFAULT '',
          subnet INTEGER,
          created_at TEXT DEFAULT (datetime('now')))""")
    h = lambda pw: hashlib.sha256(pw.encode()).hexdigest()
    generated = []
    # 1) clientes existentes -> users role=client
    for cl in conn.execute("SELECT * FROM clients"):
        ck = set(cl.keys())
        try:
            conn.execute(
                "INSERT INTO users (username, role, password_hash, token_hash, cpu_quota, ram_quota_mb, disk_quota_gb, max_vms, max_containers, ssh_key, ssh_private_key, subnet, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cl["id"], "client", None, cl["token_hash"], cl["cpu_quota"], cl["ram_quota_mb"], cl["disk_quota_gb"],
                 cl["max_vms"], cl["max_containers"], cl["ssh_key"] or "",
                 cl["ssh_private_key"] if "ssh_private_key" in ck else "",
                 cl["subnet"] if "subnet" in ck else None, cl["created_at"]))
        except sqlite3.IntegrityError:
            pass  # ya estaba
    # 2) login web existentes: role=client aporta su password al user; role=admin se inserta
    for u in conn.execute("SELECT * FROM web_users"):
        row = conn.execute("SELECT username FROM users WHERE username=?", (u["username"],)).fetchone()
        if u["role"] == "client":
            if row:
                conn.execute("UPDATE users SET password_hash=? WHERE username=?", (u["password_hash"], u["username"]))
            else:
                # web user sin cliente: crea el usuario client por defecto
                conn.execute(
                    "INSERT INTO users (username, role, password_hash) VALUES (?,?,?)",
                    (u["username"], "client", u["password_hash"]))
        elif row:
            conn.execute("UPDATE users SET password_hash=?, role=? WHERE username=?",
                         (u["password_hash"], "admin", u["username"]))
        else:
            conn.execute("INSERT INTO users (username, role, password_hash) VALUES (?,?,?)",
                         (u["username"], "admin", u["password_hash"]))
    # 3) contraseñas aleatorias para clients sin password (se muestran UNA vez, en el log)
    for row in conn.execute("SELECT username FROM users WHERE role='client' AND password_hash IS NULL"):
        pw = secrets.token_urlsafe(12)
        conn.execute("UPDATE users SET password_hash=? WHERE username=?", (h(pw), row["username"]))
        generated.append((row["username"], pw))
    conn.commit()
    conn.close()
    for uname, pw in generated:
        print(f"[migrate_users] password inicial generado para '{uname}': {pw}  (se muestra UNA sola vez, en este log)")

_migrate_users()

@contextmanager
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ─── Auth ──────────────────────────────────────────────────────────────
def _hash(tok: str) -> str:
    import hashlib
    return hashlib.sha256(tok.encode()).hexdigest()

def _resolve_client_row(c, tok: str):
    """Resuelve un token de cliente a fila de users. Acepta:
    - el slot de sesión web (users.token_hash, sobrescrito en cada login), o
    - un token API self-service (user_tokens).
    Devuelve (row, kind) con kind 'web'|'api', o (None, None)."""
    th = _hash(tok)
    row = c.execute("SELECT * FROM users WHERE token_hash=? AND role='client' LIMIT 1", (th,)).fetchone()
    if row:
        return row, "web"
    row = c.execute("SELECT * FROM users WHERE username=(SELECT username FROM user_tokens WHERE token_hash=?) AND role='client' LIMIT 1", (th,)).fetchone()
    if row:
        return row, "api"
    return None, None

def current_client(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    tok = authorization[7:]
    with db() as c:
        row, kind = _resolve_client_row(c, tok)
        if row and kind == "api":
            # marca el último uso del token API (para mostrarlo en Configuración);
            # la sesión web no marca last_used (no es un token API)
            c.execute("UPDATE user_tokens SET last_used_at=datetime('now') WHERE username=? AND token_hash=?",
                      (row["username"], _hash(tok)))
    if not row:
        raise HTTPException(401, "invalid client token")
    d = dict(row)
    d["id"] = d["username"]   # alias para compat con código que usa client["id"]
    d["token_hash"] = _hash(tok)   # para identificar el token activo en la UI
    d["token_kind"] = kind  # 'web' (sesión) o 'api' (token self-service)
    return d

# ─── WebSocket /ws/events: eventos de estado de recursos en tiempo real ────
_ws_clients: set = set()   # websockets conectados a /ws/events (en memoria)
_ws_subs: dict = {}        # websocket -> {"role": "client"|"admin", "username": str}

async def _ws_auth(token: str) -> dict | None:
    """Valida el token del query param como current_client/admin_auth:
    token admin (ADMIN_TOKEN) → role admin; token de cliente en user_tokens →
    role client. Mismo criterio de los endpoints REST; retorna None si inválido."""
    if token and token == ADMIN_TOKEN:
        return {"role": "admin", "username": "admin"}
    with db() as c:
        row, _kind = _resolve_client_row(c, token)
    if row:
        return {"role": "client", "username": row["username"]}
    return None

async def _resource_event_broadcast(kind: str, ref_id: str, status: str, client_id: str | None = None):
    """Emite un evento de cambio de estado de una fila de resources a todos los
    clientes de /ws/events conectados. kind es 'vm' o 'svc' (UI)."""
    if not _ws_clients:
        return
    ev = {"type": "resource", "kind": kind, "ref": str(ref_id), "status": status,
          "at": time.time(), "client_id": client_id}
    msg = json.dumps(ev)
    for ws in list(_ws_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            _ws_clients.discard(ws)

@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    """Eventos de estado en tiempo real. Auth por token en el query param
    (mismo criterio que current_client/admin_auth; mismo patrón que /ws/ssh).
    El cliente recibe un hello y luego eventos JSON tipo resource:
    {type:'resource', kind, ref, status, at, client_id}."""
    sub = await _ws_auth(websocket.query_params.get("token", ""))
    if not sub:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _ws_clients.add(websocket)
    _ws_subs[websocket] = sub
    try:
        await websocket.send_text(json.dumps({"type": "hello", "role": sub["role"],
                                             "username": sub["username"], "at": time.time()}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(websocket)
        _ws_subs.pop(websocket, None)

# ─── Quota check ───────────────────────────────────────────────────────────
def _usage(client_id: str) -> dict:
    with db() as c:
        rows = c.execute(
            "SELECT kind, SUM(cpu) cpu, SUM(ram_mb) ram, SUM(disk_gb) disk, COUNT(*) n "
            "FROM resources WHERE client_id=? AND status='running' GROUP BY kind",
            (client_id,)).fetchall()
    out = {"vm_cpu":0,"vm_ram":0,"vm_disk":0,"vms":0,"c_cpu":0,"c_ram":0,"c_disk":0,"containers":0}
    for r in rows:
        if r["kind"]=="vm":
            out["vm_cpu"]=r["cpu"] or 0; out["vm_ram"]=r["ram"] or 0
            out["vm_disk"]=r["disk"] or 0; out["vms"]=r["n"]
        else:
            out["c_cpu"]=r["cpu"] or 0; out["c_ram"]=r["ram"] or 0
            out["c_disk"]=r["disk"] or 0; out["containers"]=r["n"]
    return out

def _check_vm_quota(client: dict, cpu: int, ram_gb: int, disk_gb: int):
    u = _usage(client["id"])
    if u["vms"] + 1 > client["max_vms"]:
        raise HTTPException(403, f"max_vms alcanzado ({client['max_vms']})")
    if u["vm_cpu"] + cpu > client["cpu_quota"]:
        raise HTTPException(403, f"cpu_quota superado: {u['vm_cpu']}+{cpu}>{client['cpu_quota']}")
    if u["vm_ram"] + ram_gb*1024 > client["ram_quota_mb"]:
        raise HTTPException(403, f"ram_quota superado: {u['vm_ram']}+{ram_gb*1024}>{client['ram_quota_mb']} MB")
    if u["vm_disk"] + disk_gb > client["disk_quota_gb"]:
        raise HTTPException(403, f"disk_quota superado: {u['vm_disk']}+{disk_gb}>{client['disk_quota_gb']}")

def _check_container_quota(client: dict, cpu: int, ram_mb: int, disk_gb: int=0):
    u = _usage(client["id"])
    if u["containers"] + 1 > client["max_containers"]:
        raise HTTPException(403, f"max_containers alcanzado ({client['max_containers']})")
    if u["c_cpu"] + cpu > client["cpu_quota"]:
        raise HTTPException(403, f"cpu_quota superado: {u['c_cpu']}+{cpu}>{client['cpu_quota']}")
    if u["c_ram"] + ram_mb > client["ram_quota_mb"]:
        raise HTTPException(403, f"ram_quota superado: {u['c_ram']}+{ram_mb}>{client['ram_quota_mb']} MB")
    if u["c_disk"] + disk_gb > client["disk_quota_gb"]:
        raise HTTPException(403, f"disk_quota superado: {u['c_disk']}+{disk_gb}>{client['disk_quota_gb']}")

# ─── Proxmox client ────────────────────────────────────────────────────
def pve_headers():
    return {"Authorization": f"PVEAPIToken={PVE_USER}!{PVE_TOKEN}={PVE_TOKEN_VAL}"}

async def pve_get(path: str) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=30) as c:
        r = await c.get(f"{PVE_HOST}/api2/json{path}", headers=pve_headers())
    if r.status_code >= 400:
        raise HTTPException(502, f"PVE error {r.status_code}: {r.text[:200]}")
    return r.json()

async def pve_post(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=120) as c:
        r = await c.post(f"{PVE_HOST}/api2/json{path}", headers=pve_headers(), data=data)
    if r.status_code >= 400:
        raise HTTPException(502, f"PVE error {r.status_code}: {r.text[:200]}")
    return r.json()

async def pve_delete(path: str) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=120) as c:
        r = await c.delete(f"{PVE_HOST}/api2/json{path}", headers=pve_headers())
    if r.status_code >= 400:
        raise HTTPException(502, f"PVE error {r.status_code}: {r.text[:200]}")
    return r.json()

async def pve_put(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=120) as c:
        r = await c.put(f"{PVE_HOST}/api2/json{path}", headers=pve_headers(), data=data)
    if r.status_code >= 400:
        raise HTTPException(502, f"PVE error {r.status_code}: {r.text[:200]}")
    return r.json()

# ─── Nomad client ──────────────────────────────────────────────────────
def nomad_headers():
    h = {}
    if NOMAD_TOKEN:
        h["Authorization"] = f"Bearer {NOMAD_TOKEN}"
    return h

async def nomad_get(path: str) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=30) as c:
        r = await c.get(f"{NOMAD_HOST}/v1{path}", headers=nomad_headers())
    if r.status_code >= 400:
        raise HTTPException(502, f"Nomad error {r.status_code}: {r.text[:200]}")
    return r.json()

async def nomad_post(path: str, body) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=60) as c:
        r = await c.post(f"{NOMAD_HOST}/v1{path}", headers=nomad_headers(), json=body)
    if r.status_code >= 400:
        raise HTTPException(502, f"Nomad error {r.status_code}: {r.text[:200]}")
    return r.json()

async def nomad_delete(path: str) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=60) as c:
        r = await c.delete(f"{NOMAD_HOST}/v1{path}", headers=nomad_headers())
    if r.status_code >= 400:
        raise HTTPException(502, f"Nomad error {r.status_code}: {r.text[:200]}")
    return r.json()


def _cf_request(method: str, path: str, payload: dict | None = None) -> dict:
    tok = open(CF_API_TOKEN_FILE).read().strip()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise HTTPException(502, f"Cloudflare API {e.code}: {body[:200]}")

CF_TUNNEL_ID = os.environ.get("CF_TUNNEL_ID", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN_FILE = os.environ.get("CF_API_TOKEN_FILE", "/opt/cloudapi/cf_api_token.txt")

def _cf_tunnel_ingress() -> list:
    res = _cf_request("GET", f"accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations")
    return res["result"]["config"]["ingress"]

def cf_set_ssh_host(host_id: str, port: int = None) -> None:
    """Asegura la regla UNIQUE gateway SSH en el tunnel:
    ssh.<dominio> (default :443) -> ssh://<GATEWAY_IP>:2222 (gateway unico, dispatch por usuario).
    La VM se alcanza con  ssh {host_id}@ssh.surus.lat
    (ProxyCommand: cloudflared access ssh --hostname ssh.surus.lat).
    Idempotente: una sola regla compartida para todas las VMs (NO regla por-VM)."""
    ingress = _cf_tunnel_ingress()
    # quitar reglas ssh:// por-VM antiguas y la catch-all (se rearmarán)
    keep = [r for r in ingress
            if "service" in r
            and not str(r.get("service", "")).startswith("http_status")
            and not str(r.get("service", "")).startswith("ssh://")]
    # la regla web del apex debe permanecer
    if not any(r.get("hostname") == CLOUD_DOMAIN and str(r.get("service", "")).startswith("http://") for r in keep):
        keep.insert(0, {"hostname": CLOUD_DOMAIN, "service": "http://localhost:8088"})
    keep.append({"hostname": SSH_DOMAIN, "service": f"ssh://{GATEWAY_IP}:2222"})
    keep.append({"service": "http_status:404"})
    res = _cf_request("PUT", f"accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
                      {"config": {"ingress": keep}})
    if not res.get("success"):
        raise HTTPException(502, f"CF configurations: {res.get('errors')}")

def cf_del_ssh_host(host_id: str) -> None:
    """El gateway SSH es UNICO y compartido; no se toca al borrar una VM.
    Solo se limpia una regla por-VM antigua de {host_id} si existiera."""
    ingress = _cf_tunnel_ingress()
    keep = [r for r in ingress if r.get("hostname") != f"{host_id}.{CLOUD_DOMAIN}"]
    if len(keep) == len(ingress):
        return  # no habia regla por-VM para borrar
    if not any(str(r.get("service", "")).startswith("http_status") for r in keep):
        keep.append({"service": "http_status:404"})
    res = _cf_request("PUT", f"accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
                      {"config": {"ingress": keep}})
    if not res.get("success"):
        raise HTTPException(502, f"CF del: {res.get('errors')}")

@app.get("/internal/gw-user/{host_id}")
def gw_user(host_id: str):
    """Para el gateway SSH unico: host_id -> (vmid, ip, claves publicas del cliente).
    Llamado localmente por sshkeycmd/sshgw-dispatch (solo desde 127.0.0.1)."""
    with db() as c:
        r = c.execute("SELECT ref_id, client_id FROM resources WHERE kind='vm' AND host_id=? AND status!='deleted' LIMIT 1", (host_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no vm")
        k = c.execute("SELECT ssh_key FROM users WHERE username=?", (r["client_id"],)).fetchone()
    return {"vmid": int(r["ref_id"]), "ip": vmIp_of(int(r["ref_id"])), "keys": (k["ssh_key"] if k else "")}

def sshgw_port(vmid: int) -> int:
    return 2200 + (vmid - VM_ID_BASE)

async def sshgw_up(vmid: int, host_id: str, extra_keys: str = "") -> None:
    """Gateway SSH UNICO (<CLOUD_DOMAIN> -> <GATEWAY_IP>:2222).
    Por VM: crea el usuario local <host_id>, genera la clave de salto y la inyecta.
    El sshd del gateway ya corre (servicio fijo); el dispatch por usuario vive en
    /opt/sshgw/sshgw-dispatch y /opt/sshgw/sshkeycmd."""
    key_path = f"/opt/sshgw/vm{vmid}.key"
    pub_path = f"/opt/sshgw/vm{vmid}.key.pub"
    if not os.path.exists(key_path):
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-C", f"jump-{vmid}"],
                       check=True, capture_output=True, text=True, timeout=30)
    pub = open(pub_path).read().strip()
    # usuario local por host_id (sshd con UsePAM no requiere el user en /etc/passwd)
    subprocess.run(["bash", "-c",
                    f"getent passwd '{host_id}' >/dev/null || "
                    f"(mkdir -p /home/{host_id} && "
                    f"useradd -M -s /bin/sh -d /home/{host_id} '{host_id}' && "
                    f"chown {host_id}:{host_id} /home/{host_id})"],
                   check=True, capture_output=True, timeout=30)
    # password aleatorio en shadow: sshd UsePAM no usa password, pero sudo
    # rechaza cuentas 'locked' (campo * o !) -> 'account validation failure'.
    # Un hash valido deja la cuenta no-lockeada y sin password adivinable.
    import secrets
    _pw = secrets.token_urlsafe(18)
    subprocess.run(["bash", "-c",
                    f"printf %s '{host_id}:${_pw}' | chpasswd"],
                   check=True, capture_output=True, timeout=15)
    # grupo vmssh: permite sudo sin password para /usr/local/bin/gwssh
    subprocess.run(["bash", "-c", "groupadd -f vmssh"], capture_output=True, timeout=15)
    subprocess.run(["usermod", "-aG", "vmssh", host_id], capture_output=True, timeout=15)
    # inyectar la clave publica de salto + claves del usuario en la VM.
    # ⚠ El worker REEMPLAZA authorized_keys (pct push): hay que inyectar la
    # UNION de ambas, si no sshgw_up borra la clave del cliente (bug 07/10).
    await inject_ssh(vmid, pub + "\n" + (extra_keys or ""))
    os.makedirs("/var/sshgw", exist_ok=True)
    subprocess.run(["cp", key_path, f"/var/sshgw/vm{vmid}.key"], check=True, capture_output=True, timeout=15)
    # 600 (no 644): ssh rechaza claves de salto legibles por todos ("bad permissions")
    subprocess.run(["chmod", "600", f"/var/sshgw/vm{vmid}.key"], check=True, capture_output=True, timeout=15)
    # rearmar el gateway unico (idempotente)
    subprocess.run(["systemctl", "restart", "ssh-gateway"], capture_output=True, timeout=30)

def sshgw_down(vmid: int, host_id: str = None) -> None:
    """Limpia el estado per-VM del gateway unico."""
    subprocess.run(["bash", "-c",
                    f"rm -f /var/sshgw/vm{vmid}.key /opt/sshgw/vm{vmid}.key /opt/sshgw/vm{vmid}.key.pub"],
                   capture_output=True, timeout=30)
    if host_id:
        subprocess.run(["userdel", host_id], capture_output=True, timeout=30)

def _gen_host_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    while True:
        h = "".join(secrets.choice(alphabet) for _ in range(6))
        with db() as c:
            r = c.execute("SELECT 1 FROM resources WHERE host_id=?", (h,)).fetchone()
        if not r:
            return h

def ensure_ssh_user(vmid: int) -> None:
    """Crea el usuario de login vm<id> local para el gateway SSH (idempotente)."""
    import subprocess
    u = f"vm{vmid}"
    try:
        r = subprocess.run(["id", u], capture_output=True, text=True)
        if r.returncode == 0:
            return
        subprocess.run(["useradd", "-M", "-s", "/usr/sbin/nologin", u],
                       capture_output=True, text=True, timeout=30)
        # sshd rechaza cuentas con shadow '!' (locked): usar '*'
        subprocess.run(["usermod", "-p", "*", u],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass

# ─── Schemas ───────────────────────────────────────────────────────────
# ─── GPU passthrough (solo QEMU) ───────────────────────────────────────
# PVE bloquea hostpci con host=<addr> directo para tokens (solo root),
# pero permite hostpci con mapping=<id> si el token tiene Mapping.Use
# sobre /mapping/pci/<id>. El token cloud-api tiene rol 'gpu-mgr' con
# Mapping.Modify/Use/Audit en /mapping/pci, así que el backend crea el
# mapping, asigna la VM con mapping= y lo borra al eliminar la VM.
async def _all_qemu_configs() -> dict:
    """vmid -> config dict para todas las QEMU del cluster (con error -> omitido).

    /cluster/resources devuelve una LISTA plana de items.
    """
    try:
        res = await pve_get("/cluster/resources")
        data = res.get("data")
        vms = data if isinstance(data, list) else []
    except HTTPException:
        return {}
    out = {}
    for v in vms:
        if not isinstance(v, dict) or v.get("type") != "qemu":
            continue
        try:
            c = await pve_get(f"/nodes/{PVE_NODE}/qemu/{v['vmid']}/config")
            out[v["vmid"]] = c.get("data", {}) or {}
        except HTTPException:
            pass
    return out

def _gpu_model_label(device: str, device_name: str) -> str:
    """Nombre amigable de la GPU para la UI.

    PVE reporta device_name tipo 'GA102GL [RTX A5000]'. Si trae un nombre entre
    corchetes lo usamos; si no, devolvemos el device PCI. De esta forma agregar
    un modelo nuevo no requiere tocar nada: aparece con su nombre real.
    """
    name = (device_name or "").strip()
    m = re.search(r"\[([^\]]+)\]", name)
    if m:
        return m.group(1).strip()
    if name:
        return name
    return f"GPU {device}"


async def _pci_mappings() -> dict:
    """mapping id -> {addr, node, raw} para los mappings PCI del servicio."""
    try:
        res = await pve_get("/cluster/mapping/pci")
    except HTTPException:
        return {}
    out = {}
    for m in res.get("data", []):
        if not m.get("id", "").startswith(GPU_MAP_PREFIX):
            continue
        for entry in m.get("map", []):
            kv = dict(p.split("=", 1) for p in entry.split(",") if "=" in p)
            out[m["id"]] = {"addr": kv.get("path"), "node": kv.get("node")}
    return out

async def _gpu_health() -> dict:
    """Estado electrico de las GPUs del host via el worker (sysfs del host).

    El cloudapi corre en un LXC y no puede leer /sys/bus/pci del host: el
    worker cloud-inject expone POST /gpu_health -> {addr: {alive, power}}.
    alive=False = config space lee todo ff (GPU colgada a nivel PCIe; solo
    revive con power-cycle/reboot). Fail-OPEN: si el worker no responde,
    ninguna GPU se marca muerta (no castigar al pool por un worker caido).
    """
    if not INJECT_TOKEN:
        return {}
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as c:
            r = await c.post(f"{INJECT_HOST}/gpu_health",
                             headers={"Authorization": f"Bearer {INJECT_TOKEN}"})
        if r.status_code != 200:
            return {}
        return r.json() if isinstance(r.json(), dict) else {}
    except (httpx.HTTPError, ValueError):
        return {}


async def gpu_inventory() -> list:
    """Inventario live de GPUs: [{addr, name, model, iommu, in_use_by: vmid|mapping|None}].

    En uso = alguna QEMU la tiene como hostpci (directo o vía mapping).
    `model` es un nombre amigable para la UI (ej. "RTX A5000") derivado del
    device_name de PVE; si no se reconoce, se usa el device PCI.
    La UI NUNCA muestra la info de ocupación; se usa solo al crear.
    """
    try:
        pci = await pve_get("/nodes/proxmox/hardware/pci")
        gpus = [d for d in pci.get("data", [])
                if d.get("vendor") == GPU_VENDOR
                and str(d.get("class", "")).startswith("0x03")]
    except HTTPException:
        return []
    qemu = await _all_qemu_configs()
    # addr -> dueño (vmid directo)  |  mapping -> vmid
    owner_by_addr, vm_by_mapping = {}, {}
    for vmid, cfg in qemu.items():
        for k, v in cfg.items():
            if not str(k).startswith("hostpci"):
                continue
            s = str(v)
            if "mapping=" in s:
                vm_by_mapping[s.split("mapping=")[1].split(",")[0]] = vmid
            else:
                for p in s.split(","):
                    p = p.strip()
                    if p.count(":") == 2:
                        owner_by_addr[p] = vmid
    mappings = await _pci_mappings()
    health = await _gpu_health()
    # addr -> mapping id (para saber qué GPU toma un mapping del servicio)
    addr_by_mapping = {mid: info["addr"] for mid, info in mappings.items() if info.get("addr")}
    out = []
    for g in gpus:
        addr = g["id"]
        vm = owner_by_addr.get(addr)
        if vm is None:
            # ¿algún mapping del servicio apunta a esta GPU?
            mid = next((m for m, a in addr_by_mapping.items() if a == addr), None)
            if mid is not None:
                # Solo cuenta como ocupada si ALGUNA VM la referencia.
                # Un mapping huérfano (sin VM) NO consume el dispositivo.
                vm = vm_by_mapping.get(mid)
        out.append({"addr": addr, "name": g.get("device_name", ""),
                    "model": _gpu_model_label(g.get("device", ""), g.get("device_name", "")),
                    "device": g.get("device", ""),
                    "subsystem_device": g.get("subsystem_device", ""),
                    "iommu": g.get("iommugroup"), "in_use_by": vm,
                    "dead": health.get(addr, {}).get("alive", True) is False})
    return out


def _gpu_pick_order(free: list, prefer_model: str) -> list:
    """Ordena las GPUs libres: si pide un modelo, primero las que lo tengan y
    luego el resto. Vacio = orden natural (cualquiera sirve)."""
    if not prefer_model:
        return free
    want = [g for g in free if g.get("model") == prefer_model]
    rest = [g for g in free if g.get("model") != prefer_model]
    return want + rest


async def _claim_free_gpu(prefer_model: str = "") -> dict:
    """Reserva una GPU libre: crea el mapping PCI y devuelve {gpu, mapping_id}.

    Si prefer_model se indica, intenta ANTES una tarjeta de ese modelo (y si no
    queda ninguna de ese modelo, usa la siguiente libre). Si no hay ninguna
    libre: 409. Si PVE la rechaza (competencia/estado): prueba la siguiente; si
    ninguna queda, 409.
    """
    inv = await gpu_inventory()
    # GPUs electricamente muertas (config space ff) salen del pool: asignarlas
    # da una VM corriendo sin GPU (nvidia-smi: "No NVIDIA GPU found").
    free = _gpu_pick_order([g for g in inv
                            if g["in_use_by"] is None and not g.get("dead")],
                           prefer_model)
    if not free:
        raise HTTPException(409, "no hay GPUs disponibles en el cluster")
    last_err = None
    for g in free:
        # pve-configid NO permite puntos: 0000:e1:00.0 -> 0000-e1-00-0
        map_id = f"{GPU_MAP_PREFIX}-{g['addr'].replace(':', '-').replace('.', '-')}"
        # id/subsystem-id desde los PCI reales del device (no hardcodear por
        # modelo: al agregar otra GPU debe seguir funcionando). PVE espera el
        # formato sin prefijo 0x (10de:2231), los campos de hardware/pci llegan
        # con 0x → normalizar.
        device = (g.get("device") or "").removeprefix("0x")
        subsys = (g.get("subsystem_device") or "").removeprefix("0x")
        vendor = GPU_VENDOR.removeprefix("0x")
        entry = (f"node={PVE_NODE},path={g['addr']},id={vendor}:{device},"
                 f"subsystem-id={vendor}:{subsys},iommugroup={g['iommu']}")
        try:
            await pve_post("/cluster/mapping/pci", {"id": map_id, "map": entry})
        except HTTPException as e:
            det = str(e.detail)
            if "already defined" in det:
                # El mapping ya existe: reutilizar SOLO si ninguna VM lo
                # referencia (huérfano de una creación abortada). Si otra VM
                # lo usa, probar la siguiente GPU.
                maps = await _pci_mappings()
                info = maps.get(map_id, {})
                # ¿alguna VM referencia este mapping?
                referenced = False
                for cfg in (await _all_qemu_configs()).values():
                    for kk, vv in cfg.items():
                        if str(kk).startswith("hostpci") and f"mapping={map_id}" in str(vv):
                            referenced = True
                            break
                    if referenced:
                        break
                if referenced:
                    continue  # otra VM la tomó a la vez; probar la siguiente
                # huérfano: reutilizar (verificar que mapea la GPU correcta)
                if info.get("addr") != g["addr"]:
                    await pve_delete(f"/cluster/mapping/pci/{map_id}")
                    continue
                return {"gpu": g, "mapping_id": map_id}
            last_err = e
            continue
        # verificar que el mapping quedó mapeando la GPU correcta
        maps = await _pci_mappings()
        if maps.get(map_id, {}).get("addr") != g["addr"]:
            await pve_delete(f"/cluster/mapping/pci/{map_id}")
            continue
        return {"gpu": g, "mapping_id": map_id}
    raise HTTPException(409, "no hay GPUs disponibles en el cluster")

async def _release_gpu(mapping_id: str):
    """Borra el mapping PCI (la GPU vuelve a estar libre)."""
    try:
        await pve_delete(f"/cluster/mapping/pci/{mapping_id}")
    except HTTPException:
        pass  # ya no existe; no bloquear

class VMCreate(BaseModel):
    cpu: int = 1
    ram_gb: int = 2
    disk_gb: int = 10
    name: str = ""
    image: str = TEMPLATE
    gpu: bool = False          # VM con GPU passthrough (QEMU). LXC no lo soporta.
    gpu_model: str = ""        # Modelo de GPU pedido (ej. "RTX A5000"). Vacio = cualquiera de las libres.


class ServiceCreate(BaseModel):
    name: str
    image: str = "nginx:alpine"
    cpu: int = 1
    ram_mb: int = 256
    replicas: int = 1
    port: int = 8080

class AdminCreateClient(BaseModel):
    id: str
    cpu_quota: int = 4
    ram_quota_mb: int = 8192
    disk_quota_gb: int = 40
    max_vms: int = 2
    max_containers: int = 3
    ssh_key: str = ""

# ─── Admin ─────────────────────────────────────────────────────────────
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me")

def admin_auth(authorization: str = Header(default="")):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(403, "invalid admin token")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

@app.get("/", include_in_schema=False)
def index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.get("/app-admin.js", include_in_schema=False)
def app_admin_js():
    # Bundle admin diferido: solo lo pide un cliente con role=admin (lazy-load).
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(WEB_DIR, "app-admin.js"),
                        media_type="application/javascript")

@app.get("/react/island.js", include_in_schema=False)
def react_island():
    """Isla React del wizard (build IIFE de web-react/src/island.ts).
    Si el archivo no está deployado, 404 → el SPA detecta el onerror del
    <script> y cae al modal vanilla (fallback, ver ensureIsland en index.html)."""
    from fastapi.responses import FileResponse
    p = os.path.join(WEB_DIR, "react-island.js")
    if not os.path.exists(p):
        raise HTTPException(404, "island no deployada")
    return FileResponse(p, media_type="application/javascript")

@app.get("/api/brand")
def brand():
    return {"name": "nubecita", "domain": CLOUD_DOMAIN, "ssh_domain": SSH_DOMAIN,
            "vm_net": VM_NET_PREFIX}

@app.get("/api/health")
def api_health():
    return {"ok": True, "ts": int(time.time())}

@app.get("/api/gpus")
async def gpus_available():
    """Modelos de GPU del cluster para el dropdown de creación: ["RTX A5000", ...].

    Solo modelos (sin disponibilidad): la UI nunca muestra cuántas están libres;
    si no queda ninguna, el create responde 409 y eso es el aviso.
    """
    inv = await gpu_inventory()
    models = sorted({g["model"] for g in inv if g["model"] and not g.get("dead")})
    return models

class LoginReq(BaseModel):
    username: str
    password: str

# ─── Rate-limit de login (en memoria, por IP cliente) ──────────────────
# dict IP -> [timestamps de intentos]; tope 5 intentos por ventana de 60s.
# Sin dependencias externas: dict + lock alcanza (estado se pierde al restart,
# aceptable para un rate-limit defensivo).
LOGIN_RATE_MAX = 5
LOGIN_RATE_WINDOW = 60  # segundos
_LOGIN_ATTEMPTS: dict[str, list] = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()

def _login_rate_check(host: str) -> None:
    """Lanza 429 si la IP excedió el tope; si no, registra el intento."""
    now = time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        attempts = [t for t in _LOGIN_ATTEMPTS.get(host, []) if now - t < LOGIN_RATE_WINDOW]
        if len(attempts) >= LOGIN_RATE_MAX:
            raise HTTPException(429, "demasiados intentos de login, esperá 1 minuto")
        attempts.append(now)
        _LOGIN_ATTEMPTS[host] = attempts

def _login_rate_reset(host: str) -> None:
    """Intento exitoso: resetea el contador de la IP."""
    with _LOGIN_ATTEMPTS_LOCK:
        _LOGIN_ATTEMPTS.pop(host, None)

@app.post("/api/login")
def web_login(body: LoginReq, request: Request):
    import hashlib
    host = request.client.host if request.client else ""
    _login_rate_check(host)
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not row or not row["password_hash"] or row["password_hash"] != hashlib.sha256(body.password.encode()).hexdigest():
        raise HTTPException(401, "usuario o contraseña inválidos")
    _login_rate_reset(host)
    if row["role"] == "admin":
        return {"role": "admin", "token": ADMIN_TOKEN, "username": row["username"]}
    import secrets as _sec
    # Sesión web: un solo slot por usuario (users.token_hash), sobrescrito en
    # cada login — NO crea tokens API (user_tokens queda solo para tokens
    # self-service creados en Configuración). El token web anterior queda inválido.
    token = _sec.token_urlsafe(32)
    with db() as c:
        c.execute("UPDATE users SET token_hash=? WHERE username=?", (_hash(token), row["username"]))
    return {"role": "client", "token": token, "username": row["username"], "client_id": row["username"]}

class QuotaUpdate(BaseModel):
    cpu_quota: Optional[int] = None
    ram_quota_mb: Optional[int] = None
    disk_quota_gb: Optional[int] = None
    max_vms: Optional[int] = None
    max_containers: Optional[int] = None

@app.get("/admin/clients", dependencies=[Depends(admin_auth)], include_in_schema=False)
def legacy_clients():
    # compat: lista usuarios role=client como "clientes"
    return list_users(role="client")

@app.get("/admin/users", dependencies=[Depends(admin_auth)])
def list_users(role: Optional[str] = None, full: bool = False):
    with db() as c:
        q = "SELECT * FROM users"
        args = []
        if role:
            q += " WHERE role=?"
            args.append(role)
        q += " ORDER BY username"
        rows = c.execute(q, args).fetchall()

        # full=true: agregación en 3 queries totales (sin N+1 por usuario)
        usage_by_client, vm_rows, ev_rows = {}, [], []
        if full:
            usage_rows = c.execute(
                "SELECT client_id, kind, SUM(cpu) cpu, SUM(ram_mb) ram, SUM(disk_gb) disk, COUNT(*) n "
                "FROM resources WHERE status='running' GROUP BY client_id, kind").fetchall()
            for r in usage_rows:
                u = usage_by_client.setdefault(r["client_id"],
                    {"vm_cpu":0,"vm_ram":0,"vm_disk":0,"vms":0,"c_cpu":0,"c_ram":0,"c_disk":0,"containers":0})
                if r["kind"] == "vm":
                    u["vm_cpu"] = r["cpu"] or 0; u["vm_ram"] = r["ram"] or 0
                    u["vm_disk"] = r["disk"] or 0; u["vms"] = r["n"]
                else:
                    u["c_cpu"] = r["cpu"] or 0; u["c_ram"] = r["ram"] or 0
                    u["c_disk"] = r["disk"] or 0; u["containers"] = r["n"]
            vm_rows = c.execute(
                "SELECT client_id, cpu, ram_mb, disk_gb, gpu_mapping"
                " FROM resources WHERE kind='vm' AND status='running'").fetchall()
            ev_rows = c.execute(
                "SELECT username, delta, reason, created_at FROM credit_events ORDER BY id DESC").fetchall()

        out = []
        for r in rows:
            d = dict(r)
            d.pop("token_hash", None)
            ntok = c.execute("SELECT COUNT(*) FROM user_tokens WHERE username=?", (d["username"],)).fetchone()[0]
            d["has_token"] = ntok > 0
            d["n_tokens"] = ntok
            d["credits"] = float(d.get("credits") or 0)
            d["has_password"] = bool(d.get("password_hash"))
            d.pop("password_hash", None)
            if full and d["role"] == "client":
                uname = d["username"]
                d["used"] = usage_by_client.get(uname,
                    {"vm_cpu":0,"vm_ram":0,"vm_disk":0,"vms":0,"c_cpu":0,"c_ram":0,"c_disk":0,"containers":0})
                rate = sum(vm_hourly_cost(v["cpu"], v["ram_mb"], v["disk_gb"],
                                         _gpu_units(v))
                           for v in vm_rows if v["client_id"] == uname)
                d["cost_per_hour"] = round(rate, 4)
                d["hours_left"] = round(d["credits"] / rate, 2) if rate > 0 else None
                d["events"] = []
            out.append(d)
        if full:
            # últimos 25 eventos por usuario (Python: los eventos ya están ordenados por id DESC)
            by_u: dict = {d["username"]: d for d in out if d["role"] == "client"}
            for e in ev_rows:
                d = by_u.get(e["username"])
                if d and len(d["events"]) < 25:
                    d["events"].append({"delta": round(e["delta"], 4),
                                        "reason": e["reason"], "created_at": e["created_at"]})
    return out

@app.delete("/admin/clients/{client_id}", dependencies=[Depends(admin_auth)], include_in_schema=False)
def legacy_delete_client(client_id: str):
    return admin_delete_user(client_id)

@app.delete("/admin/users/{username}", dependencies=[Depends(admin_auth)])
def admin_delete_user(username: str):
    with db() as c:
        r = c.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            raise HTTPException(404, "usuario no existe")
        if r["role"] == "admin":
            raise HTTPException(400, "no se puede borrar un admin desde acá")
        c.execute("DELETE FROM users WHERE username=?", (username,))
        c.execute("DELETE FROM user_tokens WHERE username=?", (username,))
    return {"username": username, "deleted": True}

@app.get("/admin/users/{username}/tokens", dependencies=[Depends(admin_auth)])
def admin_list_tokens(username: str):
    """Lista los tokens API de un usuario (admin). El id es el token_hash (nunca el token)."""
    with db() as c:
        r = c.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            raise HTTPException(404, "usuario no existe")
        rows = c.execute(
            "SELECT token_hash, label, created_at, last_used_at FROM user_tokens "
            "WHERE username=? ORDER BY created_at DESC", (username,)).fetchall()
    return {"username": username,
            "tokens": [{"id": t["token_hash"], "label": t["label"] or "",
                        "created_at": t["created_at"], "last_used_at": t["last_used_at"]}
                       for t in rows]}

@app.delete("/admin/users/{username}/tokens/{token_id}", dependencies=[Depends(admin_auth)])
def admin_revoke_token(username: str, token_id: str):
    """Revoca UN token del usuario (el resto sigue vivo)."""
    with db() as c:
        r = c.execute("SELECT token_hash FROM user_tokens WHERE username=? AND token_hash=?",
                      (username, token_id)).fetchone()
        if not r:
            raise HTTPException(404, "token no existe")
        c.execute("DELETE FROM user_tokens WHERE username=? AND token_hash=?",
                  (username, token_id))
    return {"ok": True}

class UserCreate(BaseModel):
    username: str
    password: str = ""
    role: str                 # 'admin' | 'client'
    cpu_quota: int = 4
    ram_quota_mb: int = 8192
    disk_quota_gb: int = 40
    max_vms: int = 2
    max_containers: int = 3
    ssh_key: str = ""

class PasswordUpdate(BaseModel):
    password: str

class SSHKeyUpdate(BaseModel):
    ssh_key: str

@app.post("/admin/users", dependencies=[Depends(admin_auth)])
def admin_create_user(body: UserCreate):
    import hashlib
    if body.role not in ("admin", "client"):
        raise HTTPException(400, "role debe ser 'admin' o 'client'")
    if body.role == "client":
        if len(body.password) < 8:
            raise HTTPException(400, "contraseña mínima 8 caracteres")
        token = secrets.token_urlsafe(32)
        tok_hash = _hash(token)
    else:
        token, tok_hash = None, None
    try:
        with db() as c:
            c.execute(
                "INSERT INTO users (username, role, password_hash, token_hash, cpu_quota, ram_quota_mb, disk_quota_gb, max_vms, max_containers, ssh_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (body.username, body.role,
                 hashlib.sha256(body.password.encode()).hexdigest() if body.role == "client" and body.password else None,
                 None,
                 body.cpu_quota, body.ram_quota_mb, body.disk_quota_gb, body.max_vms, body.max_containers,
                 body.ssh_key))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "usuario ya existe")
    if token:
        with db() as c:
            c.execute("INSERT INTO user_tokens (username, token_hash) VALUES (?,?)", (body.username, _hash(token)))
    return {"username": body.username, "role": body.role,
            **({} if not token else {"token": token, "note": "guardá este token: no se puede volver a leer"})}

@app.post("/admin/clients", dependencies=[Depends(admin_auth)], include_in_schema=False)
def legacy_create_client(body: AdminCreateClient):
    # compat: crea usuario client con token
    import hashlib
    pw = secrets.token_urlsafe(12)
    token = secrets.token_urlsafe(32)
    try:
        with db() as c:
            c.execute(
                "INSERT INTO users (username, role, password_hash, token_hash, cpu_quota, ram_quota_mb, disk_quota_gb, max_vms, max_containers, ssh_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (body.id, "client", hashlib.sha256(pw.encode()).hexdigest(), _hash(token),
                 body.cpu_quota, body.ram_quota_mb, body.disk_quota_gb, body.max_vms, body.max_containers, body.ssh_key))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "client ya existe")
    return {"client_id": body.id, "token": token, "password": pw,
            "note": "guardá este token y esta contraseña: no se pueden volver a leer"}

@app.get("/admin/quota/{client_id}", dependencies=[Depends(admin_auth)])
def admin_quota(client_id: str):
    with db() as c:
        cl = c.execute("SELECT * FROM users WHERE username=? AND role='client'", (client_id,)).fetchone()
    if not cl:
        raise HTTPException(404, "no existe el cliente")
    u = _usage(client_id)
    d = dict(cl); d.pop("password_hash", None); d.pop("token_hash", None); d.pop("ssh_private_key", None)
    return {"client": d, "used": u}

@app.post("/admin/clients/{client_id}/token", dependencies=[Depends(admin_auth)])
def admin_regenerate_token(client_id: str):
    token = secrets.token_urlsafe(32)
    with db() as c:
        r = c.execute("SELECT role FROM users WHERE username=? AND role='client'", (client_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no existe el cliente")
        c.execute("DELETE FROM user_tokens WHERE username=?", (client_id,))
        c.execute("INSERT INTO user_tokens (username, token_hash) VALUES (?,?)", (client_id, _hash(token)))
        c.execute("UPDATE users SET token_hash=NULL WHERE username=?", (client_id,))  # invalida la sesión web también
    return {"client_id": client_id, "token": token,
            "note": "guardá este token: TODOS los tokens anteriores (web y API) quedaron revocados"}

@app.put("/admin/users/{username}/quota", dependencies=[Depends(admin_auth)])
def admin_update_quota(username: str, body: QuotaUpdate):
    fields, vals = [], []
    for f in ("cpu_quota", "ram_quota_mb", "disk_quota_gb", "max_vms", "max_containers"):
        v = getattr(body, f)
        if v is not None:
            fields.append(f"{f}=?"); vals.append(v)
    if not fields:
        raise HTTPException(400, "nada para actualizar")
    vals.append(username)
    with db() as c:
        r = c.execute(f"UPDATE users SET {', '.join(fields)} WHERE username=? AND role='client'", vals)
        if r.rowcount == 0:
            raise HTTPException(404, "no existe el cliente")
        cl = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    d = dict(cl); d.pop("password_hash", None); d.pop("token_hash", None)
    return {"client": d, "used": _usage(username)}

@app.put("/admin/clients/{client_id}/quota", dependencies=[Depends(admin_auth)], include_in_schema=False)
def legacy_update_quota(client_id: str, body: QuotaUpdate):
    return admin_update_quota(client_id, body)

@app.post("/admin/users/{username}/password", dependencies=[Depends(admin_auth)])
def admin_set_password(username: str, body: PasswordUpdate):
    import hashlib
    if len(body.password) < 8:
        raise HTTPException(400, "contraseña mínima 8 caracteres")
    with db() as c:
        r = c.execute("UPDATE users SET password_hash=? WHERE username=?",
                      (hashlib.sha256(body.password.encode()).hexdigest(), username))
        if r.rowcount == 0:
            raise HTTPException(404, "usuario no existe")
    return {"username": username, "ok": True}

@app.post("/admin/users/{username}/ssh-key", dependencies=[Depends(admin_auth)])
def admin_set_ssh_key(username: str, body: SSHKeyUpdate):
    with db() as c:
        r = c.execute("UPDATE users SET ssh_key=? WHERE username=? AND role='client'", (body.ssh_key, username))
        if r.rowcount == 0:
            raise HTTPException(404, "usuario no existe o no es client")
    return {"username": username, "ok": True}

@app.get("/admin/web-users", include_in_schema=False)
def legacy_web_users():
    return list_users()

def pve_get_sync(path: str) -> dict:
    import ssl
    import urllib.request
    url = f"{PVE_HOST}/api2/json{path}"
    req = urllib.request.Request(url, headers=pve_headers())
    try:
        # Cert auto-firmado de la API de PVE: mismo trato que el resto de
        # llamadas (httpx verify=False). Sin esto urllib falla con
        # CERTIFICATE_VERIFY_FAILED.
        with urllib.request.urlopen(req, timeout=15,
                                    context=ssl._create_unverified_context()) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"PVE {e.code}")

def _pve_vm_status(vmid: int, kind: str = "lxc") -> dict:
    try:
        st = pve_get_sync(f"/nodes/{PVE_NODE}/{kind}/{vmid}/status/current")
        return st.get("data", {}) or {}
    except Exception:
        # si no existe del tipo indicado, podría ser del otro tipo
        other = "qemu" if kind == "lxc" else "lxc"
        try:
            st = pve_get_sync(f"/nodes/{PVE_NODE}/{other}/{vmid}/status/current")
            return st.get("data", {}) or {}
        except Exception:
            return {"status": "absent"}

@app.get("/admin/resources", dependencies=[Depends(admin_auth)])
def admin_list_resources():
    """Todos los recursos del ledger + estado live de VMs (drift detection)."""
    with db() as c:
        rows = c.execute("""SELECT r.*, cl.id AS client FROM resources r
                            LEFT JOIN clients cl ON cl.id=r.client_id
                            ORDER BY r.created_at DESC, r.id DESC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        live = None
        if d["kind"] == "vm" and d["status"] != "deleted":
            try:
                vkind = "qemu" if d.get("gpu_mapping") else "lxc"
                live = _pve_vm_status(int(d["ref_id"]), vkind).get("status")
            except Exception:
                live = None
        d["pve_status"] = live
        d["drift"] = (d["kind"] == "vm" and d["status"] != "deleted"
                      and live is not None and live != d["status"])
        out.append(d)
    return out

@app.get("/admin/clients/{client_id}/resources", dependencies=[Depends(admin_auth)])
def admin_client_resources(client_id: str):
    with db() as c:
        r = c.execute("SELECT username FROM users WHERE username=? AND role='client'", (client_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no existe el cliente")
        rows = c.execute("SELECT * FROM resources WHERE client_id=? ORDER BY created_at DESC",
                         (client_id,)).fetchall()
    return [dict(x) for x in rows]

@app.get("/admin/overview", dependencies=[Depends(admin_auth)])
def admin_overview():
    with db() as c:
        clients = [dict(r) for r in c.execute("SELECT * FROM users WHERE role='client'").fetchall()]
        for cl in clients:
            cl["id"] = cl["username"]  # alias
        res = c.execute("""SELECT client_id, kind, COUNT(*) n,
                                  SUM(COALESCE(cpu,0)) cpu, SUM(COALESCE(ram_mb,0)) ram,
                                  SUM(COALESCE(disk_gb,0)) disk
                           FROM resources WHERE status='running' GROUP BY client_id, kind""").fetchall()
    per = {}
    for r in res:
        p = per.setdefault(r["client_id"], {"vms": 0, "containers": 0, "cpu": 0, "ram_mb": 0, "disk_gb": 0})
        p["vms" if r["kind"] == "vm" else "containers"] = r["n"]
        p["cpu"] = r["cpu"]; p["ram_mb"] = r["ram"]; p["disk_gb"] = r["disk"]
    total_q = {"cpu": 0, "ram_mb": 0, "disk_gb": 0, "vms": 0, "containers": 0}
    total_u = {"cpu": 0, "ram_mb": 0, "disk_gb": 0, "vms": 0, "containers": 0}
    for cl in clients:
        total_q["cpu"] += cl["cpu_quota"]; total_q["ram_mb"] += cl["ram_quota_mb"]
        total_q["disk_gb"] += cl["disk_quota_gb"]; total_q["vms"] += cl["max_vms"]
        total_q["containers"] += cl["max_containers"]
        u = per.get(cl["id"])
        if u:
            for k in ("cpu", "ram_mb", "disk_gb", "vms", "containers"):
                total_u[k] += u[k]
    safe = []
    for c in clients:
        d = dict(c)
        d.pop("password_hash", None); d.pop("token_hash", None); d.pop("ssh_private_key", None)
        safe.append(d)
    return {"clients": safe, "per_client": per,
            "total_quota": total_q, "total_used": total_u,
            "counts": {"clients": len(clients),
                       "running_vms": total_u["vms"],
                       "running_containers": total_u["containers"]}}

# ─── Tarifas de costo por hora (admin) ─────────────────────────────────
RATE_KEYS = ("cpu", "ram_gb", "disk_gb", "gpu")
RATE_LABELS = {"cpu": "vCPU (cr/core/h)", "ram_gb": "RAM (cr/GB/h)",
               "disk_gb": "Disco (cr/GB/h)", "gpu": "GPU (cr/unidad/h)"}

class RateUpdate(BaseModel):
    cpu: Optional[float] = None
    ram_gb: Optional[float] = None
    disk_gb: Optional[float] = None
    gpu: Optional[float] = None

@app.get("/admin/rates", dependencies=[Depends(admin_auth)])
def admin_get_rates():
    """Tasas vigentes (créditos/hora) + valores por defecto para reset."""
    return {"rates": {k: CREDIT_RATES.get(k, DEFAULT_RATES.get(k)) for k in RATE_KEYS},
            "defaults": DEFAULT_RATES,
            "labels": RATE_LABELS}

@app.put("/admin/rates", dependencies=[Depends(admin_auth)])
def admin_set_rates(body: RateUpdate):
    """Actualiza una o más tasas; el biller las usa desde el próximo tick."""
    for k in RATE_KEYS:
        v = getattr(body, k)
        if v is not None and v < 0:
            raise HTTPException(400, f"la tasa {k} debe ser >= 0")
    if not any(getattr(body, k) is not None for k in RATE_KEYS):
        raise HTTPException(400, "nada para actualizar")
    with db() as c:
        for k in RATE_KEYS:
            v = getattr(body, k)
            if v is not None:
                c.execute("INSERT INTO credit_rates (rate_key, value) VALUES (?,?) "
                          "ON CONFLICT(rate_key) DO UPDATE SET value=excluded.value", (k, float(v)))
        CREDIT_RATES.update({k: float(getattr(body, k)) for k in RATE_KEYS if getattr(body, k) is not None})
    return {"rates": {k: CREDIT_RATES.get(k) for k in RATE_KEYS}}

async def _admin_vm_action(vmid: int, action: str, client_id: str | None = None):
    # client_id: scoping de ownership — si se pasa, la VM debe ser de ese cliente
    with db() as c:
        if client_id is not None:
            row = c.execute("SELECT gpu_mapping FROM resources WHERE client_id=? AND kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                            (client_id, str(vmid))).fetchone()
            if not row:
                raise HTTPException(404, "VM no existe o no te pertenece")
        else:
            row = c.execute("SELECT gpu_mapping FROM resources WHERE kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                            (str(vmid),)).fetchone()
    api = "qemu" if (row and row["gpu_mapping"]) else "lxc"
    try:
        st = await pve_get(f"/nodes/{PVE_NODE}/{api}/{vmid}/status/current")
        cur = (st.get("data") or {}).get("status")
    except HTTPException:
        raise HTTPException(404, f"VM {vmid} no existe en PVE")
    if action == "stop" and cur == "stopped":
        return {"vmid": vmid, "status": "stopped"}
    if action == "start" and cur == "running":
        return {"vmid": vmid, "status": "running"}
    # no arrancar sin saldo: facturaría contra 0 y se detendría a los 60s
    with db() as c:
        res = c.execute("SELECT * FROM resources WHERE kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                        (str(vmid),)).fetchone()
    if res and _user_credits(res["client_id"]) <= 0:
        raise HTTPException(402, "el cliente no tiene créditos; hacé un top-up antes de arrancar la VM")
    await pve_post(f"/nodes/{PVE_NODE}/{api}/{vmid}/status/{action}", {})
    want = {"start": "running", "stop": "stopped", "restart": "running"}[action]
    for _ in range(24):
        await asyncio.sleep(5)
        try:
            st = await pve_get(f"/nodes/{PVE_NODE}/{api}/{vmid}/status/current")
            cur = (st.get("data") or {}).get("status")
            if cur == want:
                break
        except HTTPException:
            raise HTTPException(502, f"VM {vmid} desapareció de PVE durante la operación")
    with db() as c:
        c.execute("UPDATE resources SET status=? WHERE kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                  (want, str(vmid)))
    asyncio.create_task(_resource_event_broadcast("vm", str(vmid), want, res["client_id"] if res else None))
    return {"vmid": vmid, "status": cur}

@app.post("/admin/vm/{vmid}/start", dependencies=[Depends(admin_auth)])
async def admin_vm_start(vmid: int):
    return await _admin_vm_action(vmid, "start")

@app.post("/admin/vm/{vmid}/stop", dependencies=[Depends(admin_auth)])
async def admin_vm_stop(vmid: int):
    return await _admin_vm_action(vmid, "stop")

@app.post("/admin/vm/{vmid}/restart", dependencies=[Depends(admin_auth)])
async def admin_vm_restart(vmid: int):
    return await _admin_vm_action(vmid, "restart")

@app.delete("/admin/resource/{kind}/{ref_id}", dependencies=[Depends(admin_auth)])
async def admin_force_delete_resource(kind: str, ref_id: str):
    """Borrar un recurso del ledger aunque su estado PVE/Nomad esté desfasado."""
    if kind not in ("vm", "container"):
        raise HTTPException(400, "kind inválido")
    with db() as c:
        r = c.execute("SELECT * FROM resources WHERE kind=? AND ref_id=? AND status!='deleted' LIMIT 1",
                      (kind, ref_id)).fetchone()
    if not r:
        raise HTTPException(404, "recurso no existe en el ledger (o ya está borrado)")
    try:
        if kind == "vm":
            gmap = r["gpu_mapping"] if "gpu_mapping" in r.keys() else None
            api = "qemu" if gmap else "lxc"
            try:
                await pve_post(f"/nodes/{PVE_NODE}/{api}/{int(ref_id)}/status/stop", {})
            except HTTPException:
                pass
            try:
                await pve_delete(f"/nodes/{PVE_NODE}/{api}/{int(ref_id)}")
            except HTTPException:
                pass
            if gmap:
                await _release_gpu(gmap)
            host_id = r["host_id"] if "host_id" in r.keys() else None
            try:
                sshgw_down(int(ref_id), host_id)
                if host_id:
                    cf_del_ssh_host(host_id)
            except Exception:
                pass
        else:
            try:
                await nomad_delete(f"/jobs/{ref_id}?purge=true")
            except HTTPException:
                pass
    except HTTPException:
        pass
    with db() as c:
        c.execute("UPDATE resources SET status='deleted' WHERE kind=? AND ref_id=? AND status!='deleted'",
                  (kind, ref_id))
    return {"kind": kind, "ref_id": ref_id, "status": "deleted"}

# Caché del resultado de /admin/system: TTL 60s. El gather (PVE + Nomad +
# systemctl) hace 2-3 llamadas de red/subprocess por request; cachearlo evita
# martillear las dependencias al refrescar el panel de admin.
_SYSTEM_CACHE: dict = {"ts": 0.0, "data": None}
_SYSTEM_CACHE_LOCK = threading.Lock()
_SYSTEM_CACHE_TTL = 60  # segundos

def _admin_system_fresh() -> bool:
    return _SYSTEM_CACHE["data"] is not None and (time.time() - _SYSTEM_CACHE["ts"]) <= _SYSTEM_CACHE_TTL

@app.get("/admin/system", dependencies=[Depends(admin_auth)])
def admin_system():
    """Salud del plano de control: PVE node, Nomad, ssh gateways."""
    with _SYSTEM_CACHE_LOCK:
        if _admin_system_fresh():
            return _SYSTEM_CACHE["data"]
    import subprocess
    out = {}
    try:
        # El estado plano por nodo (status/cpu/mem/disco) vive en
        # /cluster/resources: /nodes/{node} lista SUBRECURSOS de la API
        # (devuelve una LISTA, no el estado) y /status no trae disco.
        res = pve_get_sync("/cluster/resources")
        data = res.get("data")
        items = data if isinstance(data, list) else []
        node = next((n for n in items if n.get("type") == "node"
                     and n.get("node") == PVE_NODE), None)
        if node:
            out["pve_node"] = {"status": node.get("status"), "cpu": node.get("cpu"),
                               "memory_used": node.get("mem"), "memory_total": node.get("maxmem"),
                               "disk_used": node.get("disk"), "disk_total": node.get("maxdisk")}
        else:
            out["pve_node"] = {"error": "nodo no encontrado en cluster/resources"}
    except Exception as e:
        out["pve_node"] = {"error": str(e)[:200]}
    try:
        with urllib.request.urlopen(NOMAD_HOST + "/v1/agent/self", timeout=10) as r:
            a = json.loads(r.read().decode())
        out["nomad"] = {"status": "up", "leader": a.get("Leader"), "version": a.get("Version")}
    except Exception as e:
        out["nomad"] = {"status": "down", "error": str(e)[:200]}
    try:
        r = subprocess.run(["systemctl", "list-units", "--type=service", "--no-legend", "--no-pager"],
                           capture_output=True, text=True, timeout=15)
        gws = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].startswith("sshgw@"):
                gws.append({"unit": parts[0], "active": parts[2], "sub": parts[3]})
        out["ssh_gateways"] = gws
    except Exception as e:
        out["ssh_gateways"] = {"error": str(e)[:200]}
    with _SYSTEM_CACHE_LOCK:
        _SYSTEM_CACHE["ts"] = time.time()
        _SYSTEM_CACHE["data"] = out
    return out

async def _pve_existing_in_range() -> dict:
    """vmid(str)->kind ('lxc'/'qemu') para lo que existe en PVE.

    /cluster/resources devuelve una LISTA plana de items
    {'type':'lxc'|'qemu'|..., 'vmid':N, ...}.
    """
    out = {}
    try:
        res = await pve_get("/cluster/resources")
    except HTTPException:
        return out
    data = res.get("data")
    items = data if isinstance(data, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("lxc", "qemu") and item.get("vmid") is not None:
            out[str(item["vmid"])] = t
    return out

async def reconcile_deleted_vms() -> dict:
    """Destruye en PVE cualquier VM del servicio (6100+) que el ledger marque
    'deleted' pero que aúN exista en PVE. Devuelve {borradas: [...]}.

    IMPORTANTE: el pool es rotativo y reutiliza vmids (6100, 6101, ...).
    La tabla resources acumula filas 'deleted' viejas del MISMO vmid que hoy
    vive otra VM. Por eso 'flagged' solo contiene vmids que NO tienen una fila
    viva (status != 'deleted'): si hay una VM activa con ese vmid, NO se toca.
    (Fix: un bug anterior usaba SELECT ref_id ... status='deleted' directo, y
    en el startup destruyó VMs vivas que compartían vmid con filas viejas.)
    """
    with db() as c:
        deleted = {str(r[0]) for r in c.execute(
            "SELECT ref_id FROM resources WHERE kind='vm' AND status='deleted'")}
        active = {str(r[0]) for r in c.execute(
            "SELECT ref_id FROM resources WHERE kind='vm' AND status != 'deleted'")}
    flagged = {v for v in deleted if v not in active}
    if not flagged:
        return {"borradas": []}
    existing = await _pve_existing_in_range()
    orphans = sorted(v for v in flagged if v in existing)
    done = []
    for vmid in orphans:
        kind = existing.get(vmid)
        try:
            await _pve_ensure_gone(kind, int(vmid))
            # liberar el mapping GPU solo si el vmid ya no tiene fila viva
            # (evita liberar la GPU de una VM activa que comparte vmid con
            # una fila 'deleted' vieja del pool rotativo)
            with db() as c:
                row = c.execute(
                    "SELECT gpu_mapping FROM resources WHERE kind='vm' AND status='deleted' AND ref_id=? LIMIT 1",
                    (vmid,)).fetchone()
                has_active = c.execute(
                    "SELECT 1 FROM resources WHERE kind='vm' AND status != 'deleted' AND ref_id=? LIMIT 1",
                    (vmid,)).fetchone()
            gmap = row[0] if row and row[0] else None
            if gmap and not has_active:
                await _release_gpu(gmap)
            done.append(f"{kind}:{vmid}")
        except HTTPException:
            done.append(f"{kind}:{vmid} (FALLO)")
    return {"borradas": done}

@app.on_event("startup")
async def _startup_reconcile():
    # Al arrancar: reconciliar huérfanos (deleted en ledger, vivos en PVE).
    # Corre en background para no demorar el boot.
    async def _bg():
        try:
            r = await reconcile_deleted_vms()
            if r["borradas"]:
                print(f"[startup] reconciliación: destruidas {r['borradas']}")
        except Exception as e:
            print(f"[startup] reconciliación falló: {e}")
    asyncio.get_event_loop().create_task(_bg())

@app.post("/admin/vms/reconcile", dependencies=[Depends(admin_auth)])
async def admin_reconcile_vms():
    """Destruye en PVE las VMs del servicio que están 'deleted' en el ledger
    pero siguen existiendo en PVE. Devuelve qué se borró."""
    return await reconcile_deleted_vms()

# ─── VMs (EC2-like) ────────────────────────────────────────────────────
async def _pve_gone(kind: str, vmid: int, retries: int = 14) -> bool:
    """True si la VM desapareció de PVE; espera la tarea asíncrona.

    Una VM que no existe hace que PVE devuelva 500 con
    'Configuration file ... does not exist' (también 404/403 en algunos casos).
    """
    for _ in range(retries):
        try:
            await pve_get(f"/nodes/{PVE_NODE}/{kind}/{vmid}/status/current")
        except HTTPException as e:
            det = str(e.detail)
            if "does not exist" in det or "404" in det or "403" in det:
                return True
        await asyncio.sleep(5)
    return False

def _pve_missing(e: HTTPException) -> bool:
    """True si la excepción indica que la VM/config ya no existe en PVE."""
    det = str(e.detail)
    return "does not exist" in det or "404" in det or "403" in det

async def _pve_ensure_gone(kind: str, vmid: int) -> None:
    """Detiene y destruye la VM hasta que desaparezca de PVE."""
    # detener (ignorar si ya no existe o está detenido)
    try:
        await pve_post(f"/nodes/{PVE_NODE}/{kind}/{vmid}/status/stop", {})
        for _ in range(10):
            try:
                st = await pve_get(f"/nodes/{PVE_NODE}/{kind}/{vmid}/status/current")
            except HTTPException as e:
                if _pve_missing(e):
                    return  # ya no existe
                raise
            if (st.get("data") or {}).get("status") == "stopped":
                break
            await asyncio.sleep(3)
    except HTTPException as e:
        if _pve_missing(e):
            return  # no existe
        if "not running" not in str(e.detail).lower():
            raise
        # 'not running' = ya estaba detenida: NO retornar, continuar al delete
        # (bug 07/10: el return temprano se saltaba el destroy y la VM quedaba
        #  stopped en PVE con el ledger en 'deleted')
    # destruir (reintentar una vez si sigue corriendo)
    for attempt in range(2):
        try:
            await pve_delete(f"/nodes/{PVE_NODE}/{kind}/{vmid}")
            break
        except HTTPException as e:
            if _pve_missing(e):
                return  # ya no existe
            if attempt == 0 and "running" in str(e.detail).lower():
                await asyncio.sleep(3)
                continue
            raise
    if not await _pve_gone(kind, vmid):
        raise HTTPException(500, f"la VM {vmid} sigue existiendo en PVE tras el borrado")

async def _next_vmid(kind: str = "lxc") -> int:
    """Buscar siguiente vmid libre en 6100+ (kind: 'lxc' o 'qemu').

    Libre = sin NINGUNA fila en el ledger (viva o 'deleted') y no existe ese
    vmid en PVE. El pool es rotativo: si reutilizáramos un vmid con filas
    'deleted' viejas, la reconciliación de arranque lo confundiría con una
    huérfana y lo destruiría (bug real: destruyó VMs vivas en 01/10 23:36).
    Nota: se usa /config como chequeo de existencia porque
    GET /nodes/{node}/{kind}/{vmid} (sin /config) SIEMPRE devuelve 200
    con una lista de subdirs, incluso si la VM no existe.
    """
    with db() as c:
        used = [r["ref_id"] for r in c.execute(
            "SELECT ref_id FROM resources WHERE kind='vm'")]
    for i in range(6100, 6100+200):
        if str(i) in used:
            continue
        try:
            await pve_get(f"/nodes/{PVE_NODE}/{kind}/{i}/config")
            continue  # ya existe en PVE (de ese tipo)
        except HTTPException:
            pass
        # si es para QEMU, también verificar que no hay LXC con ese vmid
        if kind == "qemu":
            try:
                await pve_get(f"/nodes/{PVE_NODE}/lxc/{i}/config")
                continue
            except HTTPException:
                pass
        return i
    raise HTTPException(500, "no hay vmid libre")

async def _wait_task(upid: str, timeout: int = 180) -> dict:
    """Espera el UPID hasta terminarse y devuelve su estado.

    GET /nodes/{node}/tasks/{upid}/status devuelve un dict con:
      exitstatus: 'OK' | 'FAILED' | None (todavía corriendo)
      message: detalles si falló
    """
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        up_enc = urllib.parse.quote(upid, safe=":")
        try:
            j = await pve_get(f"/nodes/{PVE_NODE}/tasks/{up_enc}/status")
        except HTTPException as e:
            # PVE NO deja consultar el status de un qmconfig worker por UPID
            # ("unable to parse worker upid" → 400). NO es 404: el task existe
            # y sigue corriendo. No romper el wait aquí: seguir el loop hasta
            # el timeout (el timeout de qmconfig es ~30s en PVE).
            last = {"note": f"task status no consultable: {e.detail}"}
            await asyncio.sleep(1)
            continue
        d = j.get("data") or {}
        if not isinstance(d, dict):
            break
        es = d.get("exitstatus")
        if es == "OK":
            return d
        if es == "FAILED":
            raise HTTPException(502, f"tarea PVE falló: {d.get('message', '')[:300]}")
        await asyncio.sleep(2)
    return last


async def _wait_config(vmid: int, expected: dict, timeout: int = 90) -> dict:
    """Espera que el config EFFECTIVO de la VM muestre los campos esperados.

    PVE no deja consultar el status de un task qmconfig por UPID (400
    "unable to parse worker upid"), así que esperar por UPID en un POST
    /config es inútil: el GET responde 400 siempre y el wait se rompe o
    se duerme hasta el timeout. Lo robusto es leer la config real y
    esperar a que los campos aplicados aparezcan (el qmconfig termina
    en ~5-30s). expected: {campo: valor_o_prefixo_str}.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            resp = await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/config")
        except HTTPException:
            # la VM aún no existe/está visible (clone en curso) → seguir esperando
            await asyncio.sleep(2)
            continue
        # la API devuelve {"data": {…campos…}} — los campos van bajo "data"
        cfg = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(cfg, dict):
            await asyncio.sleep(2)
            continue
        ok = True
        for k, v in expected.items():
            actual = str(cfg.get(k, ""))
            # matching flex: si el valor esperado es substring del real, ok.
            # PVE devuelve net0 como "virtio=MAC,bridge=vmbr-cloud" (no
            # "virtio,bridge=..."), y cores/memory como int→str.
            if str(v) not in actual:
                ok = False
        if ok:
            return cfg
        await asyncio.sleep(2)
    raise HTTPException(502, f"VM {vmid}: PVE no aplicó {expected} en {timeout}s (task qmconfig revertido o colgado)")




def _lxc_startup_tmpfs_fix(ram_mb: int) -> str:
    """(OBSOLETO — no se usa.) La opción `startup` de PVE LXC NO es un
    comando: es `order=/up=/down=` (orden de arranque/parada). Por eso
    fallaba 400 y rompía la creación. Reemplazado por tmpfs-limits.service
    en el template base (ver template 9000). Se deja aquí como no-op
    documentado."""
    return ""


def _gpu_cloudinit_user_data() -> str:
    """user-data cloud-init para VMs GPU. El template base (GPU_TEMPLATE_VID)
    YA trae nvidia-driver-570 + dkms compilado para su kernel: en el 99.9%
    de los casos el modulo se carga solo y este user-data no hace nada.
    Solo si el kernel del template no coincide (upgrade de kernel sin
    reconstruir) el runcmd reconstruye el modulo dkms. Sin 'reboot: true':
    el modulo se carga con udev al detectar la GPU."""
    setup = r"""set -e
# expandir rootfs al tamaño del disco (el PVE resiza el disco al pedido,
# la particion del template sigue de 8G): no-op si ya coincide
disk_k=$(blockdev -g /dev/sda 2>/dev/null | awk '{print $1}')
part_k=$(blockdev -g /dev/sda1 2>/dev/null | awk '{print $1}')
if [ -n "$disk_k" ] && [ -n "$part_k" ] && [ "$disk_k" -gt "$part_k" ]; then
  # growpart (la trae cloud-init del template) o sfdisk viejo como fallback
  growpart /dev/sda 1 2>/dev/null || sfdisk --force --resize --append /dev/sda 2>/dev/null || true
  resize2fs /dev/sda1 2>/dev/null || true
fi
if ! lsmod | grep -q '^nvidia '; then
  for p in $(dpkg -l 2>/dev/null | awk '/^rc/ {print $2}'); do
    if dpkg -s "$p" 2>/dev/null | grep -qi nvidia; then
      dpkg --purge "$p" || true
    fi
  done
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
nvidia-driver-570 dkms linux-modules-extra-$(uname -r) || true
fi
"""
    content = "\n".join("        " + line for line in setup.strip("\n").splitlines())
    return (
        "#cloud-config\n"
        "write_files:\n"
        "  - path: /opt/nvidia-setup.sh\n"
        "    permissions: '0755'\n"
        "    content: |\n"
        + content + "\n"
        "runcmd:\n"
        "  - /opt/nvidia-setup.sh\n"
    )

async def _create_qemu_gpu_vm(vmid: int, client: dict, body: VMCreate,
                             name: str, ip: str, mapping_id: str) -> str:
    """Crea una QEMU con GPU passthrough (mapping) + cloud-init.

    Orden crítico: aplicar TODA la config con la VM detenida (PVE no aplica
    cores/memory/net0 en caliente, los apila en [PENDING]). Por eso:
      1) clone linked (VM queda detenida)
      2) POST /config con GPU, cores, ram, net0, sshkeys, cloud-init
      3) start
    """
    # 1) clone linked del template QEMU base (queda detenida)
    # full="1": linked NO funciona desde un template con disco full propio
    # (PVE: "Linked clone feature is not supported"). Full de 8GB toma ~1 min.
    clone = await pve_post(f"/nodes/{PVE_NODE}/qemu/{GPU_TEMPLATE_VID}/clone", {
        "newid": str(vmid), "full": "1", "target": PVE_NODE,
        "name": name[:15],
    })
    if isinstance(clone, dict) and clone.get("data"):
        await _wait_task(clone["data"])
    # asegurar que está detenida antes de configurar
    try:
        st = await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/current")
        if (st.get("data") or {}).get("status") == "running":
            stopr = await pve_post(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/stop", {})
            if isinstance(stopr, dict) and stopr.get("data"):
                await _wait_task(stopr["data"])
    except HTTPException:
        pass  # ya no existe

    # 1b) redimensionar el disco al tamaño pedido. El clone full trae el
    #     tamaño del template (8G); sin esto la VM queda con 8G aunque el
    #     cliente pida más (bug real 02/10: test2 pidió 25G y salió 8G).
    #     El resize de qcow2 es extender (no copia datos): rápido. PVE NO
    #     achica ("shrinking disks is not supported"); <8G se rechaza ya
    #     en create_vm (400).
    if body.disk_gb > 8:
        want_mb = int(body.disk_gb) * 1024
        got_mb = -1
        for attempt in (1, 2):
            rs = await pve_put(f"/nodes/{PVE_NODE}/qemu/{vmid}/resize",
                               {"size": f"{int(body.disk_gb)}G", "disk": "scsi0"})
            if isinstance(rs, dict) and rs.get("data"):
                await _wait_task(rs["data"], timeout=300)
            # verificar el tamaño RESULTANTE: un resize sobre disco saturado
            # puede fallar con timeout ("qemu-img resize ... got timeout") y
            # el wait termina igualmente → sin esto la VM queda running con
            # drift (bug real 03/10: 6149 pidió 21G y quedó 8.2G).
            cfg = (await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/config")).get("data") or {}
            # PVE reporta size en la unidad que usó: 8396M, 40G, 1T —
            # buscar solo "M" fallaba para ≥10G (got_mb=-1 → rollback
            # falso y VM deleted aunque el resize SÍ había aplicado;
            # bug real 03/10: VM GPU de 40G autodestruida).
            m = re.search(r"size=(\d+)([MGTP]?)", cfg.get("scsi0") or "")
            got_mb = int(m.group(1)) * {"": 1, "M": 1, "G": 1024, "T": 1024 * 1024, "P": 1024 ** 3}.get(m.group(2), 1) if m else -1
            if got_mb >= want_mb * 0.98:
                break
            if attempt == 1:
                await asyncio.sleep(5)  # dar respiro al disco tras el clone full
        if got_mb < want_mb * 0.98:
            raise HTTPException(500, f"resize a {want_mb}M falló: el disco quedó {got_mb}M")

    # 2) update config (VM detenida → se aplica completo, sin [PENDING]).
    #    IMPORTANTE (bug real 01/10): PVE NO aplica net0+ipconfig0 si el mismo
    #    POST incluye hostpci0 (el task falla parcial: aplica hostpci/cores pero
    #    deja el net0 del template → la VM bootea en vmbr0 con dhcp y SIN GPU
    #    usable). Por eso se hace en 2 pasos: 2a) red, 2b) GPU.
    ipcfg = f"ip={ip.split('/')[0]}/24,gw={CLOUD_GW}"

    # 2a) red (solo net0/ipconfig0: SÍ aplica)
    net_data = {
        # SIN firewall=1: el PVE firewall en la NIC mete el tráfico del VM en un
        # network namespace propio y el MASQUERADE del host deja de aplicarse
        # → el VM queda sin internet. Las claves SSH van al cloud-init.
        "net0": f"virtio,bridge={BRIDGE}",
        "ipconfig0": ipcfg,
        "ciuser": "root", "onboot": "0",
    }
    keys = [l for l in (client["ssh_key"] or "").splitlines() if l.strip()]
    jump = ""
    try:
        jump = open(f"/opt/sshgw/vm{vmid}.key.pub").read().strip()
    except OSError:
        pass
    if jump and jump not in keys:
        keys.append(jump)
    sp = service_key_pub()
    if sp and sp not in keys:
        keys.append(sp)
    if keys:
        net_data["sshkeys"] = urllib.parse.quote("\n".join(keys), safe="")
    cfg = await pve_post(f"/nodes/{PVE_NODE}/qemu/{vmid}/config", net_data)
    # NO usar _wait_task aquí: PVE no deja consultar el status de un task
    # qmconfig por UPID (400). Esperar el config efectivo vía _wait_config.
    await _wait_config(vmid, {"net0": f"bridge={BRIDGE}", "ipconfig0": ipcfg})

    # 2b) GPU + sizing (sin net0: PVE aplica todo).
    #    SIN ide2: el template 9000 YA trae el disco cloudinit (vm-<vmid>-
    #    cloudinit.qcow2 en el storage de discos) y re-crear ese path aborta el task
    #    ("already exists"), revirtiendo también cores/memory/hostpci0.
    gpu_data = {
        "hostpci0": f"mapping={mapping_id},rombar=0",
        "cores": str(body.cpu), "memory": str(body.ram_gb * 1024), "cpu": "host",
    }
    await pve_post(f"/nodes/{PVE_NODE}/qemu/{vmid}/config", gpu_data)
    await _wait_config(vmid, {"hostpci0": f"mapping={mapping_id}",
                              "cores": str(body.cpu),
                              "memory": str(body.ram_gb * 1024)})

    # 3a) user-data cloud-init con driver NVIDIA (VM detenida; cloud-init
    #     corre en el primer boot e reinicia). ANTES del start: PVE no
    #     aplica cloud-init en caliente.
    try:
        await pve_post(f"/nodes/{PVE_NODE}/qemu/{vmid}/config",
                       {"cloudinit": _gpu_cloudinit_user_data()})
    except HTTPException:
        pass  # defensivo: sin user-data sigue el flujo, sin driver

    # 3) arrancar
    try:
        start = await pve_post(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/start", {})
        if isinstance(start, dict) and start.get("data"):
            await _wait_task(start["data"], timeout=300)
    except HTTPException:
        pass

    # 3b) verificación defensiva: comprobar que la VM está en la red esperada
    # (ipconfig0). Si arrancó con el net0 del template (qmset quedó [PENDING]),
    # el guest aún usa dhcp/vmbr0; se detecta por su IP real vía guest-agent.
    try:
        exp_ip = ip.split("/")[0]
        ok = False
        for _ in range(12):
            try:
                netr = await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/agent/network-get-interfaces")
                ifaces = netr.get("data") or []
                if isinstance(ifaces, dict):
                    ifaces = ifaces.get("interfaces", [])
                for i in ifaces:
                    if i.get("name") == "eth0":
                        addrs = [a.get("address") for a in i.get("ipv4-addresses", [])]
                        if exp_ip in addrs:
                            ok = True
                        break
            except HTTPException:
                pass
            if ok:
                break
            await asyncio.sleep(5)
        if not ok:
            # No forzar restart: la VM GPU tiene 'reboot: true' en cloud-init y
            # puede estar reiniciando mientras instala el driver; un restart
            # ahi cortaria la instalacion a medias. Se espera a que el PVE
            # vuelva a reportar running (~5 min) y se sigue con el flujo normal.
            for _ in range(36):
                await asyncio.sleep(5)
                try:
                    st = (await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/current")).get("data") or {}
                    if st.get("status") == "running":
                        break
                except HTTPException:
                    pass
    except Exception:
        pass  # defensivo: si no se puede verificar, seguir (el arranque ya corrió)

    observed = "starting"
    for _ in range(36):
        await asyncio.sleep(5)
        try:
            st = await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/current")
            observed = (st.get("data") or {}).get("status", observed)
            if observed == "running":
                break
        except HTTPException:
            pass
    return observed

async def _sshgw_local_setup(vmid: int, host_id: str) -> None:
    """Parte LOCAL del gateway SSH en el CT (sin inyectar en la VM):
    genera la clave de salto /opt/sshgw/vm{vmid}.key, crea el usuario
    host_id no-lockeado, grupo vmssh, copia a /var/sshgw y reinicia
    el gateway. El INYECTE de la clave pública en la VM lo hace el
    llamador (inject_ssh para LXC, cloud-init sshkeys para QEMU)."""
    key_path = f"/opt/sshgw/vm{vmid}.key"
    pub_path = f"/opt/sshgw/vm{vmid}.key.pub"
    if not os.path.exists(key_path):
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path,
                        "-C", f"jump-{vmid}"], check=True, capture_output=True, text=True, timeout=30)
    subprocess.run(["bash", "-c",
                    f"getent passwd '{host_id}' >/dev/null || "
                    f"(mkdir -p /home/{host_id} && "
                    f"useradd -M -s /bin/sh -d /home/{host_id} '{host_id}' && "
                    f"chown {host_id}:{host_id} /home/{host_id})"],
                   check=True, capture_output=True, timeout=30)
    import secrets
    _pw = secrets.token_urlsafe(18)
    subprocess.run(["bash", "-c",
                    f"printf %s '{host_id}:${_pw}' | chpasswd"],
                   check=True, capture_output=True, timeout=15)
    subprocess.run(["bash", "-c", "groupadd -f vmssh"], capture_output=True, timeout=15)
    subprocess.run(["usermod", "-aG", "vmssh", host_id], capture_output=True, timeout=15)
    os.makedirs("/var/sshgw", exist_ok=True)
    subprocess.run(["cp", key_path, f"/var/sshgw/vm{vmid}.key"], check=True, capture_output=True, timeout=15)
    subprocess.run(["chmod", "600", f"/var/sshgw/vm{vmid}.key"], check=True, capture_output=True, timeout=15)
    subprocess.run(["systemctl", "restart", "ssh-gateway"], capture_output=True, timeout=30)

@app.post("/vm")
async def create_vm(client: dict = Depends(current_client), body: VMCreate = VMCreate()):
    """Crea una VM de forma ASYNC: reserva el vmid + GPU (si aplica), inserta la
    fila en el ledger con status='provisioning' y responde de inmediato. El
    trabajo real (clone, config, start, ssh) corre en background y actualiza
    el status a 'running' o 'failed' al terminar."""
    _check_vm_quota(client, body.cpu, body.ram_gb, body.disk_gb)
    is_gpu = bool(body.gpu)
    # GPU: la VM arranca del template 9100 (disco 8G con driver incluido) y
    # PVE NO permite achicar un qcow2 ("shrinking disks is not supported").
    # Menos de 8G daría drift (VM con 8G reales, ledger facturando menos).
    if is_gpu and body.disk_gb < 8:
        raise HTTPException(400, "las VMs con GPU arrancan desde un template de 8GB "
                                 "(incluye el driver) y no se puede achicar el disco: "
                                 "elegí 8GB o más")
    est = vm_hourly_cost(body.cpu, body.ram_gb * 1024, body.disk_gb, gpu=1 if is_gpu else 0)
    if _user_credits(client["id"]) < est:
        raise HTTPException(402, f"créditos insuficientes para crear una VM que cuesta "
                                  f"~{est:.2f} cr/h (saldo {_user_credits(client['id']):.2f}). Pedí un top-up al admin.")
    vmid = await _next_vmid(kind="qemu" if is_gpu else "lxc")
    name = _sanitename(body.name, f"vm-{client['id']}-{vmid}")
    ip = f"{VM_NET_PREFIX}.{vmid - VM_ID_BASE + 2}/24"
    host_id = _gen_host_id()

    # GPU: reservar AHORA (síncrono) para que el 409 se vea de inmediato.
    # Si no queda GPU libre, NO se inserta fila ni se consume vmid.
    mapping_id = None
    gpu_model = None
    if is_gpu:
        try:
            claimed = await _claim_free_gpu(body.gpu_model or "")
            mapping_id = claimed["mapping_id"]
            gpu_model = claimed["gpu"].get("model") or body.gpu_model or None
        except HTTPException:
            raise  # 409: no queda GPU; sin fila fantasma

    # Fila en provisioning (visible al instante en el front)
    with db() as c:
        c.execute("INSERT INTO resources (client_id, kind, ref_id, name, cpu, ram_mb, disk_gb, status, host_id, gpu_mapping, gpu_model) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (client["id"], "vm", str(vmid), name, body.cpu, body.ram_gb*1024, body.disk_gb,
                   "provisioning", host_id, mapping_id, gpu_model))
    # El trabajo real corre en background (no bloquea al cliente)
    asyncio.create_task(_provision_vm_bg(client["id"], vmid, name, ip, host_id, is_gpu, mapping_id, body))
    return {"name": name, "cpu": body.cpu, "ram_gb": body.ram_gb,
            "disk_gb": body.disk_gb, "ip": ip.split("/")[0], "status": "provisioning",
            "host_id": host_id, "ssh_url": None, "gpu": is_gpu,
            "gpu_model": gpu_model, "vmid": vmid}


async def _provision_vm_bg(client_id: str, vmid: int, name: str, ip: str,
                           host_id: str, is_gpu: bool, mapping_id: str, body: VMCreate):
    """Trabajo real de la creación (background). Actualiza status en el ledger:
    'provisioning' → 'running' | 'failed'."""
    import logging
    log = logging.getLogger("cloudapi.provision")
    # re-leer ssh_key del usuario (podría cambiar entre el POST y el bg task)
    with db() as c:
        row = c.execute("SELECT ssh_key FROM users WHERE username=?", (client_id,)).fetchone()
    client = {"id": client_id, "ssh_key": row[0] if row else None}
    ssh_url = None
    ok = False
    try:
        if is_gpu:
            try:
                await _sshgw_local_setup(vmid, host_id)
            except Exception:
                pass
            observed = await _create_qemu_gpu_vm(vmid, client, body, name, ip, mapping_id)
            try:
                cf_set_ssh_host(host_id, sshgw_port(vmid))
                ssh_url = f"ssh {host_id}@{SSH_DOMAIN}"
            except HTTPException:
                pass
        else:
            data = {
                "ostemplate": body.image, "vmid": vmid, "hostname": name[:15],
                "cores": body.cpu, "memory": body.ram_gb * 1024, "swap": 512,
                "rootfs": f"local-lvm:{body.disk_gb}",
                "net0": f"name=eth0,bridge={BRIDGE},ip={ip},gw={CLOUD_GW}",
                "unprivileged": "1", "onboot": "1",
            }
            await pve_post(f"/nodes/{PVE_NODE}/lxc", data)
            try:
                await pve_post(f"/nodes/{PVE_NODE}/lxc/{vmid}/status/start", {})
            except HTTPException:
                pass
            observed = "running"
            for _ in range(18):
                await asyncio.sleep(5)
                try:
                    st = await pve_get(f"/nodes/{PVE_NODE}/lxc/{vmid}/status/current")
                    cur = st.get("data", {}) or {}
                    if cur.get("status") == "running":
                        break
                    observed = cur.get("status", observed)
                except HTTPException:
                    pass
            if client["ssh_key"]:
                try:
                    await inject_ssh(vmid, client["ssh_key"])
                except HTTPException:
                    pass
            # tmpfs: systemd monta /dev/shm (50%), /run (20%), /run/user/0 (10%)
            # leyendo /proc/meminfo ANTES de que lxcfs sirva el valor del
            # container → salen sized contra la RAM del HOST (63G en una VM
            # de 2GB). Es solo techo visual (el cgroup memory.max corta la
            # RAM al limite asignado) pero queda feo y confunde. Se inyecta
            # tmpfs-limits.service (remonta /dev/shm a 50% de la RAM del
            # container en cada boot) via el worker del host, igual que la
            # key SSH (requiere el container corriendo). /run y /run/lock
            # los monta PVE desde el host y no se pueden remontar desde el
            # LXC unprivileged (write-protected).
            try:
                await inject_tmpfs_service(vmid)
            except HTTPException:
                pass  # defensivo: sin esto solo /dev/shm queda host-sized
            try:
                await sshgw_up(vmid, host_id, extra_keys=client["ssh_key"] or "")
                cf_set_ssh_host(host_id, sshgw_port(vmid))
                ssh_url = f"ssh {host_id}@{SSH_DOMAIN}"
            except HTTPException:
                pass
        ok = (observed == "running")
        final_status = "running" if ok else (observed or "failed")
    except (HTTPException, httpx.HTTPError, Exception) as e:
        # catch-all: cualquier excepción (httpx.ConnectError del worker caído,
        # etc.) debe cerrar el ciclo — si no, la fila queda 'provisioning'
        # para siempre (bug 07/10: worker cloud-inject muerto tras reboot PVE)
        detail = getattr(e, "detail", None) or f"{type(e).__name__}: {e}"
        log.error(f"provision vm {vmid} falló: {detail}")
        try:
            await pve_post(f"/nodes/{PVE_NODE}/{'qemu' if is_gpu else 'lxc'}/{vmid}/status/stop", {})
        except HTTPException:
            pass
        try:
            await pve_delete(f"/nodes/{PVE_NODE}/{'qemu' if is_gpu else 'lxc'}/{vmid}")
        except HTTPException:
            pass
        if is_gpu and mapping_id:
            await _release_gpu(mapping_id)
        final_status = "failed"
    with db() as c:
        if ok:
            c.execute("UPDATE resources SET status=?, gpu_mapping=? WHERE client_id=? AND kind='vm' AND ref_id=? AND status='provisioning'",
                      ("running", mapping_id, client_id, str(vmid)))
        else:
            # Si falló: marcar 'deleted' + liberar GPU (si era la fila que la tenía)
            c.execute("UPDATE resources SET status='deleted', gpu_mapping=NULL WHERE client_id=? AND kind='vm' AND ref_id=? AND status='provisioning'",
                      (client_id, str(vmid)))
    asyncio.create_task(_resource_event_broadcast("vm", str(vmid), final_status, client_id))
    return {"vmid": vmid, "status": final_status, "ssh_url": ssh_url if ok else None}

@app.get("/vm")
def list_vms(client: dict = Depends(current_client)):
    with db() as c:
        rows = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='vm'", (client["id"],)).fetchall()
    out = []
    for r in rows:
        out.append(dict(r))
    return out

@app.post("/vm/{vmid}/start")
async def client_vm_start(vmid: int, client: dict = Depends(current_client)):
    """Arranca una VM del cliente (mismo flujo que el admin: PVE + polling + ledger)."""
    return await _admin_vm_action(vmid, "start", client_id=client["id"])

@app.post("/vm/{vmid}/stop")
async def client_vm_stop(vmid: int, client: dict = Depends(current_client)):
    """Detiene una VM del cliente (idempotente: ya parada → devuelve el estado)."""
    return await _admin_vm_action(vmid, "stop", client_id=client["id"])

@app.delete("/vm/{vmid}")
async def delete_vm(vmid: int, client: dict = Depends(current_client)):
    with db() as c:
        r = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                      (client["id"], str(vmid))).fetchone()
    if not r:
        raise HTTPException(404, "VM no pertenece a este cliente")
    host_id = r["host_id"] if "host_id" in r.keys() else None
    gmap = r["gpu_mapping"] if "gpu_mapping" in r.keys() else None
    is_qemu = bool(gmap)
    api = "qemu" if is_qemu else "lxc"
    # destruir en PVE (detiene + destruye + verifica que desapareció).
    # Si falla, NUNCA se marca 'deleted' en el ledger (evita huérfanos).
    await _pve_ensure_gone(api, vmid)
    # liberar la GPU si era una VM con GPU (borrar el mapping PCI)
    if is_qemu:
        await _release_gpu(gmap)
    # bajar el gateway ssh de esta VM y borrar su hostname público
    try:
        sshgw_down(vmid, host_id)
        if host_id:
            cf_del_ssh_host(host_id)
    except Exception:
        pass
    with db() as c:
        c.execute("UPDATE resources SET status='deleted' WHERE client_id=? AND ref_id=? AND kind='vm'",
                  (client["id"], str(vmid)))
    return {"vmid": vmid, "status": "deleted"}

# ─── Servicios de contenedores (ECS-like) ──────────────────────────────
def _nomad_job_hcl(client_id: str, body: ServiceCreate, name: str) -> str:
    n = name.replace("-", "_")
    return f'''
job "{n}" {{
  datacenters = ["dc1"]
  type        = "service"

  group "{n}" {{
    count = {body.replicas}

    network {{
      port "http" {{
        static = {body.port}
        to     = 80
      }}
    }}

    task "app" {{
      driver = "docker"

      config {{
        image = "{body.image}"
        ports = ["http"]
        labels = {{ client = "{client_id}" }}
      }}

      resources {{
        cpu    = {body.cpu * 100}
        memory = {body.ram_mb}
      }}
    }}
  }}
}}
'''

@app.post("/service")
async def create_service(body: ServiceCreate, client: dict = Depends(current_client)):
    total_cpu = body.cpu * body.replicas
    total_ram = body.ram_mb * body.replicas
    _check_container_quota(client, total_cpu, total_ram)
    name = _sanitename(f"{client['id']}-{body.name}", f"{client['id']}-svc")
    # Nomad API: POST /v1/jobs con JSON body
    job_spec = {
        "Job": {
            "Datacenters": ["dc1"],
            "ID": name,
            "Type": "service",
            "TaskGroups": [{
                "Name": name,
                "Count": body.replicas,
                "Networks": [{
                    "DynamicPorts": [{"Label": "http", "To": 80}]
                }],
                "Tasks": [{
                    "Name": "app",
                    "Driver": "docker",
                    "Config": {
                        "image": body.image,
                        "ports": ["http"],
                        "labels": {"client": client["id"]}
                    },
                    "Resources": {"CPU": body.cpu * 100, "Memory": body.ram_mb}
                }]
            }]
        }
    }
    async with httpx.AsyncClient(verify=False, timeout=60) as c:
        r = await c.post(f"{NOMAD_HOST}/v1/jobs",
                         headers=nomad_headers(),
                         json=job_spec)
    if r.status_code >= 400:
        raise HTTPException(502, f"Nomad error {r.status_code}: {r.text[:200]}")
    data = r.json()
    job_id = data.get("JobModifyIndex", name)
    with db() as c:
        c.execute("INSERT INTO resources (client_id, kind, ref_id, name, cpu, ram_mb, status) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (client["id"], "container", name, name, total_cpu, total_ram, "running"))
    return {"job_id": name, "name": name, "cpu": total_cpu, "ram_mb": total_ram, "replicas": body.replicas}

@app.get("/service")
def list_services(client: dict = Depends(current_client)):
    with db() as c:
        rows = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='container'", (client["id"],)).fetchall()
    return [dict(r) for r in rows]

@app.delete("/service/{job_id}")
async def delete_service(job_id: str, client: dict = Depends(current_client)):
    with db() as c:
        r = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='container' AND ref_id=?",
                      (client["id"], job_id)).fetchone()
    if not r:
        raise HTTPException(404, "service no pertenece a este cliente")
    try:
        # Detener y purgar el job de Nomad
        await nomad_delete(f"/jobs/{job_id}?purge=true")
    except HTTPException as e:
        if e.status_code == 502:
            pass
    with db() as c:
        c.execute("UPDATE resources SET status='deleted' WHERE client_id=? AND ref_id=? AND kind='container'",
                  (client["id"], job_id))
    return {"job_id": job_id, "status": "deleted"}

# ─── Logs / start / stop / scale de servicios (Nomad) ───────────────────
async def _svc_row(client_id: str, job_id: str):
    """Fila del ledger del servicio, scoping por cliente (404 si no es suya)."""
    with db() as c:
        r = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='container' AND ref_id=? AND status!='deleted' LIMIT 1",
                      (client_id, job_id)).fetchone()
    if not r:
        raise HTTPException(404, "service no pertenece a este cliente")
    return r

async def _nomad_job_status(job_id: str) -> int | None:
    """Estado del job en Nomad (GET /v1/job/<id>); None si no existe (purgado)."""
    try:
        data = await nomad_get(f"/job/{job_id}")
    except HTTPException as e:
        if "404" in str(e.detail):
            return None
        raise
    return data.get("Status")

async def _nomad_set_count(job_id: str, count: int) -> dict:
    """PUT /v1/job/<id> con la spec actual y count actualizado en el grupo."""
    spec = await nomad_get(f"/job/{job_id}")
    j = spec.get("Job")
    if not j:
        raise HTTPException(502, "Nomad no devolvió la spec del job")
    groups = j.get("TaskGroups") or []
    if not groups:
        raise HTTPException(502, "job sin task groups")
    groups[0]["Count"] = count
    async with httpx.AsyncClient(verify=False, timeout=60) as c:
        r = await c.put(f"{NOMAD_HOST}/v1/job/{job_id}", headers=nomad_headers(), json={"Job": j})
    if r.status_code >= 400:
        raise HTTPException(502, f"Nomad error {r.status_code}: {r.text[:200]}")
    return r.json()

@app.get("/service/job/logs")
async def service_job_logs(job_id: str, client: dict = Depends(current_client)):
    """Últimos ~50KB de stdout por alloc del job, concatenados con prefijo del alloc."""
    await _svc_row(client["id"], job_id)
    try:
        allocs = await nomad_get(f"/job/{job_id}/allocations")
    except HTTPException:
        allocs = []
    if not isinstance(allocs, list):
        allocs = []
    parts = []
    for a in allocs:
        alloc = (a or {}).get("ID")
        if not alloc:
            continue
        try:
            log = await nomad_get(f"/alloc/{alloc}/logs/stdout")
        except HTTPException:
            continue
        txt = log if isinstance(log, str) else (log.get("Log") if isinstance(log, dict) else "")
        if not txt:
            continue
        tail = txt[-50000:]  # últimos ~50KB
        if tail != txt:
            parts.append(f"\n[{alloc}] …(truncado: mostrando los últimos 50KB)…" + tail)
        else:
            parts.append(f"\n[{alloc}] " + tail)
    return {"job_id": job_id, "logs": "".join(parts)}

@app.post("/service/job/stop")
async def service_job_stop(job_id: str, client: dict = Depends(current_client)):
    """Detiene el job en Nomad (NO purga: el job queda re-levantable con /start)
    y actualiza el ledger a 'stopped'."""
    row = await _svc_row(client["id"], job_id)
    cur = await _nomad_job_status(job_id)
    if cur is None:
        # el job ya no existe en Nomad: solo el ledger
        with db() as c:
            c.execute("UPDATE resources SET status='stopped' WHERE client_id=? AND ref_id=? AND kind='container'",
                      (client["id"], job_id))
        return {"job_id": job_id, "status": "stopped"}
    if cur == 1:  # ya está stopped en Nomad
        if row["status"] != "stopped":
            with db() as c:
                c.execute("UPDATE resources SET status='stopped' WHERE client_id=? AND ref_id=? AND kind='container'",
                          (client["id"], job_id))
        return {"job_id": job_id, "status": "stopped"}
    await nomad_post(f"/job/{job_id}/stop", {})  # sin purge: conserva el job
    for _ in range(12):
        await asyncio.sleep(2)
        s = await _nomad_job_status(job_id)
        if s in (1, 3):
            break
    with db() as c:
        c.execute("UPDATE resources SET status='stopped' WHERE client_id=? AND ref_id=? AND kind='container'",
                  (client["id"], job_id))
    return {"job_id": job_id, "status": "stopped"}

@app.post("/service/job/start")
async def service_job_start(job_id: str, client: dict = Depends(current_client)):
    """Re-leva un job detenido (Nomad conserva el job si se detuvo sin purgar).
    Si el job ya fue purgado/borrado en Nomad, devuelve 400 pidiendo recrearlo."""
    row = await _svc_row(client["id"], job_id)
    cur = await _nomad_job_status(job_id)
    if cur is None:
        raise HTTPException(400, "el job ya no existe en Nomad: recreá el servicio desde la UI")
    if cur == 2:
        if row["status"] != "running":
            with db() as c:
                c.execute("UPDATE resources SET status='running' WHERE client_id=? AND ref_id=? AND kind='container'",
                          (client["id"], job_id))
        return {"job_id": job_id, "status": "running"}
    try:
        await nomad_post(f"/job/{job_id}/start", {})
    except HTTPException as e:
        if "404" in str(e.detail):
            raise HTTPException(400, "el job ya no existe en Nomad: recreá el servicio desde la UI")
        raise
    for _ in range(15):
        await asyncio.sleep(2)
        s = await _nomad_job_status(job_id)
        if s == 2:
            break
    with db() as c:
        c.execute("UPDATE resources SET status='running' WHERE client_id=? AND ref_id=? AND kind='container'",
                  (client["id"], job_id))
    return {"job_id": job_id, "status": "running"}

class ServiceScale(BaseModel):
    replicas: int

@app.post("/service/job/scale")
async def service_job_scale(job_id: str, body: ServiceScale, client: dict = Depends(current_client)):
    """Cambia la cantidad de replicas del job en Nomad (PUT /v1/job/<id>)
    y actualiza el ledger (cpu/ram totales)."""
    if body.replicas < 1 or body.replicas > 100:
        raise HTTPException(400, "replicas debe ser entre 1 y 100")
    row = await _svc_row(client["id"], job_id)
    cur = await _nomad_job_status(job_id)
    if cur is None:
        raise HTTPException(400, "el job ya no existe en Nomad: recreá el servicio desde la UI")
    spec = await nomad_get(f"/job/{job_id}")
    cur_count = ((spec.get("Job") or {}).get("TaskGroups") or [{}])[0].get("Count", 1)
    if cur_count == body.replicas:
        return {"job_id": job_id, "replicas": body.replicas, "status": row["status"]}
    # escalando hacia arriba: chequear la quota del cliente
    if body.replicas > cur_count:
        per_cpu = int(row["cpu"] or 0) // max(cur_count, 1) or 1
        per_ram = int(row["ram_mb"] or 0) // max(cur_count, 1) or 0
        _check_container_quota(client, per_cpu * body.replicas, per_ram * body.replicas)
    await _nomad_set_count(job_id, body.replicas)
    new_status = row["status"]
    if row["status"] == "running":
        for _ in range(15):
            await asyncio.sleep(2)
            s = await _nomad_job_status(job_id)
            if s == 2:
                new_status = "running"
                break
    per_cpu = int(row["cpu"] or 0) // max(cur_count, 1) or 1
    per_ram = int(row["ram_mb"] or 0) // max(cur_count, 1) or 0
    with db() as c:
        c.execute("UPDATE resources SET cpu=?, ram_mb=?, status=? WHERE client_id=? AND ref_id=? AND kind='container'",
                  (per_cpu * body.replicas, per_ram * body.replicas, new_status, client["id"], job_id))
    return {"job_id": job_id, "replicas": body.replicas, "status": new_status}

class CreditsTopUp(BaseModel):
    amount: float
    reason: str = ""

@app.post("/admin/users/{username}/credits", dependencies=[Depends(admin_auth)])
def admin_topup_credits(username: str, body: CreditsTopUp):
    """Top-up manual de créditos (admin). Suma al saldo actual."""
    if body.amount <= 0:
        raise HTTPException(400, "amount debe ser > 0")
    with db() as c:
        r = c.execute("SELECT role, credits FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            raise HTTPException(404, "usuario no existe")
        if r["role"] != "client":
            raise HTTPException(400, "solo clientes tienen créditos")
        bal = (float(r["credits"]) if r["credits"] is not None else 0.0) + body.amount
        c.execute("UPDATE users SET credits=? WHERE username=?", (bal, username))
        c.execute("INSERT INTO credit_events (username, delta, reason) VALUES (?,?,?)",
                  (username, body.amount, f"topup {body.reason or 'admin'}"))
    return {"username": username, "credits": bal}

class CreditsSet(BaseModel):
    amount: float
    reason: str = ""

@app.put("/admin/users/{username}/credits", dependencies=[Depends(admin_auth)])
def admin_set_credits(username: str, body: CreditsSet):
    """Set absoluto del saldo de créditos (admin)."""
    if body.amount < 0:
        raise HTTPException(400, "amount debe ser >= 0")
    with db() as c:
        r = c.execute("SELECT role, credits FROM users WHERE username=?", (username,)).fetchone()
        if not r:
            raise HTTPException(404, "usuario no existe")
        if r["role"] != "client":
            raise HTTPException(400, "solo clientes tienen créditos")
        old = float(r["credits"]) if r["credits"] is not None else 0.0
        delta = body.amount - old
        c.execute("UPDATE users SET credits=? WHERE username=?", (body.amount, username))
        c.execute("INSERT INTO credit_events (username, delta, reason) VALUES (?,?,?)",
                  (username, delta, f"set_admin {body.reason or 'admin'} (a {body.amount:g})"))
    return {"username": username, "credits": body.amount}

@app.get("/admin/users/{username}/credits", dependencies=[Depends(admin_auth)])
def admin_credits(username: str):
    with db() as c:
        r = c.execute("SELECT credits FROM users WHERE username=? AND role='client'", (username,)).fetchone()
        ev = c.execute("SELECT delta, reason, created_at FROM credit_events WHERE username=? "
                       "ORDER BY id DESC LIMIT 25", (username,)).fetchall()
    if not r:
        raise HTTPException(404, "no existe el cliente")
    return {"username": username, "credits": float(r[0] or 0),
            "events": [{"delta": e["delta"], "reason": e["reason"], "created_at": e["created_at"]} for e in ev]}

@app.get("/me/credits")
def my_credits(client: dict = Depends(current_client)):
    """Saldo + tasa de consumo de las VMs running + historial."""
    uname = client["id"]
    with db() as c:
        rows = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='vm' AND status='running'",
                         (uname,)).fetchall()
        ev = c.execute("SELECT delta, reason, created_at FROM credit_events WHERE username=? "
                       "ORDER BY id DESC LIMIT 25", (uname,)).fetchall()
    running_rate = sum(vm_hourly_cost(r["cpu"], r["ram_mb"], r["disk_gb"],
                                     _gpu_units(r)) for r in rows)
    hours_left = round(_user_credits(uname) / running_rate, 2) if running_rate > 0 else None
    return {
        "credits": _user_credits(uname),
        "running_vms": len(rows),
        "cost_per_hour": round(running_rate, 4),
        "hours_left": hours_left,
        "low_balance": (hours_left is not None and hours_left < 10),
        "rates": CREDIT_RATES,
        "events": [{"delta": round(e["delta"], 4), "reason": e["reason"], "created_at": e["created_at"]} for e in ev],
    }

@app.get("/me/rates")
def my_rates(client: dict = Depends(current_client)):
    """Tasa de costo por recurso (para que el cliente calcule cuánto le costaría una VM)."""
    return {"rates_per_hour": CREDIT_RATES,
            "estimate": {"note": "créditos/hora = cpu*cpu + (ram_mb/1024)*ram_gb + disk_gb*disk_gb + n_gpus*gpu"}}

# ─── Consumo de créditos (agregado desde credit_events) ─────────────────
# reason del biller: "billing vm:<vmid> <segundos>s" (parseable por vmid).
# Otros reasons: "topup <razón>", "set_admin <razón> (a <monto>)".
_BILLING_RE = re.compile(r"^billing\s+(\w+):(\d+)")

def _credit_usage(username: Optional[str], days: int):
    """Agregado por día [{date, delta_net, events_n}] + breakdown por recurso
    (suma neta por vm:<vmid>, parseado del reason del biller)."""
    sql = ("SELECT date(created_at) d, SUM(delta) net, COUNT(*) n "
           "FROM credit_events WHERE created_at >= date('now', ?)")
    args: list = [f"-{days} days"]
    if username:
        sql += " AND username=?"
        args.append(username)
    sql += " GROUP BY d"
    with db() as c:
        day_rows = c.execute(sql, args).fetchall()
        ev_sql = "SELECT reason, delta FROM credit_events WHERE created_at >= date('now', ?)"
        ev_args = [f"-{days} days"]
        if username:
            ev_sql += " AND username=?"
            ev_args.append(username)
        ev_rows = c.execute(ev_sql, ev_args).fetchall()
    by_day = [{"date": r["d"], "delta_net": round(float(r["net"]), 4),
               "events_n": r["n"]} for r in day_rows]
    # breakdown por recurso: solo eventos de billing con vmid parseable
    by_res: dict = {}
    for e in ev_rows:
        m = _BILLING_RE.match(e["reason"] or "")
        if not m:
            continue
        key = f"vm {m.group(2)}"
        by_res[key] = round(by_res.get(key, 0.0) + float(e["delta"]), 4)
    return by_day, by_res


@app.get("/me/usage")
def my_usage(days: int = 30, client: dict = Depends(current_client)):
    """Consumo agregado por día + desglose por recurso (vmid)."""
    days = max(1, min(days, 365))
    by_day, by_res = _credit_usage(client["id"], days)
    return {"days": days, "by_day": by_day,
            "by_resource": by_res or None}


@app.get("/admin/usage", dependencies=[Depends(admin_auth)])
def admin_usage(days: int = 30):
    """Consumo global por cliente, 1 query con GROUP BY."""
    days = max(1, min(days, 365))
    with db() as c:
        rows = c.execute(
            "SELECT username, SUM(delta) net, COUNT(*) n FROM credit_events "
            "WHERE created_at >= date('now', ?) GROUP BY username "
            "ORDER BY net ASC", (f"-{days} days",)).fetchall()
    return {"days": days,
            "by_user": [{"username": r["username"], "delta_net": round(float(r["net"]), 4),
                        "events_n": r["n"]} for r in rows]}

# ─── Tokens API del cliente (self-service) ─────────────────────────────
# El cliente gestiona sus propios tokens desde la UI (Configuración → Tokens
# API). El texto del token solo se devuelve UNA vez (en la creación).
class TokenCreate(BaseModel):
    label: str = ""

class TokenLabelUpdate(BaseModel):
    label: str

def _my_tokens(client: dict):
    cur = client.get("token_hash") or ""
    with db() as c:
        rows = c.execute(
            "SELECT token_hash, label, created_at, last_used_at FROM user_tokens "
            "WHERE username=? ORDER BY created_at DESC", (client["id"],)).fetchall()
    return [{
        "id": r["token_hash"],           # el id es el hash (no el token en sí)
        "label": r["label"] or "",
        "created_at": r["created_at"],
        "last_used_at": r["last_used_at"],
        "is_current": r["token_hash"] == cur,
    } for r in rows]

@app.get("/me/tokens")
def list_my_tokens(client: dict = Depends(current_client)):
    return {"tokens": _my_tokens(client)}

@app.post("/me/tokens")
def create_my_token(body: TokenCreate, client: dict = Depends(current_client)):
    token = secrets.token_urlsafe(32)
    label = (body.label or "").strip()[:80] or f"token {time.strftime('%Y-%m-%d')}"
    with db() as c:
        c.execute("INSERT INTO user_tokens (username, token_hash, label) VALUES (?,?,?)",
                  (client["id"], _hash(token), label))
    return {"id": _hash(token), "label": label, "token": token}

@app.patch("/me/tokens/{token_id}")
def rename_my_token(token_id: str, body: TokenLabelUpdate, client: dict = Depends(current_client)):
    with db() as c:
        r = c.execute("SELECT token_hash FROM user_tokens WHERE username=? AND token_hash=?",
                      (client["id"], token_id)).fetchone()
        if not r:
            raise HTTPException(404, "token no existe")
        c.execute("UPDATE user_tokens SET label=? WHERE username=? AND token_hash=?",
                  ((body.label or "").strip()[:80], client["id"], token_id))
    return {"ok": True}

@app.delete("/me/tokens/{token_id}")
def revoke_my_token(token_id: str, client: dict = Depends(current_client)):
    cur = client.get("token_hash") or ""
    if token_id == cur:
        raise HTTPException(400, "no podés revocar el token con el que estás autenticado")
    with db() as c:
        r = c.execute("SELECT token_hash FROM user_tokens WHERE username=? AND token_hash=?",
                      (client["id"], token_id)).fetchone()
        if not r:
            raise HTTPException(404, "token no existe")
        c.execute("DELETE FROM user_tokens WHERE username=? AND token_hash=?",
                  (client["id"], token_id))
    return {"ok": True}

# ─── Claves SSH del cliente ────────────────────────────────────────────
class SSHKeysUpdate(BaseModel):
    ssh_key: str  # una o más claves públicas, una por línea

@app.put("/me/ssh-keys")
def update_ssh_keys(body: SSHKeysUpdate, client: dict = Depends(current_client)):
    key = body.ssh_key.strip()
    for line in key.splitlines():
        line = line.strip()
        if line and not (line.startswith("ssh-") or line.startswith("ecdsa-") or line.startswith("sk-")):
            raise HTTPException(400, "línea inválida: debe ser una clave pública SSH (una por línea)")
    with db() as c:
        c.execute("UPDATE users SET ssh_key=? WHERE username=?", (key, client["id"]))
    return {"ok": True, "n_keys": len([l for l in key.splitlines() if l.strip()])}

@app.get("/me/ssh-keys")
def get_ssh_keys(client: dict = Depends(current_client)):
    return {"ssh_key": client["ssh_key"] or ""}

@app.put("/vm/{vmid}/ssh-keys")
async def refresh_vm_ssh_keys(vmid: int, client: dict = Depends(current_client)):
    """Reinyecta las claves SSH del cliente en una VM existente."""
    with db() as c:
        r = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                      (client["id"], str(vmid))).fetchone()
        r2 = c.execute("SELECT ssh_key FROM users WHERE username=?", (client["id"],)).fetchone()
    if not r:
        raise HTTPException(404, "VM no pertenece a este cliente")
    if not r2 or not r2["ssh_key"]:
        raise HTTPException(400, "no tenés claves SSH cargadas")
    gmap = r["gpu_mapping"] if "gpu_mapping" in r.keys() else None
    is_qemu = bool(gmap)
    ensure_ssh_user(vmid)
    if is_qemu:
        # QEMU: la clave va al cloud-init drive vía config (requiere VM stop)
        st = (await pve_get(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/current")).get("data") or {}
        if st.get("status") == "running":
            raise HTTPException(409, "pará la VM para actualizar las claves SSH (QEMU)")
        keys = [l for l in (r2["ssh_key"] or "").splitlines() if l.strip()]
        sp = service_key_pub()
        if sp and sp not in keys:
            keys.append(sp)
        data = {}
        if keys:
            data["sshkeys"] = urllib.parse.quote("\n".join(keys), safe="")
        if data:
            await pve_post(f"/nodes/{PVE_NODE}/qemu/{vmid}/config", data)
    else:
        # ⚠ el worker REEMPLAZA authorized_keys (pct push): inyectar solo la
        # clave del usuario borra la clave de salto del gateway (jump-{vmid})
        # y el SSH via ssh.surus.lat cae a password (bug 07/10, boton
        # 'reinyectar claves'). Inyectar la UNION: usuario + salto + service.
        keys = [l for l in (r2["ssh_key"] or "").splitlines() if l.strip()]
        jump_pub = f"/opt/sshgw/vm{vmid}.key.pub"
        if os.path.exists(jump_pub):
            jp = open(jump_pub).read().strip()
            if jp and jp not in keys:
                keys.append(jp)
        try:
            await inject_ssh(vmid, "\n".join(keys))
        except HTTPException:
            raise HTTPException(502, "no se pudo inyectar la clave en el contenedor")
    return {"vmid": vmid, "ok": True}

# ─── SSH vía WebSocket (terminal en el navegador) ─────────────────────
# El cliente carga su clave PRIVADA (se usa solo para abrir la sesión SSH;
# el cliente ya inyectó la pública en la VM). El backend abre `ssh` contra
# root@IP de la VM y hace pump bidireccional con el navegador (xterm.js).

class PrivateKeyUpdate(BaseModel):
    private_key: str  # clave privada OpenSSH, con o sin passphrase

@app.put("/me/ssh-private-key")
def update_private_key(body: PrivateKeyUpdate, client: dict = Depends(current_client)):
    k = body.private_key.strip()
    if k and "PRIVATE KEY" not in k:
        raise HTTPException(400, "no parece una clave privada SSH")
    with db() as c:
        c.execute("UPDATE users SET ssh_private_key=? WHERE username=?", (k, client["id"]))
    return {"ok": True}

@app.get("/me/ssh-private-key")
def has_private_key(client: dict = Depends(current_client)):
    row = None
    with db() as c:
        row = c.execute("SELECT ssh_private_key FROM users WHERE username=?", (client["id"],)).fetchone()
    return {"has": bool(row and row["ssh_private_key"])}

@app.websocket("/ws/ssh/{vmid}")
async def ws_ssh(websocket: WebSocket, vmid: int):
    from fastapi import WebSocketDisconnect
    await websocket.accept()
    # identificar al cliente por el Bearer en el query string
    tok = websocket.query_params.get("token", "")
    with db() as c:
        row, _kind = _resolve_client_row(c, tok)
    if not row:
        await websocket.close(code=4401); return
    with db() as c:
        res = c.execute("SELECT * FROM resources WHERE client_id=? AND kind='vm' AND ref_id=? AND status!='deleted' LIMIT 1",
                        (row["username"], str(vmid))).fetchone()
        priv = c.execute("SELECT ssh_private_key FROM users WHERE username=?", (row["username"],)).fetchone()
    if not res or res["status"] == "deleted":
        await websocket.close(code=4404); return
    if not priv or not priv["ssh_private_key"]:
        await websocket.close(code=4402); return
    ip = vmIp_of(vmid)
    keyfile = f"/tmp/.sshkey_{row['id']}_{vmid}"
    with open(keyfile, "w") as f:
        f.write(priv["ssh_private_key"].rstrip() + "\n")
    os.chmod(keyfile, 0o600)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "RequestTTY=force", "-tt",
            "-i", keyfile, f"root@{ip}",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        await websocket.send_text(f"\r\n→ ssh root@{ip} (VM {vmid})")
        async def t2w():
            try:
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk: break
                    await websocket.send_text(chunk.decode(errors="replace"))
            except Exception:
                pass
        async def e2w():
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk: break
                    await websocket.send_text("[2m" + chunk.decode(errors="replace") + "[0m")
            except Exception:
                pass
        async def w2t():
            try:
                while True:
                    msg = await websocket.receive_text()
                    if msg.startswith("{") and '"cols"' in msg:
                        import fcntl, struct, termios, json
                        try:
                            r = json.loads(msg)
                            fcntl.ioctl(proc.stdin.fileno(), termios.TIOCSWINSZ,
                                        struct.pack("HHHH", r.get("rows", 24), r.get("cols", 80), 0, 0))
                        except Exception:
                            pass
                        continue
                    proc.stdin.write(msg.encode())
                    await proc.stdin.drain()
            except Exception:
                pass
        try:
            await asyncio.gather(t2w(), e2w(), w2t())
        finally:
            try: proc.kill()
            except Exception: pass
            await proc.wait()
    except Exception as e:
        try: await websocket.send_text(f"[error] {e}\n")
        except Exception: pass
    finally:
        try: os.unlink(keyfile)
        except Exception: pass
        try: await websocket.close()
        except Exception: pass

def vmIp_of(vmid: int) -> str:
    """IP actual de la VM segun la LAN del cliente dueño (subnet del cliente)."""
    with db() as c:
        r = c.execute("SELECT cl.subnet FROM resources r JOIN clients cl ON cl.id=r.client_id "
                      "WHERE r.kind='vm' AND r.ref_id=? AND r.status!='deleted' LIMIT 1",
                      (str(vmid),)).fetchone()
    return f"{VM_NET_PREFIX}.{vmid - VM_ID_BASE + 2}"

# ─── Quota propia ──────────────────────────────────────────────────────
@app.get("/quota")
def my_quota(client: dict = Depends(current_client)):
    u = _usage(client["id"])
    return {
        "client_id": client["id"],
        "quota": {"cpu": client["cpu_quota"], "ram_mb": client["ram_quota_mb"],
                  "disk_gb": client["disk_quota_gb"], "max_vms": client["max_vms"],
                  "max_containers": client["max_containers"]},
        "used": u,
        "credits": _user_credits(client["id"])
    }

@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


# ─── Tickets / reportes de bugs ────────────────────────────────────────
TICKET_STATUSES = ("open", "in_progress", "resolved", "closed")
TICKET_CATEGORIES = ("bug", "cuenta", "recursos", "facturacion", "otro")

def _migrate_tickets():
    """Tablas de reportes de bugs (ticketera). Idempotente: solo crea si no existen."""
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tickets (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL,
      title TEXT NOT NULL,
      description TEXT NOT NULL,
      category TEXT NOT NULL DEFAULT 'bug',
      status TEXT NOT NULL DEFAULT 'open',
      admin_note TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS ticket_replies (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ticket_id INTEGER NOT NULL,
      username TEXT NOT NULL,
      body TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    conn.commit()
    conn.close()

_migrate_tickets()

class TicketCreate(BaseModel):
    title: str
    description: str
    category: str = "bug"

class TicketReplyBody(BaseModel):
    body: str

class TicketUpdate(BaseModel):
    status: str | None = None
    admin_note: str | None = None

def _ticket_dict(row) -> dict:
    return dict(row)

def _load_ticket(c, ticket_id: int, owner: str | None = None) -> dict:
    """Carga un ticket con sus respuestas. owner restringe a ese usuario
    (si no es None y no coincide, None = no es suyo / no existe)."""
    q = "SELECT * FROM tickets WHERE id=?"
    if owner:
        q += " AND username=?"
        row = c.execute(q, (ticket_id, owner)).fetchone()
    else:
        row = c.execute(q, (ticket_id,)).fetchone()
    if not row:
        return None
    t = dict(row)
    t["replies"] = [
        dict(r) for r in c.execute(
            "SELECT * FROM ticket_replies WHERE ticket_id=? ORDER BY id",
            (ticket_id,)).fetchall()
    ]
    return t

# ── Cliente ──

@app.post("/tickets")
def create_ticket(body: TicketCreate, client: dict = Depends(current_client)):
    title = body.title.strip()
    description = body.description.strip()
    if not title:
        raise HTTPException(400, "title es obligatorio")
    if len(title) > 200:
        raise HTTPException(400, "title: máximo 200 caracteres")
    if not description:
        raise HTTPException(400, "description es obligatoria")
    if len(description) > 8000:
        raise HTTPException(400, "description: máximo 8000 caracteres")
    category = body.category if body.category in TICKET_CATEGORIES else "bug"
    with db() as c:
        cur = c.execute(
            "INSERT INTO tickets (username, title, description, category) VALUES (?,?,?,?)",
            (client["username"], title, description, category))
        tid = cur.lastrowid
    return {"id": tid, "status": "open", "title": title, "description": description,
            "category": category}

@app.get("/tickets")
def my_tickets(client: dict = Depends(current_client)):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM tickets WHERE username=? ORDER BY id DESC",
            (client["username"],)).fetchall()
        out = []
        for r in rows:
            t = dict(r)
            n = c.execute("SELECT COUNT(*) FROM ticket_replies WHERE ticket_id=?",
                         (t["id"],)).fetchone()[0]
            t["n_replies"] = n
            out.append(t)
    return {"tickets": out}

@app.get("/tickets/{tid}")
def get_ticket(tid: int, client: dict = Depends(current_client)):
    with db() as c:
        t = _load_ticket(c, tid, owner=client["username"])
    if not t:
        raise HTTPException(404, "ticket no encontrado")
    return t

@app.post("/tickets/{tid}/replies")
def reply_ticket(tid: int, body: TicketReplyBody, client: dict = Depends(current_client)):
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "el mensaje no puede estar vacío")
    if len(text) > 8000:
        raise HTTPException(400, "máximo 8000 caracteres")
    with db() as c:
        t = _load_ticket(c, tid, owner=client["username"])
        if not t:
            raise HTTPException(404, "ticket no encontrado")
        if t["status"] == "closed":
            raise HTTPException(400, "el ticket está cerrado; no admite respuestas")
        c.execute("INSERT INTO ticket_replies (ticket_id, username, body) VALUES (?,?,?)",
                  (tid, client["username"], text))
        c.execute("UPDATE tickets SET updated_at=datetime('now') WHERE id=?", (tid,))
    return {"ok": True}

# ── Admin ──

@app.get("/admin/tickets", dependencies=[Depends(admin_auth)])
def admin_list_tickets(status: str = "", category: str = "", username: str = ""):
    q = "SELECT * FROM tickets"
    conds, args = [], []
    if status:
        conds.append("status=?"); args.append(status)
    if category:
        conds.append("category=?"); args.append(category)
    if username:
        conds.append("username=?"); args.append(username)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC"
    with db() as c:
        rows = c.execute(q, tuple(args)).fetchall()
        out = []
        for r in rows:
            t = dict(r)
            n = c.execute("SELECT COUNT(*) FROM ticket_replies WHERE ticket_id=?",
                         (t["id"],)).fetchone()[0]
            t["n_replies"] = n
            out.append(t)
        counts = {s: 0 for s in TICKET_STATUSES}
        for r in c.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall():
            counts[r["status"]] = r[1]
    return {"tickets": out, "counts": counts}

@app.get("/admin/tickets/{tid}", dependencies=[Depends(admin_auth)])
def admin_get_ticket(tid: int):
    with db() as c:
        t = _load_ticket(c, tid)
    if not t:
        raise HTTPException(404, "ticket no encontrado")
    return t

@app.patch("/admin/tickets/{tid}", dependencies=[Depends(admin_auth)])
def admin_update_ticket(tid: int, body: TicketUpdate):
    if body.status is not None and body.status not in TICKET_STATUSES:
        raise HTTPException(400, f"status inválido (esperado: {', '.join(TICKET_STATUSES)})")
    with db() as c:
        t = _load_ticket(c, tid)
        if not t:
            raise HTTPException(404, "ticket no encontrado")
        sets, args = [], []
        if body.status is not None:
            sets.append("status=?"); args.append(body.status)
        if body.admin_note is not None:
            sets.append("admin_note=?"); args.append(body.admin_note)
        if sets:
            args.append(tid)
            c.execute(f"UPDATE tickets SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?",
                      tuple(args))
        t = _load_ticket(c, tid)
    return t

@app.post("/admin/tickets/{tid}/replies", dependencies=[Depends(admin_auth)])
def admin_reply_ticket(tid: int, body: TicketReplyBody):
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "el mensaje no puede estar vacío")
    if len(text) > 8000:
        raise HTTPException(400, "máximo 8000 caracteres")
    with db() as c:
        t = _load_ticket(c, tid)
        if not t:
            raise HTTPException(404, "ticket no encontrado")
        c.execute("INSERT INTO ticket_replies (ticket_id, username, body) VALUES (?,?,?)",
                  (tid, "admin", text))
        c.execute("UPDATE tickets SET updated_at=datetime('now') WHERE id=?", (tid,))
        t = _load_ticket(c, tid)
    return t

# ─── Arranque del facturador de créditos ─────────────────────────────────
@app.on_event("startup")
async def _start_credit_billing():
    asyncio.create_task(credit_billing_loop())
