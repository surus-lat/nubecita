/**
 * App — raíz del scaffold.
 *
 * Muestra el wizard + una lista de rows "provisioning" mock que recibe
 * `onCreated` para demostrar el flujo optimista.
 *
 * Comportamiento documentado (store no incluido en el scaffold):
 *   - Cuando el wizard llama `onCreated(row)`, el store real insertaría
 *     esa fila con status 'provisioning' y la actualizaría cuando el
 *     WebSocket reporte 'running' | 'failed'.
 *   - Aquí, al recibir onCreated, se agrega la fila a la lista de demo.
 */
import { useCallback, useState } from "react";
import {
  ResourceLaunchWizard,
  type Row,
} from "./components/ResourceLaunchWizard";
import { StatusPill as Pill } from "./components/StatusPill";

// Rows de arranque: dos filas mock para que la UI tenga algo que mostrar.
const INITIAL_ROWS: Row[] = [
  { ref: "vm: 6101", name: "api-prod", status: "running" },
  { ref: "vm: 6102", name: "grafana", status: "stopped" },
];

export function App() {
  const [rows, setRows] = useState<Row[]>(INITIAL_ROWS);

  // El store real (no incluido aquí) insertaría la fila optimista.
  const onCreated = useCallback((row: Row) => {
    setRows((prev) => [row, ...prev]);
  }, []);

  return (
    <>
      <h1>
        nubecita · Asistente de Lanzamiento de Recursos
      </h1>
      <p className="muted small">
        Elegí un preset, ajustá los campos y lanzá. El estado final llega por
        WebSocket (este scaffold no se conecta: la fila queda "provisioning").
      </p>

      <ResourceLaunchWizard onCreated={onCreated} />

      <h2 style={{ marginTop: 18 }}>
        Tus recursos <span className="muted small">({rows.length})</span>
      </h2>
      <div className="rows">
        {rows.map((r) => (
          <div key={r.ref} className="row">
            <span className="ref">{r.ref}</span>
            <span className="name">{r.name}</span>
            <Pill status={r.status} />
          </div>
        ))}
      </div>
    </>
  );
}

export default App;
