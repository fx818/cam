import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { AuthProvider } from "@/lib/auth"

import { AgentSetupPage } from "./AgentSetupPage"

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <AgentSetupPage />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

const me = {
  user: {
    id: "u1",
    email: "rahul@example.com",
    handle: "rahul",
    display_name: "Rahul",
    role: "user",
    kind: "human",
    status: "active",
  },
  org: { id: "o1", slug: "acme", name: "Acme", settings: {} },
  base_url: "http://cam.test",
}

const tokens = {
  tokens: [
    {
      id: "t1",
      label: "laptop",
      token_prefix: "ab12",
      last_used_at: null,
      revoked_at: null,
      created_at: "2026-09-01T00:00:00Z",
    },
  ],
  next_cursor: null,
}

describe("AgentSetupPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    document.cookie = "cam_csrf=csrf-value"
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? "GET"
        if (url.includes("/api/auth/me")) {
          return jsonResponse(me)
        }
        if (url.includes("/api/tokens") && method === "GET") {
          return jsonResponse(tokens)
        }
        if (url.includes("/api/tokens") && method === "POST") {
          const body = JSON.parse(String(init?.body)) as { label: string }
          return jsonResponse(
            {
              id: "t2",
              label: body.label,
              token_prefix: "zz99",
              last_used_at: null,
              revoked_at: null,
              created_at: "2026-09-24T00:00:00Z",
              token: "cam_raw_secret_value",
            },
            201,
          )
        }
        throw new Error(`unexpected fetch ${method} ${url}`)
      }),
    )
  })

  it("lists active tokens", async () => {
    renderPage()
    expect(await screen.findByText("laptop")).toBeInTheDocument()
    expect(screen.getByText(/cam_ab12/)).toBeInTheDocument()
  })

  it("shows the raw token once after creating it", async () => {
    renderPage()
    const user = userEvent.setup()
    await screen.findByText("laptop")
    await user.click(screen.getByRole("button", { name: /generate token/i }))
    await user.type(screen.getByLabelText(/label/i), "new-agent")
    await user.click(screen.getByRole("button", { name: "Create" }))
    await waitFor(() => {
      expect(screen.getByText("cam_raw_secret_value")).toBeInTheDocument()
    })
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument()
    const request = vi.mocked(fetch).mock.calls.find(
      ([input, init]) =>
        String(input).includes("/api/tokens") &&
        (init?.method ?? "GET") === "POST",
    )
    expect(request?.[1]?.headers).toMatchObject({ "X-CSRF-Token": "csrf-value" })
  })

  it("renders agent config snippets with the base URL", async () => {
    renderPage()
    const matches = await screen.findAllByText(/http:\/\/cam\.test\/mcp/)
    expect(matches.length).toBeGreaterThanOrEqual(2)
  })
})
