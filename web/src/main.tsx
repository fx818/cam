import { QueryClientProvider } from "@tanstack/react-query"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router-dom"

import { AppRoutes } from "@/App"
import { AuthProvider } from "@/lib/auth"
import { queryClient } from "@/lib/queryClient"
import "./index.css"

const container = document.getElementById("root")
if (!container) {
  throw new Error("root element not found")
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
