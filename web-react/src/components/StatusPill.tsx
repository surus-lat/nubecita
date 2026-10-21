/**
 * StatusPill — chip de estado de un recurso.
 *
 * Colores (mismos que web/index.html):
 *   running       → green  #3ecf7a
 *   provisioning  → cyan   #00A2B0 (animado: dot pulse)
 *   stopped       → amber  #f0b34c
 *   deleted       → red    #ff6161
 *   (absente/ojo desconocido) → red, con el texto como está
 */
export type ResourceStatus =
  | "running"
  | "provisioning"
  | "stopped"
  | "deleted"
  | "failed"
  | string;

const LABEL: Record<string, string> = {
  running: "corriendo",
  provisioning: "provisionando",
  stopped: "detenida",
  deleted: "borrada",
  failed: "falló",
};

export function StatusPill({ status }: { status: ResourceStatus }) {
  const key = status.toLowerCase();
  // 'absent' (fila que ya no existe) se trata como deleted
  const cls =
    key === "absent" ? "deleted" : key === "failed" ? "deleted" : key;
  const text = LABEL[key] || key;
  return (
    <span className={`pill ${cls}`}>
      <span className="dot" />
      {text}
    </span>
  );
}

export default StatusPill;
