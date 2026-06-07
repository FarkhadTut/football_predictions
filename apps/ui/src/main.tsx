import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const container = document.getElementById("root");
if (!container) {
  throw new Error("root element not found");
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Match data updates while the watcher is running — short freshness window.
      staleTime: 30_000,
      retry: 1,
    },
  },
});

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
);
