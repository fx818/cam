import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { AdminSettingsPage } from "./AdminSettingsPage"

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }
}

const settings = {
  settings: {
    default_timeout_ms: 60000,
    response_cap_tokens: 50000,
    log_retention_days: 30,
    allowed_email_domains: [],
    instructions: "",
    brand: {},
  },
}

const allowlist = {
  entries: [
    {
      id: "e1",
      pattern: "10.0.0.0/8",
      note: "internal",
      created_by_email: "admin@example.com",
      created_at: "2026-09-01T00:00:00Z",
    },
  ],
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AdminSettingsPage />
    </QueryClientProvider>,
  )
}

describe("AdminSettingsPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    document.cookie = "cam_csrf=csrf-value"
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? "GET"
        if (url.includes("/api/admin/settings") && method === "GET") {
          return jsonResponse(settings)
        }
        if (url.includes("/api/admin/settings") && method === "PATCH") {
          return jsonResponse(settings)
        }
        if (url.includes("/api/admin/url-allowlist") && method === "GET") {
          return jsonResponse(allowlist)
        }
        if (url.includes("/api/admin/url-allowlist") && method === "POST") {
          return jsonResponse({ entry: allowlist.entries[0] }, 201)
        }
        throw new Error(`unexpected fetch ${method} ${url}`)
      }),
    )
  })

  it("renders defaults and allowlist entries", async () => {
    renderPage()
    expect(await screen.findByLabelText(/timeout/i)).toHaveValue("60000")
    expect(screen.getByText("10.0.0.0/8")).toBeInTheDocument()
    expect(screen.getByLabelText(/log retention/i)).toHaveValue("30")
  })

  it("saves settings and adds allowlist patterns", async () => {
    renderPage()
    const user = userEvent.setup()
    await screen.findByLabelText(/timeout/i)

    await user.clear(screen.getByLabelText(/timeout/i))
    await user.type(screen.getByLabelText(/timeout/i), "45000")
    await user.click(screen.getByRole("button", { name: /save settings/i }))
    await waitFor(() => {
      expect(screen.getByText("Saved.")).toBeInTheDocument()
    })

    await user.type(screen.getByLabelText(/^pattern/i), "intranet.local")
    await user.click(screen.getByRole("button", { name: "Add" }))
    await waitFor(() => {
      const postCall = vi.mocked(fetch).mock.calls.find(
        ([input, init]) =>
          String(input).includes("/api/admin/url-allowlist") &&
          (init?.method ?? "GET") === "POST",
      )
      expect(postCall).toBeTruthy()
    })
  })
})
