import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import "./styles.css";

const rootEl = document.getElementById("root");
if (!rootEl) {
  // si por alguna razón el <div id="root"> no está, lanzamos un error claro.
  throw new Error("falta <div id='root'> en index.html");
}

// QueryClient compartido por el scaffold (App → wizards). La isla (island.ts)
// crea el suyo propio dentro del custom element.
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(rootEl).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
