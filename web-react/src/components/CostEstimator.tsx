/**
 * CostEstimator — estimación de precio en vivo (créditos/hora) + "horas que
 * te quedan con tu saldo" si arrancas este recurso ahora.
 *
 * Fórmula (espeja a `vm_hourly_cost` del backend, app.py l.150):
 *   VM   : cpu*rate_cpu + ram_gb*rate_ram + disk_gb*rate_disk + (gpu ? rate_gpu : 0)
 *   SVC  : (cpu*rate_cpu + ram_mb/1024*rate_ram) * replicas
 *
 * `credits` es el saldo actual del cliente (de /me/credits). Si `cost` > 0,
 * horas restantes = credits / cost. Si credits < cost, el wizard ya lo marca
 * como "saldo insuficiente" (validación local en ResourceLaunchWizard).
 */
import { useMemo } from "react";
import type { Rates } from "../api";

export type CostKind = "vm" | "service";

export type CostEstimatorProps = {
  kind: CostKind;
  rates?: Rates;
  cpu: number;
  ram_gb?: number; // para VM
  ram_mb?: number; // para service
  disk_gb?: number; // para VM
  gpu?: boolean;
  replicas?: number; // para service
  credits?: number;
};

function fmt(n: number, digits = 3): string {
  return n.toLocaleString("es-AR", {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });
}

export function computeCostPerHour(
  kind: CostKind,
  rates: Rates | undefined,
  p: {
    cpu: number;
    ram_gb?: number;
    ram_mb?: number;
    disk_gb?: number;
    gpu?: boolean;
    replicas?: number;
  },
): number {
  if (!rates) return 0;
  if (kind === "vm") {
    const cpu = p.cpu || 0;
    const ram = p.ram_gb || 0;
    const disk = p.disk_gb || 0;
    const gpu = p.gpu ? 1 : 0;
    return (
      cpu * rates.cpu +
      ram * rates.ram_gb +
      disk * rates.disk_gb +
      gpu * rates.gpu
    );
  }
  const cpu = p.cpu || 0;
  const ram = p.ram_mb || 0;
  const reps = p.replicas || 1;
  return (cpu * rates.cpu + (ram / 1024) * rates.ram_gb) * reps;
}

export function CostEstimator({
  kind,
  rates,
  cpu,
  ram_gb,
  ram_mb,
  disk_gb,
  gpu,
  replicas,
  credits,
}: CostEstimatorProps) {
  // useMemo: se recalcula solo cuando cambia alguno de los insumos.
  const cost = useMemo(
    () =>
      computeCostPerHour(kind, rates, {
        cpu,
        ram_gb,
        ram_mb,
        disk_gb,
        gpu,
        replicas,
      }),
    [kind, rates, cpu, ram_gb, ram_mb, disk_gb, gpu, replicas],
  );
  const hoursLeft = useMemo(() => {
    if (credits === undefined || credits === null) return null;
    if (cost <= 0) return null;
    return credits / cost;
  }, [credits, cost]);
  const insufficient =
    credits !== undefined && credits !== null && credits < cost && cost > 0;

  if (!rates) {
    return <span className="muted small">calculando…</span>;
  }
  return (
    <div className="cost">
      <div className="price">
        {fmt(cost, 4)} <span className="unit">cr/h</span>
      </div>
      {hoursLeft !== null && (
        <div className="line">
          con tu saldo te quedan ≈ <strong>{fmt(hoursLeft, 1)}</strong> horas
        </div>
      )}
      {insufficient && (
        <div className="line bad">
          saldo insuficiente: faltan ≈ {fmt(cost - (credits ?? 0), 4)} cr
        </div>
      )}
    </div>
  );
}

export default CostEstimator;
