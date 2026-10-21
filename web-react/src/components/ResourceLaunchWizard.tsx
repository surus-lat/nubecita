/**
 * ResourceLaunchWizard — Asistente de Lanzamiento de Recursos.
 *
 * Tres niveles de revelación progresiva:
 *   1) Presets (Tiny / Small / Medium / GPU) — elige un punto de partida.
 *   2) Nombre / imagen / spec — ajustás los campos a tu gusto.
 *   3) <details> "Configuración avanzada" colapsado — para lo fino.
 *
 * Estimación de precio en vivo (CostEstimator, useMemo sobre `rates`).
 *
 * Validación local (el server sanitiza y hace el chequeo final):
 *   - nombre: mín 1 char (el server lo sanitiza igual que `sanitizeName`).
 *   - GPU: requiere disco >= 8GB (el template GPU trae 8GB y no se achica).
 *   - saldo insuficiente: si credits < costo, se muestra el faltante.
 *
 * Al crear: POST /vm (o /service), y se llama `onCreated(row)` con
 * {ref,name,status} para que el store inserte la fila optimista. Este
 * scaffold NO tiene store propio: App.tsx mantiene una lista de rows mock
 * "provisioning" y la recibe vía onCreated para demostrar el flujo.
 *
 * Carga de datos: Promise.allSettled sobre rates/credits/quota/gpus.
 * Una caída NO rompe el wizard: se muestra un aviso y el wizard sigue
 * usable con lo que se pudo cargar (el CostEstimator muestra "calculando…"
 * si faltan rates).
 *
 * Busy state: spinner en el botón de submit durante el POST.
 */
import { useEffect, useMemo, useState } from "react";
import {
  getRates,
  getCredits,
  getQuota,
  getGpus,
  createVM,
  createService,
  type Rates,
  type Quota,
} from "../api";
import { sanitizeName } from "../lib/sanitize";
import { CostEstimator } from "./CostEstimator";
import { Skeleton } from "./Skeleton";
import { StatusPill } from "./StatusPill";

// ─── Tipos de preset ───────────────────────────────────────────────────────
export type Preset = {
  id: string;
  label: string;
  desc: string;
  cpu: number;
  ram_gb: number;
  disk_gb: number;
  gpu: boolean;
  gpu_model: string;
};

// Presets de arranque. El preset GPU es SOLO para VM (LXC no soporta GPU);
// usa el modelo RTX A5000 por defecto (el server lo resuelve si queda libre).
export const PRESETS: Preset[] = [
  { id: "tiny", label: "Tiny", desc: "1 core · 2 GB RAM · 10 GB disco", cpu: 1, ram_gb: 2, disk_gb: 10, gpu: false, gpu_model: "" },
  { id: "small", label: "Small", desc: "2 cores · 4 GB RAM · 20 GB disco", cpu: 2, ram_gb: 4, disk_gb: 20, gpu: false, gpu_model: "" },
  { id: "medium", label: "Medium", desc: "4 cores · 16 GB RAM · 40 GB disco", cpu: 4, ram_gb: 16, disk_gb: 40, gpu: false, gpu_model: "" },
  { id: "gpu", label: "GPU", desc: "4 cores · 16 GB RAM · 40 GB disco · RTX A5000", cpu: 4, ram_gb: 16, disk_gb: 40, gpu: true, gpu_model: "RTX A5000" },
];

// Imágenes / templates de arranque (el server trae su TEMPLATE por defecto).
const VM_IMAGES = [
  "Ubuntu 24.04 (default)",
  "Debian 12",
  "Alpine",
];
const SVC_IMAGES = [
  "nginx:alpine",
  "node:20-alpine",
  "python:3.12-slim",
];

export type Row = {
  ref: string; // vmid (string) o job_id
  name: string;
  status: string; // 'provisioning' | 'running' | ...
};

export type ResourceLaunchWizardProps = {
  /** Callback para que el store inserte la fila optimista tras el POST. */
  onCreated: (row: Row) => void;
  /** Opcional: saldo inicial para el CostEstimator si no se quiere cargar /me/credits. */
  initialCredits?: number;
};

type LoadState = {
  loading: boolean;
  rates: Rates | undefined;
  credits: number | undefined;
  quota: Quota | undefined;
  gpus: string[];
  // mensajes de aviso por endpoint caído (una caída no rompe el wizard)
  errors: string[];
};

type Kind = "vm" | "service";

export function ResourceLaunchWizard({ onCreated, initialCredits }: ResourceLaunchWizardProps) {
  // ── Estado de carga de datos ──────────────────────────────────────────
  const [load, setLoad] = useState<LoadState>({
    loading: true,
    rates: undefined,
    credits: initialCredits,
    quota: undefined,
    gpus: [],
    errors: [],
  });

  // ── Estado del wizard ──────────────────────────────────────────────────
  const [kind, setKind] = useState<Kind>("vm");
  const [presetId, setPresetId] = useState<string>("tiny");
  const [name, setName] = useState("");
  const [image, setImage] = useState(VM_IMAGES[0]);
  const [cpu, setCpu] = useState(PRESETS[0].cpu);
  const [ramGb, setRamGb] = useState(PRESETS[0].ram_gb);
  const [diskGb, setDiskGb] = useState(PRESETS[0].disk_gb);
  const [gpu, setGpu] = useState(false);
  const [gpuModel, setGpuModel] = useState("");
  // service-only
  const [ramMb, setRamMb] = useState(256);
  const [replicas, setReplicas] = useState(1);

  // ── Estado de envío ─────────────────────────────────────────────────────
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // ── Carga inicial (Promise.allSettled: una caída no rompe el wizard) ────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [r, c, q, g] = await Promise.allSettled([
        getRates(),
        getCredits(),
        getQuota(),
        getGpus(),
      ]);
      if (cancelled) return;
      const errors: string[] = [];
      if (r.status === "fulfilled") {
        // /me/rates devuelve {rates_per_hour, estimate} en el backend real,
        // pero el contrato acordado para este scaffold es {rates,defaults,labels}.
        const data = r.value as unknown as {
          rates?: Rates;
          rates_per_hour?: Rates;
        };
        const rates = data.rates ?? data.rates_per_hour;
        if (rates) {
          setLoad((s) => ({ ...s, rates }));
        } else {
          errors.push("no llegaron las tasas de costo");
        }
      } else {
        errors.push("no pude cargar las tasas");
      }
      if (c.status === "fulfilled") {
        setLoad((s) => ({ ...s, credits: c.value.credits }));
      } else {
        errors.push("no pude cargar tu saldo");
      }
      if (q.status === "fulfilled") {
        setLoad((s) => ({ ...s, quota: q.value.quota }));
      } else {
        errors.push("no pude cargar tu cuota");
      }
      if (g.status === "fulfilled") {
        setLoad((s) => ({ ...s, gpus: g.value }));
      } else {
        errors.push("no pude listar las GPUs disponibles");
      }
      setLoad((s) => ({ ...s, loading: false, errors }));
    })();
    return () => {
      cancelled = true;
    };
  }, [initialCredits]);

  // ── Aplica un preset: ajusta todos los campos de una vez ───────────────
  const applyPreset = (p: Preset) => {
    setPresetId(p.id);
    setCpu(p.cpu);
    setRamGb(p.ram_gb);
    setDiskGb(p.disk_gb);
    setGpu(p.gpu);
    setGpuModel(p.gpu_model);
    // si pasamos a GPU y el tipo es service, forzamos a VM (GPU es solo VM)
    if (p.gpu && kind === "service") setKind("vm");
  };

  const switchKind = (k: Kind) => {
    setKind(k);
    setSubmitError(null);
    // reset de campos específicos según tipo
    if (k === "service") {
      setGpu(false);
      setGpuModel("");
      setImage(SVC_IMAGES[0]);
    } else {
      setImage(VM_IMAGES[0]);
    }
  };

  // ── Nombre saneado (espejo del server) ────────────────────────────────
  const sanitizedName = useMemo(
    () => sanitizeName(name, "vm-nueva"),
    [name],
  );

  // ── Validación local ────────────────────────────────────────────────────
  const validation = useMemo(() => {
    const problems: string[] = [];
    const credits = load.credits;
    let cost = 0;
    if (load.rates) {
      cost =
        kind === "vm"
          ? cpu * load.rates.cpu +
            ramGb * load.rates.ram_gb +
            diskGb * load.rates.disk_gb +
            (gpu ? load.rates.gpu : 0)
          : (cpu * load.rates.cpu + (ramMb / 1024) * load.rates.ram_gb) * replicas;
    }
    if (kind === "vm") {
      if (gpu && diskGb < 8) {
        problems.push(
          "las VMs con GPU arrancan desde un template de 8GB (trae el driver) y no se puede achicar el disco: elegí 8GB o más",
        );
      }
    } else {
      if (replicas < 1) problems.push("los replicas deben ser ≥ 1");
      if (cpu < 1) problems.push("los CPU deben ser ≥ 1");
      if (ramMb < 128) problems.push("la RAM debe ser ≥ 128 MB");
    }
    if (cpu < 1) problems.push("los CPU deben ser ≥ 1");
    if (kind === "vm" && ramGb < 1) problems.push("la RAM debe ser ≥ 1 GB");
    if (kind === "vm" && diskGb < 1) problems.push("el disco debe ser ≥ 1 GB");
    // saldo insuficiente
    if (credits !== undefined && cost > 0 && credits < cost) {
      problems.push(
        `créditos insuficientes: el recurso cuesta ≈ ${cost.toFixed(4)} cr/h y tenés ${credits.toFixed(2)} (faltan ≈ ${(cost - credits).toFixed(4)} cr)`,
      );
    }
    return { problems, cost };
  }, [kind, cpu, ramGb, diskGb, gpu, gpuModel, ramMb, replicas, load.rates, load.credits]);

  const valid = validation.problems.length === 0;

  // ── Envío ────────────────────────────────────────────────────────────────
  const submit = async () => {
    if (busy || !valid) return;
    setBusy(true);
    setSubmitError(null);
    try {
      if (kind === "vm") {
        const body = {
          name: name.trim(), // el server sanitiza igual que sanitizeName
          cpu,
          ram_gb: ramGb,
          disk_gb: diskGb,
          gpu,
          gpu_model: gpu ? gpuModel : "",
        };
        const res = await createVM(body);
        // Comportamiento del store: insertamos la fila optimista con el
        // status 'provisioning' que devuelve el server. El estado final
        // ('running' | 'failed') llega después por WebSocket — este
        // scaffold no se conecta al WS, así que la fila se queda
        // "provisioning" hasta que App.tsx la actualice o la descarte.
        onCreated({ ref: String(res.vmid), name: res.name, status: res.status });
      } else {
        const body = {
          name: name.trim(),
          image,
          cpu,
          ram_mb: ramMb,
          replicas,
        };
        const res = await createService(body);
        onCreated({ ref: res.job_id, name: res.name, status: res.status });
      }
      // reset mínimo: nombre + preset de origen
      setName("");
      setSubmitError(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "algo falló al crear el recurso";
      setSubmitError(msg);
    } finally {
      setBusy(false);
    }
  };

  const gpuOptions = useMemo(() => {
    // Si el server devuelve modelos concretos los usamos; si no, el que
    // el usuario escribió a mano + el default del preset.
    const set = new Set<string>();
    set.add(gpuModel);
    for (const g of load.gpus) set.add(g);
    return Array.from(set).filter(Boolean);
  }, [load.gpus, gpuModel]);

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <div className="panel">
      <h2>
        {kind === "vm" ? "Lanzar una VM" : "Lanzar un servicio"}
        {kind === "vm" ? "" : " (Nomad)"}
      </h2>

      {/* Selector de tipo: VM o servicio */}
      <div className="cost" style={{ marginBottom: 4 }}>
        <button
          className={kind === "vm" ? "" : "ghost"}
          onClick={() => switchKind("vm")}
          type="button"
        >
          VM
        </button>
        <button
          className={kind === "service" ? "" : "ghost"}
          onClick={() => switchKind("service")}
          type="button"
        >
          Servicio
        </button>
      </div>

      {/* ── Nivel 1: Presets ─────────────────────────────────────────────── */}
      <div className="wizard-step active">
        <h2>
          <span className="stepnum">1</span> Elegí un punto de partida
        </h2>
        {load.loading ? (
          <Skeleton height={56} />
        ) : (
          <div className="presets">
            {PRESETS.map((p) => {
              const disabled = kind === "service" && p.gpu; // GPU solo VM
              return (
                <button
                  key={p.id}
                  type="button"
                  className={`preset ${presetId === p.id ? "active" : ""}`}
                  disabled={disabled}
                  title={disabled ? "GPU solo para VMs" : undefined}
                  onClick={() => applyPreset(p)}
                >
                  <div className="name">{p.label}</div>
                  <div className="specs">{p.desc}</div>
                </button>
              );
            })}
          </div>
        )}
        {load.errors.length > 0 && (
          <div className="notice warn">{load.errors.join(" · ")}</div>
        )}
      </div>

      {/* ── Nivel 2: Nombre / imagen / spec ──────────────────────────────── */}
      <div className="wizard-step active">
        <h2>
          <span className="stepnum">2</span> Nombre, imagen y spec
        </h2>

        <label>
          Nombre
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="mi-app"
            maxLength={30}
          />
          {name.trim() !== "" && (
            <span className="muted small">
              se va a lanzar como <strong>{sanitizedName}</strong>
            </span>
          )}
        </label>

        <label>
          Imagen / template
          <select value={image} onChange={(e) => setImage(e.target.value)}>
            {(kind === "vm" ? VM_IMAGES : SVC_IMAGES).map((img) => (
              <option key={img} value={img}>
                {img}
              </option>
            ))}
          </select>
        </label>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <div style={{ flex: "1 1 120px" }}>
            <label>CPU (cores)</label>
            <input
              type="number"
              min={1}
              value={cpu}
              onChange={(e) => setCpu(Math.max(1, Number(e.target.value) || 1))}
            />
          </div>
          {kind === "vm" ? (
            <div style={{ flex: "1 1 120px" }}>
              <label>RAM (GB)</label>
              <input
                type="number"
                min={1}
                value={ramGb}
                onChange={(e) => setRamGb(Math.max(1, Number(e.target.value) || 1))}
              />
            </div>
          ) : (
            <div style={{ flex: "1 1 120px" }}>
              <label>RAM (MB)</label>
              <input
                type="number"
                min={128}
                value={ramMb}
                onChange={(e) =>
                  setRamMb(Math.max(128, Number(e.target.value) || 128))
                }
              />
            </div>
          )}
          {kind === "vm" ? (
            <div style={{ flex: "1 1 120px" }}>
              <label>Disco (GB)</label>
              <input
                type="number"
                min={1}
                value={diskGb}
                onChange={(e) => setDiskGb(Math.max(1, Number(e.target.value) || 1))}
              />
            </div>
          ) : (
            <div style={{ flex: "1 1 120px" }}>
              <label>Replicas</label>
              <input
                type="number"
                min={1}
                value={replicas}
                onChange={(e) =>
                  setReplicas(Math.max(1, Number(e.target.value) || 1))
                }
              />
            </div>
          )}
        </div>

        {/* GPU: solo en VM */}
        {kind === "vm" && (
          <>
            <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                checked={gpu}
                onChange={(e) => setGpu(e.target.checked)}
                style={{ width: "auto" }}
              />
              Con GPU passthrough (QEMU)
            </label>
            {gpu && (
              <label>
                Modelo de GPU
                <select
                  value={gpuModel}
                  onChange={(e) => setGpuModel(e.target.value)}
                >
                  {gpuOptions.map((m) => (
                    <option key={m} value={m}>
                      {m || "cualquiera de las libres"}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </>
        )}
      </div>

      {/* ── Nivel 3: Configuración avanzada (colapsado) ─────────────────── */}
      <div className="wizard-step">
        <details className="advanced">
          <summary>Configuración avanzada</summary>
          <p className="muted small">
            El server resuelve los detalles (vmid, IP, host, mapping de GPU).
            Acá quedan notas para quien quiera revisar antes de lanzar.
          </p>
          {load.loading ? (
            <Skeleton height={32} />
          ) : load.quota ? (
            <div className="small">
              <div>
                cuota: {load.quota.cpu} cores · {load.quota.ram_mb / 1024} GB RAM ·{" "}
                {load.quota.disk_gb} GB disco · {load.quota.max_vms} VMs /{" "}
                {load.quota.max_containers} cont.
              </div>
              <div>
                saldo: {load.credits?.toFixed(2) ?? "—"} cr
              </div>
              {load.rates && (
                <div>
                  tasas: cpu {load.rates.cpu} · ram_gb {load.rates.ram_gb} · disk_gb{" "}
                  {load.rates.disk_gb} · gpu {load.rates.gpu} cr/h
                </div>
              )}
            </div>
          ) : (
            <div className="muted small">sin cuota disponible</div>
          )}
        </details>
      </div>

      {/* ── Estimación + validación + submit ─────────────────────────────── */}
      <div className="panel" style={{ background: "#02181d" }}>
        <CostEstimator
          kind={kind}
          rates={load.rates}
          cpu={cpu}
          ram_gb={kind === "vm" ? ramGb : undefined}
          ram_mb={kind === "service" ? ramMb : undefined}
          disk_gb={kind === "vm" ? diskGb : undefined}
          gpu={kind === "vm" ? gpu : undefined}
          replicas={kind === "service" ? replicas : undefined}
          credits={load.credits}
        />
      </div>

      {submitError && (
        <div className="notice error">{submitError}</div>
      )}
      {!valid && !busy && (
        <div className="notice warn">
          {validation.problems.map((p, i) => (
            <div key={i}>{p}</div>
          ))}
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 8 }}>
        <button onClick={submit} disabled={busy || !valid} type="button">
          {busy && <span className="spinner" aria-hidden />}
          {busy ? "Lanzando…" : "Lanzar recurso"}
        </button>
        <span className="muted small">
          {kind === "vm" ? "responde en ~2s con status 'provisioning'" : "crea el job en Nomad"}
        </span>
      </div>

      {/* Demo: una fila optimista "provisioning" para mostrar el flujo */}
      <div style={{ marginTop: 14 }}>
        <div className="muted small" style={{ marginBottom: 6 }}>
          Demo de fila optimista (store no incluido en el scaffold)
        </div>
        <div className="row optimistic">
          <span className="ref">vm: 6101</span>
          <span className="name">{sanitizedName}</span>
          <StatusPill status="provisioning" />
        </div>
      </div>
    </div>
  );
}

export default ResourceLaunchWizard;
