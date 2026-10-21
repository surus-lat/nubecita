/**
 * api.ts — cliente mínimo de fetch contra los endpoints del backend nubecita.
 *
 * Todos los endpoints usan un Bearer token guardado en sessionStorage
 * (clave `nubecita_token`), el mismo que ya usa web/index.html.
 *
 * Endpoints que consume (base relativa './' — ver vite.config.ts):
 *   GET  /me/rates      → {rates:{cpu,ram_gb,disk_gb,gpu},defaults,labels}
 *   GET  /me/credits    → {credits,cost_per_hour,hours_left}
 *   GET  /quota         → {quota:{cpu,ram_mb,disk_gb,max_vms,max_containers},used}
 *   GET  /api/gpus      → string[]
 *   POST /vm            {name,cpu,ram_gb,disk_gb,gpu,gpu_model}
 *                        → {vmid,name,host_id,ip,cpu,ram_gb,disk_gb,status:'provisioning'}
 *   POST /service       {name,image,cpu,ram_mb,replicas}
 *                        → {job_id,name,status}
 *
 * Errores del backend llegan como {detail:'...'} → se normalizan a throw de
 * ApiError con el mensaje, para que el wizard pueda mostrar el texto al cliente.
 */

// ─── Tipos ───────────────────────────────────────────────────────────────
export type Rates = { cpu: number; ram_gb: number; disk_gb: number; gpu: number };
export type Defaults = Rates;
export type RateLabels = Record<string, string>;

export type RatesResponse = {
  rates: Rates;
  defaults?: Defaults;
  labels?: RateLabels;
};

export type CreditsResponse = {
  credits: number;
  cost_per_hour: number;
  hours_left: number | null;
};

export type Quota = {
  cpu: number;
  ram_mb: number;
  disk_gb: number;
  max_vms: number;
  max_containers: number;
};

export type Used = {
  vms?: number;
  containers?: number;
  cpu?: number;
  ram_mb?: number;
  disk_gb?: number;
};

export type QuotaResponse = {
  quota: Quota;
  used: Used;
};

export type CreateVMBody = {
  name: string;
  cpu: number;
  ram_gb: number;
  disk_gb: number;
  gpu: boolean;
  gpu_model: string;
};

export type CreateVMResponse = {
  vmid: number;
  name: string;
  host_id: string;
  ip: string;
  cpu: number;
  ram_gb: number;
  disk_gb: number;
  status: "provisioning";
};

export type CreateServiceBody = {
  name: string;
  image: string;
  cpu: number;
  ram_mb: number;
  replicas: number;
};

export type CreateServiceResponse = {
  job_id: string;
  name: string;
  status: string;
};

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// ─── Cliente ───────────────────────────────────────────────────────────────
const TOKEN_KEY = "nubecita_token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // sessionStorage no disponible (p. ej. SSR) → sin token
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    // fallback: mensaje amable si el fetch no llega (red cortada, etc.)
    throw new ApiError(0, "no pude hablar con el servidor");
  }

  // 401/403 → token inválido o vencido
  if (res.status === 401 || res.status === 403) {
    throw new ApiError(res.status, "no tenés acceso con este token");
  }

  // Parsear cuerpo (o vaciar si el endpoint no devuelve nada)
  let data: unknown = null;
  const ct = res.headers.get("content-type") || "";
  if (res.status !== 204 && ct.includes("application/json")) {
    data = await res.json();
  } else if (res.status !== 204) {
    try {
      data = await res.json();
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    const msg =
      (data as { detail?: string } | null)?.detail ||
      `error ${res.status} del servidor`;
    throw new ApiError(res.status, msg);
  }

  return data as T;
}

// ─── Endpoints ──────────────────────────────────────────────────────────────
export const getRates = () => request<RatesResponse>("GET", "/me/rates");
export const getCredits = () => request<CreditsResponse>("GET", "/me/credits");
export const getQuota = () => request<QuotaResponse>("GET", "/quota");
export const getGpus = () => request<string[]>("GET", "/api/gpus");
export const createVM = (body: CreateVMBody) =>
  request<CreateVMResponse>("POST", "/vm", body);
export const createService = (body: CreateServiceBody) =>
  request<CreateServiceResponse>("POST", "/service", body);
