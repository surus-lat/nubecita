/**
 * sanitizeName — espejo del `_sanitename` del backend (app.py, l.162).
 *
 * El server sanitiza el nombre de igual forma antes de escribirlo en PVE
 * (DNS name: minúsculas, [a-z0-9-], sin espacios); este helper replica la
 * lógica para que la UI muestre "el nombre que va a salir" antes del POST.
 *
 * Reglas (igual al backend):
 *   1) lowercase + trim
 *   2) reemplazar `[^a-z0-9-]` por `-`
 *   3) colapsar guiones consecutivos (`--` → `-`) y cortar guiones en bordes
 *   4) slice(0, 20) (límite de PVE)
 *   5) si queda vacío → usar fallback (el server usa `vm-<client>-<vmid>`;
 *      la UI no conoce el vmid aún, así que pasa uno genérico)
 */
export function sanitizeName(name: string, fallback = "vm-nueva"): string {
  let n = (name ?? "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]/g, "-");
  n = n.replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "");
  n = n.slice(0, 20);
  if (!n) {
    let f = (fallback ?? "vm-nueva")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, "-");
    f = f.replace(/-{2,}/g, "-").replace(/^-+|-+$/g, "").slice(0, 20);
    n = f || "vm-nueva";
  }
  return n;
}
