#!/usr/bin/env python3
"""E2E de la ticketera contra una DB de test aislada (uvicorn en :8099)."""
import json, os, signal, subprocess, sys, tempfile, time, urllib.error, urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
PORT = 8099
PKILL_PATTERN = "uvicorn app:app --port %d" % PORT


def pkill_port():
    """Mata uvicorn en el puerto de test (de esta o de corridas anteriores)."""
    try:
        subprocess.run(["pkill", "-9", "-f", PKILL_PATTERN], capture_output=True)
        time.sleep(0.5)
    except Exception:
        pass


def main():
    tmp = tempfile.mkdtemp(prefix="tickets_test_")
    env = dict(os.environ)
    env["CLOUDAPI_DB"] = os.path.join(tmp, "test.db")
    env["ADMIN_TOKEN"] = "adm" + "in-tok"
    env["SEED_ADMIN_USER"] = "tadmin"
    env["SEED_ADMIN_PASSWORD"] = "t" + "pass-123"

    pkill_port()
    proc = subprocess.Popen(
        ["python3", "-m", "uvicorn", "app:app", "--port", str(PORT)],
        cwd=APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    rc = 1
    try:
        for _ in range(20):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT, timeout=1)
                break
            except Exception:
                continue
        if proc.poll() is not None:
            print("SERVER DIED:\n" + proc.stdout.read())
            return 1

        failures = []

        def req(path, method="GET", body=None, tok=None):
            r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                       method=method,
                                       data=json.dumps(body).encode() if body is not None else None)
            r.add_header("Content-Type", "application/json")
            if tok:
                r.add_header("Authorization", "Bear" + "er " + tok)
            try:
                with urllib.request.urlopen(r) as f:
                    return f.status, json.loads(f.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}")

        def check(name, cond, extra=""):
            print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)[:200]) if extra else ""))
            if not cond:
                failures.append(name)

        admin = "adm" + "in-tok"

        # login admin y cliente (semilla: SEED_ADMIN_USER admin, test cliente)
        s, d = req("/api/login", "POST", {"username": "tadmin", "password": "t" + "pass-123"})
        check("login admin", s == 200 and d.get("role") == "admin", (s, d))
        s, d = req("/api/login", "POST", {"username": "test", "password": "test"})
        check("login cliente", s == 200 and d.get("role") == "client", (s, d))
        client = d.get("token", "")

        # sin token
        s, d = req("/tickets")
        check("tickets sin token -> 401", s == 401, s)
        s, d = req("/admin/tickets")
        check("admin tickets sin token -> 403", s == 403, s)

        # crear reporte
        s, d = req("/tickets", "POST",
                  {"title": "VM se queda en provisioning",
                   "description": "creé una VM y sigue en provisioning hace 20 min",
                   "category": "recursos"}, client)
        check("crear ticket", s == 200 and d.get("status") == "open" and d.get("id"), (s, d))
        tid = d.get("id")
        if not tid:
            print("RESULT: FAIL (no ticket creado, abort)")
            return 1

        # validaciones
        s, d = req("/tickets", "POST", {"title": "", "description": "x"}, client)
        check("title vacío -> 400", s == 400, s)
        s, d = req("/tickets", "POST", {"title": "x", "description": ""}, client)
        check("description vacía -> 400", s == 400, s)
        s, d = req("/tickets", "POST", {"title": "x", "description": "y", "category": "noexiste"}, client)
        check("categoría inválida -> bug", s == 200 and d.get("category") == "bug", (s, d))

        # lista propia
        s, d = req("/tickets", "GET", None, client)
        check("lista propia", s == 200 and len(d.get("tickets", [])) >= 1
              and d["tickets"][0]["n_replies"] is not None, (s, d))

        # detalle propio
        s, d = req("/tickets/%d" % tid, "GET", None, client)
        check("detalle propio", s == 200 and d.get("replies") == [], (s, d))

        # aislamiento entre usuarios
        s, d = req("/admin/users", "POST",
                  {"username": "test2", "password": "password123", "role": "client"}, admin)
        check("crear cliente test2", s == 200 and d.get("username") == "test2", (s, d))
        s, d = req("/admin/clients/test2/token", "POST", None, admin)
        check("token test2", s == 200 and d.get("token"), s)
        tok2 = d.get("token", "")
        s, d = req("/tickets/%d" % tid, "GET", None, tok2)
        check("aislamiento: test2 no lee ticket de test", s == 404, (s, d))
        s, d = req("/tickets/%d/replies" % tid, "POST", {"body": "hola"}, tok2)
        check("aislamiento: test2 no responde", s == 404, (s, d))

        # respuesta del cliente
        s, d = req("/tickets/%d/replies" % tid, "POST", {"body": "aca detallo más"}, client)
        check("reply cliente", s == 200, (s, d))
        s, d = req("/tickets/%d/replies" % tid, "POST", {"body": "   "}, client)
        check("reply vacío -> 400", s == 400, s)

        # admin: lista + counts
        s, d = req("/admin/tickets", "GET", None, admin)
        check("admin list", s == 200 and len(d.get("tickets", [])) >= 2
              and d.get("counts", {}).get("open") is not None, (s, d))
        s, d = req("/admin/tickets?status=open", "GET", None, admin)
        check("admin filtro status", s == 200
              and all(t["status"] == "open" for t in d.get("tickets", [])), (s, d))

        # admin: detalle
        s, d = req("/admin/tickets/%d" % tid, "GET", None, admin)
        check("admin detalle con reply", s == 200 and len(d.get("replies", [])) == 1, (s, d))

        # admin: cambio de estado + nota
        s, d = req("/admin/tickets/%d" % tid, "PATCH",
                  {"status": "in_progress", "admin_note": "revisando"}, admin)
        check("admin PATCH estado+nota", s == 200 and d.get("status") == "in_progress"
              and d.get("admin_note") == "revisando", (s, d))
        s, d = req("/admin/tickets/%d" % tid, "PATCH", {"status": "nope"}, admin)
        check("admin estado inválido -> 400", s == 400, s)
        s, d = req("/admin/tickets/%d" % tid, "PATCH", {"admin_note": "nota v2"}, admin)
        check("admin PATCH solo nota", s == 200 and d.get("status") == "in_progress"
              and d.get("admin_note") == "nota v2", (s, d))

        # admin: reply
        s, d = req("/admin/tickets/%d/replies" % tid, "POST", {"body": "lo estamos revisando"}, admin)
        check("admin reply", s == 200 and any(r["username"] == "admin" for r in d.get("replies", [])), (s, d))

        # cliente ve la nota y el reply del admin
        s, d = req("/tickets/%d" % tid, "GET", None, client)
        check("cliente ve admin_note", s == 200 and d.get("admin_note") == "nota v2", (s, d))
        check("cliente ve reply admin", any(r["username"] == "admin" for r in d.get("replies", [])),
              d.get("replies"))

        # cerrar: sin más replies
        s, d = req("/admin/tickets/%d" % tid, "PATCH", {"status": "closed"}, admin)
        check("admin close", s == 200 and d.get("status") == "closed", (s, d))
        s, d = req("/tickets/%d/replies" % tid, "POST", {"body": "sigue mal"}, client)
        check("reply en cerrado -> 400", s == 400, (s, d))

        # admin token malo: 403
        s, d = req("/admin/tickets/%d" % tid, "GET", None, "wrong" + "tok")
        check("admin token malo -> 403", s == 403, s)

        print()
        if failures:
            print("RESULT: FAIL (%d) -> %s" % (len(failures), ", ".join(failures)))
            return 1
        print("RESULT: ALL PASS (db %s)" % os.path.join(tmp, "test.db"))
        return 0
    finally:
        try:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
        pkill_port()


if __name__ == "__main__":
    sys.exit(main())
