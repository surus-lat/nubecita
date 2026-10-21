#!/usr/bin/env python3
"""CI checks para cloudapi (nubecita). Corre en el worktree del commit a deployar.

Usage: python3 ci/check.py <worktree_dir>
Sale con codigo 0 si todo pasa, 1 si algo falla (el hook no deploya).

Checks:
  1) py_compile de app.py y worker/inject_worker.py
  2) node --check del script principal de web/index.html (el SPA es un solo
     <script> inline; un SyntaxError ahi rompe toda la UI - ver skill).
  3) bash -n de los scripts del gateway SSH (gateway/): un bug ahi rompe
     el SSH de TODOS los clientes.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    wt = sys.argv[1] if len(sys.argv) > 1 else "."
    failures = []

    # 1) python compile
    for rel in ("app.py", os.path.join("worker", "inject_worker.py")):
        path = os.path.join(wt, rel)
        if not os.path.exists(path):
            continue
        r = subprocess.run([sys.executable, "-m", "py_compile", path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(f"py_compile {rel}: {r.stderr.strip()[:300]}")

    # 2) SPA node --check
    html = os.path.join(wt, "web", "index.html")
    if os.path.exists(html):
        text = open(html, encoding="utf-8").read()
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                             text, re.S)
        body = max(scripts, key=len) if scripts else ""
        node = shutil.which("node") or "node"
        if body.strip():
            fd, js = tempfile.mkstemp(suffix=".js")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                nr = subprocess.run([node, "--check", js],
                                    capture_output=True, text=True)
                if nr.returncode != 0:
                    failures.append(f"node --check SPA: {nr.stderr.strip()[:300]}")
            finally:
                os.unlink(js)
        else:
            failures.append("SPA: no se encontro script inline en web/index.html")
    else:
        failures.append("falta web/index.html")

    # 3) gateway SSH scripts (bash -n)
    gwd = os.path.join(wt, "gateway")
    if os.path.isdir(gwd):
        for fn in sorted(os.listdir(gwd)):
            p = os.path.join(gwd, fn)
            if not os.path.isfile(p):
                continue
            r = subprocess.run(["bash", "-n", p], capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(f"bash -n gateway/{fn}: {r.stderr.strip()[:300]}")

    if failures:
        print("CI FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("CI OK: app.py, worker e SPA validos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
