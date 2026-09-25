import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { RegisterServicePage } from "./RegisterServicePage"

const operations = [
  { name: "getUser", description: "Fetches one user by id" },
  {
    name: "listUsers",
    description:
      "Lists every single user of the workspace with full paging support, custom filters and per-user metadata; this description is deliberately long so it must truncate.",
  },
]

function renderWizard() {
  return render(
    <MemoryRouter>
      <RegisterServicePage />
    </MemoryRouter>,
  )
}

describe("RegisterServicePage", () => {
  const fetchMock = vi.fn()
  let calls: { url: string; body: Record<string, unknown> }[] = []

  beforeEach(() => {
    vi.unstubAllGlobals()
    calls = []
    fetchMock.mockReset()
    vi.stubGlobal("fetch", fetchMock)
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {}
      calls.push({ url, body })
      if (url.includes("/api/services/test")) {
        return {
          ok: true,
          status: 200,
          text: async () => JSON.stringify({ ok: true, tools: operations }),
        }
      }
      if (url.includes("/api/services")) {
        return {
          ok: true,
          status: 200,
          text: async () =>
            JSON.stringify({ service: { id: "rahul/my-api", name: "My API" } }),
        }
      }
      return { ok: true, status: 200, text: async () => "{}" }
    })
  })

  const advance = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("button", { name: "Next" }))
  }

  const walkToTest = async (
    user: ReturnType<typeof userEvent.setup>,
    spec = "",
  ) => {
    await user.click(screen.getByRole("button", { name: /rest api/i }))
    await advance(user)
    await user.type(screen.getByLabelText(/slug/i), "my-api")
    await user.type(screen.getByLabelText(/name/i), "My API")
    await user.type(screen.getByRole("textbox", { name: /^url/i }), "https://api.example.com")
    if (spec) await user.type(screen.getByLabelText(/openapi spec url/i), spec)
    await advance(user)
    await advance(user)
  }

  it("keeps the REST card selectable and shows REST fields", async () => {
    const user = userEvent.setup()
    renderWizard()
    await user.click(screen.getByRole("button", { name: /rest api/i }))
    await advance(user)
    expect(screen.getByLabelText(/openapi spec url/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/slug/i)).toBeInTheDocument()
  })

  it("walks the same steps for REST: details, auth, test", async () => {
    const user = userEvent.setup()
    renderWizard()
    await walkToTest(user)
    expect(screen.getByRole("button", { name: /run test/i })).toBeInTheDocument()
  })

  it("runs the test, shows truncated operations and lets the user filter them", async () => {
    const user = userEvent.setup()
    renderWizard()
    await walkToTest(user)
    await user.click(screen.getByRole("button", { name: /run test/i }))
    expect(await screen.findByText(/reachable/i)).toBeInTheDocument()
    expect(screen.getByText(/getUser — Fetches one user by id/)).toBeInTheDocument()
    expect(screen.queryByText(/must truncate/)).not.toBeInTheDocument()
    await advance(user)
    const checkboxes = screen.getAllByRole("checkbox")
    expect(checkboxes).toHaveLength(2)
    expect(checkboxes[0]).toBeChecked()
    expect(checkboxes[1]).toBeChecked()
    await user.click(checkboxes[1])
    expect(checkboxes[1]).not.toBeChecked()
  })

  it("saves with kind=rest, spec_url and the selected operations", async () => {
    const user = userEvent.setup()
    renderWizard()
    await walkToTest(user, "https://api.example.com/openapi.json")
    await user.click(screen.getByRole("button", { name: /run test/i }))
    await screen.findByText(/reachable/i)
    await advance(user)
    await user.click(screen.getAllByRole("checkbox")[1])
    await advance(user)
    await advance(user)
    await user.click(screen.getByRole("button", { name: /register service/i }))
    const save = calls.find((call) => call.url === "/api/services")
    expect(save).toBeDefined()
    expect(save?.body).toMatchObject({
      kind: "rest",
      slug: "my-api",
      url: "https://api.example.com",
      spec_url: "https://api.example.com/openapi.json",
      tools: ["getUser"],
    })
  })

  it("omits the tools list when every operation stays selected", async () => {
    const user = userEvent.setup()
    renderWizard()
    await walkToTest(user)
    await user.click(screen.getByRole("button", { name: /run test/i }))
    await screen.findByText(/reachable/i)
    await advance(user)
    await advance(user)
    await advance(user)
    await user.click(screen.getByRole("button", { name: /register service/i }))
    const save = calls.find((call) => call.url === "/api/services")
    expect(save?.body).not.toHaveProperty("tools")
  })
})
