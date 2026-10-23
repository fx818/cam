import { QueryClientProvider } from "@tanstack/react-query"
import { act, renderHook, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { AuthProvider, useAuth } from "./auth"
import { queryClient, resetQueryCache } from "./queryClient"

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
  }
}

function userResponse(id: string, email: string) {
  return {
    user: {
      id,
      email,
      handle: id,
      display_name: id,
      role: "user",
      kind: "person",
      status: "active",
    },
    org: { id: "o1", slug: "acme", name: "Acme", settings: {} },
    base_url: "http://cam.test",
  }
}

function wrapper({ children }: { children: ReactNode }) {
  // Mirror production wiring (main.tsx): the app's singleton client backs the
  // provider, and AuthProvider resets that same client.
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  )
}

describe("auth query-cache isolation", () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    queryClient.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("drops every cached query when the user signs out", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes("/api/auth/me")) return jsonResponse(userResponse("u1", "a@example.com"))
        if (url.includes("/api/auth/logout")) {
          return { ok: true, status: 204, text: async () => "" }
        }
        return jsonResponse({})
      }),
    )

    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.user?.id).toBe("u1"))
    expect(result.current.identityKey).toBe("o1:u1")

    // Simulate pages having populated the cache for the signed-in account.
    act(() => {
      queryClient.setQueryData(["services", "o1:u1"], { services: [{ id: "secret" }] })
      queryClient.setQueryData(["audit", {}, "o1:u1"], { entries: [] })
    })
    expect(queryClient.getQueryCache().getAll()).toHaveLength(2)

    await act(async () => {
      await result.current.logout()
    })

    expect(result.current.user).toBeNull()
    expect(queryClient.getQueryData(["services", "o1:u1"])).toBeUndefined()
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0)
  })

  it("clears the cache and renamespaces when a different user signs in", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes("/api/auth/me")) return jsonResponse(userResponse("u1", "a@example.com"))
        if (url.includes("/api/auth/login")) {
          return jsonResponse(userResponse("u2", "b@example.com"))
        }
        return jsonResponse({})
      }),
    )

    const { result } = renderHook(() => useAuth(), { wrapper })
    await waitFor(() => expect(result.current.user?.id).toBe("u1"))

    act(() => {
      queryClient.setQueryData(["services", "o1:u1"], { services: [{ id: "user-a" }] })
    })

    await act(async () => {
      await result.current.login("b@example.com", "pw")
    })

    expect(result.current.user?.id).toBe("u2")
    expect(result.current.identityKey).toBe("o1:u2")
    expect(queryClient.getQueryData(["services", "o1:u1"])).toBeUndefined()
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0)
  })

  it("cancels in-flight requests so they cannot repopulate the cache after a reset", async () => {
    let resolveFetch!: (value: unknown) => void
    const pending = new Promise<unknown>((resolve) => {
      resolveFetch = resolve
    })

    const inFlight = queryClient
      .fetchQuery({ queryKey: ["services", "o1:u1"], queryFn: () => pending })
      .catch(() => undefined)

    await resetQueryCache()
    expect(queryClient.getQueryData(["services", "o1:u1"])).toBeUndefined()

    // The request settles only after the reset — its data must be discarded.
    resolveFetch({ services: [{ id: "leaked" }] })
    await inFlight

    expect(queryClient.getQueryData(["services", "o1:u1"])).toBeUndefined()
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0)
  })
})
