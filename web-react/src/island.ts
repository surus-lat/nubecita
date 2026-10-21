/**
 * island.ts — isla React montable desde el SPA vanilla.
 *
 * Build: `npm run build:island` → web/react-island.js (IIFE de un solo
 * archivo, sin ESM). El SPA la carga lazy con <script src="/react/island.js">
 * al primer clic en «+ Crear VM» (ver web/index.html).
 *
 * Contrato con el vanilla:
 *   - Al cargarse define window.NubecitaWizard (flag: la isla está disponible;
 *     si el script 404, el flag no existe y el SPA usa el modal vanilla).
 *   - Registra el custom element <nubecita-launch-wizard>: al conectarse monta
 *     el React root (QueryClientProvider + LaunchWizard) en una shadow root
 *     con wizard.css inlined; al desconectarse hace root.unmount().
 *   - Al crear la VM despacha CustomEvent 'nc-created' (bubbles+composed) con
 *     detail = la fila optimista, MISMO contrato que insertVmRow() del vanilla.
 */
import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LaunchWizard, type CreatedRow } from "./components/LaunchWizard";
import wizardCss from "./wizard.css?inline";

const TAG = "nubecita-launch-wizard";

class NubecitaLaunchWizard extends HTMLElement {
  private root: Root | null = null;

  connectedCallback() {
    if (this.root) return; // re-conexión: no montar dos veces
    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = wizardCss; // CSS inlined → aislado del SPA
    const host = document.createElement("div");
    shadow.append(style, host);

    const qc = new QueryClient({
      defaultOptions: { queries: { refetchOnWindowFocus: false } },
    });
    this.root = createRoot(host);
    this.root.render(
      React.createElement(
        QueryClientProvider,
        { client: qc },
        React.createElement(LaunchWizard, {
          onCreated: (row: CreatedRow) => {
            this.dispatchEvent(
              new CustomEvent("nc-created", {
                bubbles: true,
                composed: true, // atraviesa la shadow root → lo escucha el modal vanilla
                detail: row,
              }),
            );
          },
        }),
      ),
    );
  }

  disconnectedCallback() {
    if (this.root) {
      this.root.unmount();
      this.root = null;
    }
  }
}

if (!customElements.get(TAG)) {
  customElements.define(TAG, NubecitaLaunchWizard);
}

// Flag de disponibilidad para el SPA vanilla (ver openCreate en index.html).
declare global {
  interface Window {
    NubecitaWizard?: { version: string; tag: string };
  }
}
window.NubecitaWizard = { version: "0.1.0", tag: TAG };
