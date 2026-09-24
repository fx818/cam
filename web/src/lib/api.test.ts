import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError, CSRF_COOKIE, CSRF_HEADER, apiFetch } from "./api"

function setCookie(name: string, value: string) {
  document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`
  if (value) {
    document.cookie = `${name}=${value}; path=/`
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  setCookie(CSRF_COOKIE, "")
})

describe("apiFetch", () => {
  beforeEach(() => {
    setCookie(CSRF_COOKIE, "")
  })

  it("sends credentials and CSRF header on mutating requests", async () => {
    setCookie(CSRF_COOKIE, "csrf-test-value")
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ ok: true }),
    })
    vi.stubGlobal("fetch", fetchMock)

    const result = await apiFetch<{ ok: boolean }>("/api/auth/logout", {
      method: "POST",
      body: {},
    })

    expect(result).toEqual({ ok: true })
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe("/api/auth/logout")
    expect(init.credentials).toBe("include")
    const headers = init.headers as Record<string, string>
    expect(headers[CSRF_HEADER]).toBe("csrf-test-value")
    expect(headers["Content-Type"]).toBe("application/json")
  })

  it("does not send a CSRF header when the cookie is absent", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => "",
    })
    vi.stubGlobal("fetch", fetchMock)

    await apiFetch("/api/auth/me")
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers[CSRF_HEADER]).toBeUndefined()
  })

  it("throws ApiError with code and message from the error envelope", async () => {
    setCookie(CSRF_COOKIE, "csrf-test-value")
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 429,
        text: async () =>
          JSON.stringify({ error: { code: "RATE_LIMITED", message: "Locked out." } }),
      }),
    )

    await expect(apiFetch("/api/auth/login", { method: "POST", body: {} })).rejects.toMatchObject({
      code: "RATE_LIMITED",
      message: "Locked out.",
      status: 429,
    })
    await expect(apiFetch("/api/auth/login", { method: "POST", body: {} })).rejects.toBeInstanceOf(
      ApiError,
    )
  })
})
