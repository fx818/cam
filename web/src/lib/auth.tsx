import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react"
import type { ReactNode } from "react"

import { apiFetch } from "@/lib/api"
import { resetQueryCache } from "@/lib/queryClient"

export interface SessionUser {
  id: string
  email: string
  handle: string
  display_name: string
  role: string
  kind: string
  status: string
}

export interface SessionOrg {
  id: string
  slug: string
  name: string
  settings: Record<string, unknown>
}

export interface MeResponse {
  user: SessionUser
  org: SessionOrg
  base_url: string
}

/**
 * Identity used in query keys and for cache resets while nobody is signed in.
 * The login screen issues no authenticated queries, so this scope holds no data.
 */
export const ANON_IDENTITY = "anon"

interface AuthContextValue {
  user: SessionUser | null
  org: SessionOrg | null
  baseUrl: string
  loading: boolean
  /** Stable per-session key (`<org id>:<user id>`) that namespaces cached data. */
  identityKey: string
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<SessionUser | null>(null)
  const [org, setOrg] = useState<SessionOrg | null>(null)
  const [baseUrl, setBaseUrl] = useState("")
  const [loading, setLoading] = useState(true)

  const identityKey = useMemo(
    () => (user ? `${org?.id ?? ""}:${user.id}` : ANON_IDENTITY),
    [user, org],
  )

  const applyMe = useCallback((me: MeResponse) => {
    setUser(me.user)
    setOrg(me.org)
    setBaseUrl(me.base_url)
  }, [])

  const clearSession = useCallback(() => {
    setUser(null)
    setOrg(null)
    setBaseUrl("")
  }, [])

  const refresh = useCallback(async () => {
    try {
      const me = await apiFetch<MeResponse>("/api/auth/me")
      applyMe(me)
    } catch {
      // Any failure (including 401) means we are not authenticated.
      clearSession()
    } finally {
      setLoading(false)
    }
  }, [applyMe, clearSession])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Whenever the signed-in identity changes (logout, sign-in as another user,
  // org switch) cancel outstanding requests and drop every cached entry, so one
  // account's data can never be shown for, or written into, another's session.
  // The anonymous -> signed-in transition is skipped: the login screen runs no
  // authenticated queries, so its scope is already empty and clearing there
  // would only abort the freshly started requests of the newly mounted pages.
  const identityRef = useRef(ANON_IDENTITY)
  useEffect(() => {
    const previous = identityRef.current
    identityRef.current = identityKey
    if (previous !== identityKey && previous !== ANON_IDENTITY) {
      void resetQueryCache()
    }
  }, [identityKey])

  const login = useCallback(
    async (email: string, password: string) => {
      const me = await apiFetch<MeResponse>("/api/auth/login", {
        method: "POST",
        body: { email, password },
      })
      applyMe(me)
    },
    [applyMe],
  )

  const logout = useCallback(async () => {
    try {
      await apiFetch<void>("/api/auth/logout", { method: "POST" })
    } finally {
      // Cancel in-flight requests and clear caches before the identity flips so
      // a late response cannot repopulate the previous account's data.
      await resetQueryCache()
      clearSession()
    }
  }, [clearSession])

  const value = useMemo(
    () => ({ user, org, baseUrl, loading, identityKey, login, logout, refresh }),
    [user, org, baseUrl, loading, identityKey, login, logout, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) {
    throw new Error("useAuth must be used inside AuthProvider")
  }
  return value
}

/**
 * The current identity scope for query keys. Unlike `useAuth` this does not
 * throw when there is no provider, so isolated component tests (which render a
 * page without `AuthProvider`) still get a stable `ANON_IDENTITY` scope.
 */
export function useIdentityKey(): string {
  const value = useContext(AuthContext)
  return value?.identityKey ?? ANON_IDENTITY
}
