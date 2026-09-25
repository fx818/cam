import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ConnectDialog } from "./ConnectDialog"

type DialogProps = React.ComponentProps<typeof ConnectDialog>

interface Call {
  url: string
  method: string | undefined
  body: string | undefined
}

let calls: Call[]
let assign: ReturnType<typeof vi.fn>

function setup(handler: (call: Call) => { status: number; payload: unknown }) {
  calls = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: Call = {
        url: String(input),
        method: init?.method,
        body: (init?.body as string | undefined) ?? undefined,
      }
      calls.push(call)
      const result = handler(call)
      return {
        ok: result.status < 400,
        status: result.status,
        text: async () => JSON.stringify(result.payload),
      }
    }),
  )
}

function renderDialog(props: Partial<DialogProps> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ConnectDialog
        target={{ id: "global/notion", name: "Notion" }}
        open
        onOpenChange={vi.fn()}
        {...props}
      />
    </QueryClientProvider>,
  )
}

describe("ConnectDialog", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    assign = vi.fn()
    Object.defineProperty(window, "location", {
      value: { ...window.location, assign },
      writable: true,
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("shows a hint and secret input for bearer auth", () => {
    setup(() => ({ status: 200, payload: {} }))
    renderDialog({ detail: { auth_type: "bearer", auth_config: {} } })
    expect(
      screen.getByText(/Enter the token WITHOUT the 'Bearer ' prefix/),
    ).toBeInTheDocument()
    expect(screen.getByLabelText("Value")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Connect" })).toBeDisabled()
  })

  it("submits the credential to the right endpoint for bearer auth", async () => {
    setup((call) =>
      call.method === "POST"
        ? { status: 200, payload: { connection: { id: "c9", status: "connected" } } }
        : { status: 200, payload: {} },
    )
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    renderDialog({
      detail: { auth_type: "bearer", auth_config: {} },
      onOpenChange,
    })
    await user.type(screen.getByLabelText("Value"), "tok123")
    await user.click(screen.getByRole("button", { name: "Connect" }))
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
    expect(onOpenChange).toHaveBeenCalledWith(false)
    const posted = calls.find((call) => call.method === "POST")
    expect(posted?.url).toBe("/api/services/global/notion/connect")
    expect(posted?.body).toBe(JSON.stringify({ credential: "tok123" }))
  })

  it("surfaces API errors under the form", async () => {
    setup((call) =>
      call.method === "POST"
        ? {
            status: 422,
            payload: { error: { code: "VALIDATION_ERROR", message: "credential is required" } },
          }
        : { status: 200, payload: {} },
    )
    const user = userEvent.setup()
    renderDialog({ detail: { auth_type: "static-header", auth_config: {} } })
    await user.type(screen.getByLabelText("Value"), "x")
    await user.click(screen.getByRole("button", { name: "Connect" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("credential is required")
  })

  it("shows the fixed header name for static-header auth", () => {
    setup(() => ({ status: 200, payload: {} }))
    renderDialog({
      detail: {
        auth_type: "static-header",
        auth_config: { header_name: "X-API-Token" },
      },
    })
    expect(screen.getByText(/Header name: X-API-Token/)).toBeInTheDocument()
  })

  it("shows key name and location for api-key auth", () => {
    setup(() => ({ status: 200, payload: {} }))
    renderDialog({
      detail: {
        auth_type: "api-key",
        auth_config: { api_key_name: "key", api_key_in: "header" },
      },
    })
    expect(screen.getByText(/Key 'key' is sent in a request header/)).toBeInTheDocument()
  })

  it("shows the fixed username for basic auth", () => {
    setup(() => ({ status: 200, payload: {} }))
    renderDialog({
      detail: { auth_type: "basic", auth_config: { username: "svc-bot" } },
    })
    expect(screen.getByText(/Username 'svc-bot' is fixed/)).toBeInTheDocument()
    expect(screen.getByLabelText("Password")).toBeInTheDocument()
  })

  it("posts and redirects for oauth", async () => {
    setup((call) =>
      call.method === "POST"
        ? { status: 200, payload: { authorize_url: "https://oauth.example/authorize?state=s1" } }
        : { status: 200, payload: {} },
    )
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    renderDialog({
      detail: { auth_type: "oauth", auth_config: {} },
      onOpenChange,
    })
    await user.click(await screen.findByRole("button", { name: "Authorize" }))
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith("https://oauth.example/authorize?state=s1")
    })
    expect(onOpenChange).not.toHaveBeenCalled()
    const posted = calls.find((call) => call.method === "POST")
    expect(posted?.url).toBe("/api/services/global/notion/connect")
  })

  it("connects immediately for none auth without inputs", async () => {
    setup((call) =>
      call.method === "POST"
        ? { status: 200, payload: { connection: { id: "c1", status: "connected" } } }
        : { status: 200, payload: {} },
    )
    const onOpenChange = vi.fn()
    renderDialog({
      detail: { auth_type: "none", auth_config: {} },
      onOpenChange,
    })
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
    const posted = calls.find((call) => call.method === "POST")
    expect(posted?.body).toBe(JSON.stringify({}))
    expect(screen.queryByLabelText("Value")).not.toBeInTheDocument()
  })
})
