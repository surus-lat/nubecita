#!/usr/bin/env python3
"""Tests unitarios del HarvesterProvider con transporte mock (sin nodo real).
Corre con DB aislada: CLOUDAPI_DB=tmp. Valida shapes de request y mapping de
status. El e2e real contra el nodo queda para S2 (plan §F2).

Uso: cd <repo> && python3 test_harvester.py
"""
import asyncio, importlib.util, json, os, sys, tempfile, types

os.environ.setdefault("CLOUDAPI_DB", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("HARVESTER_TOKEN_VAL", "test-token")

spec = importlib.util.spec_from_file_location("app_mod", os.path.join(os.path.dirname(__file__), "app.py"))
app_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_mod)

NODE = {"id": "hv-1", "kind": "harvester",
        "endpoint": "https://hv.example:443", "token_ref": "HARVESTER_TOKEN_VAL",
        "status": "online"}
prov = app_mod.HarvesterProvider(NODE)

calls = []

class FakeResp:
    def __init__(self, code, payload):
        self.status_code = code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.text = json.dumps(payload)
    def json(self):
        return self._payload

class FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def request(self, method, url, headers=None, json=None):
        calls.append((method, url.replace(prov.base, ""), json))
        p = url.replace(prov.base, "")
        if p.startswith("/api/v1/namespaces/test") and method == "GET":
            return FakeResp(404, {"reason": "not found"})
        if p == "/api/v1/namespaces" and method == "POST":
            return FakeResp(201, {})
        if "virtualmachineimages" in p:
            return FakeResp(200, {"items": [{"metadata": {"name": "ubuntu-24.04"},
                                             "status": {"progress": 100}}]})
        if "persistentvolumeclaims" in p or "secrets" in p:
            return FakeResp(201, {})
        if p.endswith("/virtualmachines") and method == "POST":
            return FakeResp(201, {})
        if "/virtualmachines/vm-6500/start" in p:
            return FakeResp(200, {"status": {"printableStatus": "Running"}})
        if "/virtualmachines/vm-6500/stop" in p:
            return FakeResp(200, {"status": {"printableStatus": "Stopped"}})
        if p.endswith("/virtualmachines/vm-6500") and method == "GET":
            return FakeResp(200, {"status": {"printableStatus": "Running",
                                             "conditions": [{"type": "Ready", "status": "True"}]}})
        if p.endswith("/virtualmachines/vm-6500") and method == "DELETE":
            return FakeResp(200, {})
        if "/api/v1/nodes" in p and method == "GET":
            return FakeResp(200, {"items": [{
                "metadata": {"name": "hv-node1"},
                "status": {"capacity": {"cpu": "16", "memory": "64Gi",
                                        "nvidia.com/geforce-rtx-2080-ti": "2"},
                           "allocatable": {"cpu": "15", "memory": "63Gi",
                                           "nvidia.com/geforce-rtx-2080-ti": "2"}}}]})
        if "persistentvolumeclaims/rootdisk-6500" in p or "secrets/cloudinit-6500" in p:
            return FakeResp(200, {})
        return FakeResp(404, {"reason": f"unmocked {method} {p}"})

app_mod.httpx.AsyncClient = FakeClient

async def main():
    global calls
    # 1) create: ns + imagen + pvc + secret + VM, en orden y con shapes correctas
    calls = []
    await prov.create_vm("test", 6500, "mv", 4, 8192, 40, ["ssh-ed25519 AAAA... jump"])
    paths = [c[1] for c in calls]
    assert "/api/v1/namespaces" in paths, "namespace no creado"
    assert any("persistentvolumeclaims" in p for p in paths), "PVC no creado"
    assert any("secrets" in p for p in paths), "cloudinit no creado"
    vmcall = next(c for c in calls if c[1].endswith("/virtualmachines"))
    body = vmcall[2]
    assert body["apiVersion"] == "kubevirt.io/v1"
    assert body["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] == 4
    assert body["spec"]["template"]["spec"]["domain"]["memory"]["guest"] == "8192Mi"
    vols = body["spec"]["template"]["spec"]["volumes"]
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "rootdisk-6500" for v in vols)
    assert any(v.get("cloudInitConfigMap", {}).get("name") == "cloudinit-6500" for v in vols)
    pvc = next(c for c in calls if "persistentvolumeclaims" in c[1])[2]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "40Gi"
    print("OK create: ns+pvc+cloudinit+VM con spec KubeVirt valida")

    # 2) status mapping
    st = await prov.vm_status(6500, client_id="test")
    assert st == "running", st
    print("OK vm_status running")

    # 3) start/stop usan subresources
    calls = []
    await prov.start("test", 6500); await prov.stop("test", 6500)
    assert calls[0][0] == "PUT" and calls[0][1].endswith("/start")
    assert calls[1][0] == "PUT" and calls[1][1].endswith("/stop")
    print("OK start/stop subresources")

    # 4) delete purga VM + PVC + secret
    calls = []
    await prov.delete("test", 6500)
    dels = [c[1] for c in calls if c[0] == "DELETE"]
    assert any("/virtualmachines/vm-6500" in p for p in dels)
    assert any("rootdisk-6500" in p for p in dels)
    assert any("cloudinit-6500" in p for p in dels)
    print("OK delete purga VM+PVC+secret")

    # 5) node_usage y gpu_inventory
    u = await prov.node_usage()
    assert u["cpu"] == 16 and u["ram_mb"] == 64 * 1024 and u["gpus"] == 2, u
    g = await prov.gpu_inventory()
    assert len(g) == 2 and "8080" not in str(g) and g[0]["model"], g
    assert "2080" in g[0]["model"] or "Ti" in g[0]["model"], g[0]["model"]
    print(f"OK node_usage {u} y gpu_inventory model={g[0]['model']!r}")

    # 6) sin token -> 503 claro
    p2 = app_mod.HarvesterProvider({"id": "hv-x", "endpoint": "https://x", "token_ref": "NOPE_MISSING"})
    try:
        await p2.node_usage()
        raise SystemExit("debia fallar sin token")
    except app_mod.HTTPException as e:
        assert e.status_code == 503
    print("OK guard de token ausente (503)")

    print("\nTODO OK")

asyncio.run(main())
