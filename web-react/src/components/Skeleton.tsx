/**
 * Skeleton — barra de carga (mismo estilo que el SPA vanilla de web/index.html).
 *
 * Se usa mientras cargan rates/credits/quota/gpus. Una caída de cualquiera
 * de esos endpoints NO rompe el wizard: el skeleton queda oculto y se
 * muestra un aviso con el dato que no se pudo cargar.
 */
export function Skeleton({ height = 16 }: { height?: number }) {
  return (
    <div
      className="skeleton"
      style={{ height: `${height}px` }}
      role="progressbar"
      aria-busy="true"
      aria-label="cargando"
    />
  );
}

export default Skeleton;
