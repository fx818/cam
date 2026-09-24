import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { AuthProvider } from "@/lib/auth"

import { LoginPage } from "./LoginPage"

function renderLogin() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        const message = url.includes("/auth/login")
          ? "Invalid email or password."
          : "Sign in to continue."
        return {
          ok: false,
          status: 401,
          text: async () => JSON.stringify({ error: { code: "UNAUTHORIZED", message } }),
        }
      }),
    )
  })

  it("renders the sign-in form", async () => {
    renderLogin()
    expect(await screen.findByLabelText(/email/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument()
  })

  it("shows validation errors for an empty submit", async () => {
    renderLogin()
    const user = userEvent.setup()
    await user.click(screen.getByRole("button", { name: /sign in/i }))
    expect(await screen.findByText(/valid email address/i)).toBeInTheDocument()
    expect(screen.getByText(/password is required/i)).toBeInTheDocument()
  })

  it("surfaces the server error message on failed login", async () => {
    renderLogin()
    const user = userEvent.setup()
    await screen.findByLabelText(/email/i)
    await user.type(screen.getByLabelText(/email/i), "person@example.com")
    await user.type(screen.getByLabelText(/password/i), "secret")
    await user.click(screen.getByRole("button", { name: /sign in/i }))
    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument()
  })
})
