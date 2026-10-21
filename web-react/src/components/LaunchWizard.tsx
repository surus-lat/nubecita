/**
 * LaunchWizard — Asistente de Lanzamiento de Recursos (nubecita)
 * React 18 + TypeScript + TanStack Query (cache, retry, invalidación).
 *
 * Patrones demostrados:
 *  1. REVELACIÓN PROGRESIVA en 3 capas:
 *     a) presets de un clic (la mayoría de los usuarios nunca toca specs),
 *     b) sliders para specs (menos typos que number inputs),
 *     c) <details> de configuración avanzada colapsado (GPU).
 *  2. ESTIMACIÓN DINÁMICA DE PRECIO: useMemo sobre las tasas del server
 *     (/me/rates → rates_per_hour) + saldo de créditos (/me/credits) con
 *     aviso de saldo insuficiente ANTES del POST (el server daría 402).
 *  3. ESTADOS DE CARGA FLUIDOS: skeletons por sección, una caída de endpoint
 *     NO rompe el wizard (retry:1 + degradación elegante), botón con estado,
 *     y al crear: fila optimista 'provisioning' + cierre inmediato
 *     (el WS /ws/events la lleva a 'running' sin esperar).
 *  4. ESPEJO DE SANITIZACIÓN: preview del nombre/hhost_id replicando
 *     _sanitename del backend (incluye NFD para acentos), para que el usuario
 *     vea el hostname REAL antes de crear.
 *
 * Endpoints (mismos JSON que consume web/index.html):
 *   GET /me/rates    → {rates_per_hour:{cpu,ram_gb,disk_gb,gpu},...}
 *   GET /me/credits  → {credits,...}
 *   GET /quota       → {quota:{cpu,ram_mb,disk_gb,max_vms,...},used:{vm_cpu,vm_ram,vm_disk,vms,...}}
 *   GET /api/gpus    → string[]
 *   POST /vm         → {vmid,name,host_id,ip,cpu,ram_gb,disk_gb,status:'provisioning',gpu,gpu_model}
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

// ─── Tipos de API (idénticos a los JSON reales de app.py) ──────────────────
type Rates = { cpu: number; ram_gb: number; disk_gb: number; gpu: number };
type RatesResponse = { rates_per_hour?: Rates; rates?: Rates };
type CreditsResponse = { credits: number };
type QuotaResponse = {
  quota: { cpu: number; ram_mb: number; disk_gb: number; max_vms: number; max_containers: number };
  used: { vm_cpu?: number; vm_ram?: number; vm_disk?: number; vms?: number };
};
type CreateVMResp = {
  vmid: number; name: string; cpu: number; ram_gb: number; disk_gb: number;
  status: "provisioning"; ip?: string; host_id?: string;
  gpu?: boolean; gpu_model?: string | null;
};

/** Fila optimista: mismo contrato que insertVmRow() en web/index.html. */
export type CreatedRow = {
  ref_id: string; name: string; status: string; cpu: number; ram_mb: number;
  disk_gb: number; host_id: string | null; subnet: number | null;
  gpu_mapping: string | null; gpu_model: string | null;
};

// ─── Token: el SPA vanilla guarda {role,token} en sessionStorage["nubecita"];
//     el scaffold usaba "nubecita_token" (token crudo). Probar ambos. ───────
function getToken(): string {
  try {
    const raw = sessionStorage.getItem("nubecita");
    if (raw) {
      const s = JSON.parse(raw) as { token?: string };
      if (s?.token) return s.token;
    }
    return sessionStorage.getItem("nubecita_token") ?? "";
  } catch {
    return "";
  }
}

async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: {
      Authorization: "Bearer " + getToken(),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let d: { detail?: string } | null = null;
  try { d = await res.json(); } catch { /* sin cuerpo JSON */ }
  if (!res.ok) throw new Error(d?.detail ?? `${res.status} ${res.statusText}`);
  return d as T;
}

// ─── Espejo de _sanitename del backend (app.py) ────────────────────────────
export function sanitizeName(raw: string, fallback = "vm"): string {
  let s = raw.toLowerCase()
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")   // acentos → letra base
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 20);
  return s || fallback;
}

// ─── Presets: defaults lógicos, un clic ────────────────────────────────────
type Preset = {
  id: string; label: string; desc: string;
  cpu: number; ram_gb: number; disk_gb: number; gpu_model?: string;
};
const PRESETS: Preset[] = [
  { id: "tiny",   label: "Tiny",   desc: "1c · 2 GB · 10 GB",  cpu: 1, ram_gb: 2,  disk_gb: 10 },
  { id: "small",  label: "Small",  desc: "2c · 4 GB · 20 GB",  cpu: 2, ram_gb: 4,  disk_gb: 20 },
  { id: "medium", label: "Medium", desc: "4c · 16 GB · 40 GB", cpu: 4, ram_gb: 16, disk_gb: 40 },
  { id: "gpu",    label: "GPU",    desc: "4c · 16 GB · 40 GB · A5000", cpu: 4, ram_gb: 16, disk_gb: 40, gpu_model: "RTX A5000" },
];

const fmtCr = (n: number) => n.toFixed(2).replace(/\.?0+$/, "");

// ─── Skeletons (percepción de velocidad) ───────────────────────────────────
const Skel = ({ w = "100%", h = 14 }: { w?: string | number; h?: number }) => (
  <span className="skel" style={{ display: "inline-block", width: w, height: h,
    borderRadius: 6, background: "linear-gradient(90deg,#222 25%,#333 50%,#222 75%)",
    backgroundSize: "200% 100%", animation: "skel 1.2s infinite" }} aria-hidden />
);

// ─── Componente principal ──────────────────────────────────────────────────
export function LaunchWizard({ onCreated }: {
  // el store del listado inserta la fila optimista con este payload
  onCreated: (row: CreatedRow) => void;
}) {
  const qc = useQueryClient();
  const [presetId, setPresetId] = useState("small");
  const [name, setName] = useState("");
  const [cpu, setCpu] = useState(2);
  const [ramGb, setRamGb] = useState(4);
  const [diskGb, setDiskGb] = useState(20);
  const [gpuModel, setGpuModel] = useState("");        // "" = sin GPU (LXC)
  const [advOpen, setAdvOpen] = useState(false);       // revelación progresiva

  // Datos: React Query cachea rates/credits/quota/gpus entre aperturas del modal.
  // retry:1 + staleTime largo: si un endpoint cae, el wizard degrada sin romperse.
  const ratesQ  = useQuery({ queryKey: ["rates"],   staleTime: 5 * 60_000, retry: 1,
    queryFn: async () => {
      const r = await api<RatesResponse>("/me/rates");
      return r.rates_per_hour ?? r.rates ?? null;
    } });
  const credQ   = useQuery({ queryKey: ["credits"], queryFn: () => api<CreditsResponse>("/me/credits"), retry: 1 });
  const quotaQ  = useQuery({ queryKey: ["quota"],   queryFn: () => api<QuotaResponse>("/quota"), retry: 1 });
  const gpusQ   = useQuery({ queryKey: ["gpus"],    queryFn: () => api<string[]>("/api/gpus"), staleTime: 30_000, retry: 1 });

  const rates = ratesQ.data;
  const credits = credQ.data?.credits;
  const safeName = sanitizeName(name);
  const hostId = `h${safeName}`.slice(0, 24);

  // ── Estimación de costo en vivo (revelación: siempre visible, compacta) ──
  const cost = useMemo(() => {
    if (!rates) return null;
    return cpu * rates.cpu + ramGb * rates.ram_gb + diskGb * rates.disk_gb
      + (gpuModel ? rates.gpu : 0);
  }, [rates, cpu, ramGb, diskGb, gpuModel]);

  const insufficient = cost != null && credits != null && credits < cost;
  const hoursLeft = cost && credits != null && cost > 0
    ? Math.floor(credits / cost) : null;

  // ── Guardas locales ANTES de gastar un round-trip (mismas claves que /quota) ──
  const quotaErr = useMemo(() => {
    const q = quotaQ.data; if (!q) return null;
    const freeCpu = (q.quota.cpu ?? Infinity) - (q.used.vm_cpu ?? 0);
    if (cpu > freeCpu) return `CPU: te quedan ${Math.max(0, freeCpu)} cores libres`;
    const freeRamMb = (q.quota.ram_mb ?? Infinity) - (q.used.vm_ram ?? 0);
    if (ramGb * 1024 > freeRamMb) return "RAM: superás tu cuota";
    const freeDisk = (q.quota.disk_gb ?? Infinity) - (q.used.vm_disk ?? 0);
    if (diskGb > freeDisk) return "Disco: superás tu cuota";
    if ((q.used.vms ?? 0) + 1 > (q.quota.max_vms ?? Infinity))
      return `máx. de VMs alcanzado (${q.quota.max_vms})`;
    return null;
  }, [quotaQ.data, cpu, ramGb, diskGb]);

  const createMut = useMutation({
    mutationFn: () => api<CreateVMResp>("/vm", "POST", {
      name: safeName, cpu, ram_gb: ramGb, disk_gb: diskGb,
      gpu: !!gpuModel, gpu_model: gpuModel,
    }),
    onSuccess: (r) => {
      // Fila optimista: la UI muestra 'provisioning' al instante; el WS
      // /ws/events la actualiza a 'running' sin refetch completo.
      const ip = String(r.ip || "").split("/")[0];
      onCreated({
        ref_id: String(r.vmid), name: r.name, status: "provisioning",
        cpu: r.cpu, ram_mb: r.ram_gb * 1024, disk_gb: r.disk_gb,
        host_id: r.host_id ?? null,
        subnet: ip ? parseInt(ip.split(".")[2]) : null,
        gpu_mapping: r.gpu ? (r.gpu_model ?? "gpu") : null,
        gpu_model: r.gpu_model ?? null,
      });
      qc.invalidateQueries({ queryKey: ["credits"] });
      qc.invalidateQueries({ queryKey: ["quota"] });
    },
  });

  const pickPreset = (p: Preset) => {
    setPresetId(p.id); setCpu(p.cpu); setRamGb(p.ram_gb); setDiskGb(p.disk_gb);
    setGpuModel(p.gpu_model ?? "");
    if (p.gpu_model && diskGb < 8) setDiskGb(8); // template GPU = 8G mínimo
  };

  const loadingHeader = ratesQ.isLoading || credQ.isLoading;

  return (
    <div className="wizard" role="dialog" aria-label="Asistente de lanzamiento">
      {/* Header con saldo — contexto de costo siempre a mano */}
      <header className="wz-head">
        <h3>Crear VM</h3>
        {loadingHeader ? <Skel w={140} /> : credits != null && (
          <span className="balance">Saldo: <b>{fmtCr(credits)} cr</b></span>
        )}
      </header>

      {/* Paso 1 — presets: la decisión de 1 clic */}
      <section aria-label="Presets">
        <div className="preset-grid">
          {PRESETS.map((p) => (
            <button key={p.id}
              className={`preset ${presetId === p.id ? "on" : ""}`}
              onClick={() => pickPreset(p)}>
              <b>{p.label}</b><small>{p.desc}</small>
              {rates && (
                <small className="pcost">
                  {fmtCr(p.cpu * rates.cpu + p.ram_gb * rates.ram_gb + p.disk_gb * rates.disk_gb
                    + (p.gpu_model ? rates.gpu : 0))} cr/h
                </small>
              )}
            </button>
          ))}
        </div>
      </section>

      {/* Paso 1 — nombre con espejo de hostname real */}
      <section>
        <label htmlFor="wzName">Nombre</label>
        <input id="wzName" value={name} placeholder="mi-vm"
          onChange={(e) => setName(e.target.value)} autoFocus />
        {name && (
          <p className="hint">
            hostname SSH: <code>ssh {hostId}@ssh.surus.lat</code>
            {safeName !== name.trim() && <> · se normaliza a <code>{safeName}</code></>}
          </p>
        )}
      </section>

      {/* Paso 1 — specs (sliders en vez de number inputs: menos typos) */}
      <section className="specs">
        {([["CPU", cpu, setCpu, 1, 8, "cores"],
           ["RAM", ramGb, setRamGb, 1, 32, "GB"],
           ["Disco", diskGb, setDiskGb, gpuModel ? 8 : 5, 80, "GB"]] as const)
          .map(([label, val, set, min, max, unit]) => (
          <label key={label} className="spec">
            <span>{label}: <b>{val}</b> {unit}</span>
            <input type="range" min={min} max={max} value={val}
              onChange={(e) => set(+e.target.value)} />
          </label>
        ))}
      </section>

      {/* Revelación progresiva: avanzado colapsado por defecto */}
      <details open={advOpen} onToggle={(e) => setAdvOpen((e.target as HTMLDetailsElement).open)}>
        <summary>Configuración avanzada</summary>
        <label>GPU
          <select value={gpuModel}
            onChange={(e) => { setGpuModel(e.target.value); if (e.target.value && diskGb < 8) setDiskGb(8); }}>
            <option value="">Sin GPU (contenedor LXC, más rápido)</option>
            {(gpusQ.data ?? []).map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        </label>
        {gpuModel && <p className="hint">VM QEMU con passthrough · arranque 2-3 min · disco mínimo 8 GB.</p>}
      </details>

      {/* Estimador de costo en vivo (el corazón económico del flujo) */}
      <div className="costbox" aria-live="polite">
        {cost == null
          ? <><Skel w={90} h={12} /> calculando costo…</>
          : <>
              <b>{fmtCr(cost)} créditos/hora</b>
              {hoursLeft != null && <> · ≈ {hoursLeft} h con tu saldo</>}
              {insufficient && <p className="warn">Saldo insuficiente para 1 h — recargá antes de crear.</p>}
              {quotaErr && <p className="warn">{quotaErr}</p>}
            </>}
      </div>

      {/* Pie: un solo botón primario; el error del server se ve inline */}
      <footer className="wz-foot">
        {createMut.isError && (
          <p className="err">{(createMut.error as Error).message}</p>
        )}
        <button className="primary"
          disabled={createMut.isPending || insufficient || !!quotaErr}
          onClick={() => createMut.mutate()}>
          {createMut.isPending ? "Creando…" : "Crear VM"}
        </button>
      </footer>
    </div>
  );
}
