import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { ServiceDetailResponse, ServicesListResponse } from "@/lib/types"

import { MyServicesPage } from "./MyServicesPage"

const notionDetail: ServiceDetailResponse = {
  service: {
    id: "global/notion",
    tier: "template",
    name: "Notion",
    description: "",
    slug: "notion",
    kind: "rest",
    url: "https://api.notion.com",
    transport: null,
    spec_url: null,
    auth_type: "oauth",
    auth_config: {},
    visibility: "all",
    timeout_ms: 30000,
    response_cap_tokens: 50000,
    status: "active",
    health: "unknown",
    last_synced_at: null,
    created_at: null,
    owner: null,
  },
  connection: null,
  viewer: { is_owner: true, is_admin: false, can_manage: true, is_shared: false },
}

const services: ServicesListResponse = {
  services: [
    {
      id: "global/redash",
      group: "global",
      tier: "global",
      name: "Redash",
      description: "Query runner",
      owner: null,
      shared_by: null,
      health: "healthy",
      tool_count: 4,
      status: "active",
      connection: null,
    },
    {
      id: "global/notion",
      group: "templates",
      tier: "template",
      name: "Notion",
      description: "OAuth template",
      owner: null,
      shared_by: null,
      health: "unknown",
      tool_count: 6,
      status: "active",
      connection: null,
    },
    {
      id: "rahul/my-wiki",
      group: "mine",
      tier: "personal",
      name: "My wiki",
      description: "Personal MCP",
      owner: "rahul",
      shared_by: null,
      health: "healthy",
      tool_count: 2,
      status: "active",
      connection: {
        id: "c1",
        status: "active",
        expires_at: null,
        created_at: null,
      },
    },
    {
      id: "anita/jira",
      group: "shared",
      tier: "personal",
      name: "Jira",
      description: "Shared by Anita",
      owner: "anita",
      shared_by: "anita",
      health: "healthy",
      tool_count: 3,
      status: "active",
      connection: {
        id: "c2",
        status: "active",
        expires_at: null,
        created_at: null,
      },
    },
  ],
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <MyServicesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe("MyServicesPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(services),
      })),
    )
  })

  it("shows all four groups with their services", async () => {
    renderPage()
    expect(await screen.findByText("Global")).toBeInTheDocument()
    expect(screen.getByText("Templates")).toBeInTheDocument()
    expect(screen.getAllByText("My services").length).toBeGreaterThan(1)
    expect(screen.getByText("Shared with me")).toBeInTheDocument()
    expect(await screen.findByText("Redash")).toBeInTheDocument()
    expect(screen.getByText("My wiki")).toBeInTheDocument()
    expect(screen.getByText("Jira")).toBeInTheDocument()
  })

  it("offers Connect for unconnected templates", async () => {
    renderPage()
    expect(await screen.findByRole("button", { name: "Connect" })).toBeInTheDocument()
  })

  it("opens the connect dialog and redirects for OAuth templates", async () => {
    const user = userEvent.setup()
    const assign = vi.fn()
    Object.defineProperty(window, "location", {
      value: { ...window.location, assign },
      writable: true,
    })
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (init?.method === "POST") {
          return {
            ok: true,
            status: 200,
            text: async () =>
              JSON.stringify({ authorize_url: "https://oauth.example/authorize?state=s1" }),
          }
        }
        if (url === "/api/services") {
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify(services),
          }
        }
        return {
          ok: true,
          status: 200,
          text: async () => JSON.stringify(notionDetail),
        }
      }),
    )
    renderPage()
    await user.click(await screen.findByRole("button", { name: "Connect" }))
    await user.click(await screen.findByRole("button", { name: "Authorize" }))
    await vi.waitFor(() => {
      expect(assign).toHaveBeenCalledWith("https://oauth.example/authorize?state=s1")
    })
  })

  it("links personal services to their detail page", async () => {
    renderPage()
    const open = await screen.findAllByRole("link", { name: "Open" })
    expect(open).toHaveLength(3)
    expect(open[0]).toHaveAttribute("href", "/services/global/redash")
  })
})
