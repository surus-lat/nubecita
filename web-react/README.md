# web-react — Asistente de Lanzamiento de Recursos (nubecita)

Scaffold **React + Vite + TypeScript** del Asistente de Lanzamiento de
Recursos (wizard) para `nubecita.surus.lat`. Convive **sin reemplazar** la SPA
vanilla existente (`web/index.html`); es una alternativa lista para
integrar al deploy cuando se quiera migrar.

## Qué es

Un wizard de 3 niveles de revelación progresiva para lanzar **VMs** (LXC/QEMU)
y **servicios** (Nomad):

1. **Presets** — Tiny (1c/2G/10G), Small (2c/4G/20G), Medium (4c/16G/40G),
   GPU (4c/16G/40G + RTX A5000, **solo para VM**).
2. **Nombre / imagen / spec** — ajustás CPU, RAM, disco (o replicas), imagen
   y (para VM) si querés GPU + modelo.
3. **Configuración avanzada** — `<details>` colapsado con cuota, saldo y tasas.

Incluye:
- **Estimación de precio en vivo** (`CostEstimator`, `useMemo` sobre `rates`):
  muestra `cr/h` y "horas restantes con tu saldo".
- **Validación local**: nombre (mín 1 char, el server sanitiza), GPU requiere
  disco ≥ 8 GB, y "saldo insuficiente" con el faltante si `credits < costo`.
- **Skeletons** mientras cargan rates/credits/quota/gpus (`Promise.allSettled`
  — **una caída no rompe el wizard**: muestra un aviso y sigue usable).
- **Busy state** con spinner en el submit.
- Al crear: `POST /vm` o `POST /service`, luego `onCreated(row)` con
  `{ref,name,status}` para que el store inserte la fila optimista (comportamiento
  documentado en `src/components/ResourceLaunchWizard.tsx`; el scaffold no trae
  store, `App.tsx` muestra un list de rows mock que recibe `onCreated`).
- `StatusPill`: running (green `#3ecf7a`), provisioning (cyan `#00A2B0`,
  animado), stopped (amber `#f0b34c`), deleted/absent (red `#ff6161`).
- `sanitizeName` (`src/lib/sanitize.ts`) **espeja al backend** (`_sanitename`
  de `app.py`): lowercase, trim, `[^a-z0-9-]`→`-`, colapso de guiones,
  `slice(0,20)`, fallback si queda vacío.
- **Tema dark-teal** (`--bg:#001519 --panel:#05252e --line:#14434e
  --acc:#00A2B0 --txt:#d6eef0 --mut:#7ea9ad`). UI en español rioplatense.
- **Sin dependencias pesadas**: solo `react`, `react-dom`, `vite`,
  `@vitejs/plugin-react` y `typescript`.

## Cómo build

```bash
npm install
npm run build        # tsc --noEmit && vite build  → dist/
npm run dev          # servidor de desarrollo
```

El `dist/` queda listo para servir. `vite.config.ts` usa `base: './'` para que
los assets sean relativos y el bundle pueda servirse desde cualquier sub-path
de uvicorn (mismo contrato que `web/index.html`).

## Qué consume (endpoints del backend, base relativa `./`)

El cliente (`src/api.ts`) usa un **Bearer token** guardado en `sessionStorage`
(clave `nubecita_token`, el mismo que la SPA vanilla). Errores del backend
llegan como `{detail:'...'}` y se normalizan a `ApiError`.

| Método | Ruta         | Cuerpo                                        | Respuesta                                                                 |
| ------ | ------------ | --------------------------------------------- | ------------------------------------------------------------------------- |
| GET    | `/me/rates`  | —                                             | `{rates:{cpu,ram_gb,disk_gb,gpu},defaults,labels}`                        |
| GET    | `/me/credits`| —                                             | `{credits,cost_per_hour,hours_left}`                                      |
| GET    | `/quota`     | —                                             | `{quota:{cpu,ram_mb,disk_gb,max_vms,max_containers},used}`                |
| GET    | `/api/gpus`  | —                                             | `string[]` (modelos de GPU del cluster)                                    |
| POST   | `/vm`        | `{name,cpu,ram_gb,disk_gb,gpu,gpu_model}`     | `{vmid,name,host_id,ip,cpu,ram_gb,disk_gb,status:'provisioning'}`         |
| POST   | `/service`   | `{name,image,cpu,ram_mb,replicas}`            | `{job_id,name,status}`                                                    |

Nota: el `POST /vm` responde en ~2s con `status:'provisioning'`; el estado
final (`running`/`failed`) llega por **WebSocket**, que este scaffold **no**
implementa.

## Cómo se integraría al deploy actual (NO se toca todavía)

La SPA actual se sirve desde `web/index.html` vía `FileResponse` en `app.py`
(`GET /`). Para pasar a esta SPA:

1. `npm run build` → `web-react/dist/` (index.html + assets/).
2. Copiar/mapear `dist/` a `web/react/` (o un path dedicado) en el CT 6003.
3. Servir `dist/index.html` por uvicorn como **alternativa** a `web/index.html`
   (p. ej. un alias `/app` o reemplazo puntual en `app.py`).
4. Mantener los endpoints igualitos (ya lo son) y el token en sessionStorage.

**Este commit no modifica `app.py`, `web/index.html` ni el deploy** — el
scaffold queda listo para ser integrado en un paso futuro.

## Estructura

```
web-react/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── api.ts
    ├── styles.css
    ├── vite-env.d.ts
    ├── components/
    │   ├── ResourceLaunchWizard.tsx
    │   ├── StatusPill.tsx
    │   ├── CostEstimator.tsx
    │   └── Skeleton.tsx
    └── lib/
        └── sanitize.ts
```
