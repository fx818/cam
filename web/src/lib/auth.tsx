import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react"
import type { ReactNode } from "react"

import { ApiError, apiFetch } from "@/lib/api"

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

interface AuthContextValue {
  user: SessionUser | null
  org: SessionOrg | null
  baseUrl: string
  loading: boolean
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

  const applyMe = useCallback((me: MeResponse) => {
    setUser(me.user)
    setOrg(me.org)
    setBaseUrl(me.base_url)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const me = await apiFetch<MeResponse>("/api/auth/me")
      applyMe(me)
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setUser(null)
        setOrg(null)
      } else {
        setUser(null)
        setOrg(null)
      }
    } finally {
      setLoading(false)
    }
  }, [applyMe])

  useEffect(() => {
    void refresh()
  }, [refresh])

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
      setUser(null)
      setOrg(null)
    }
  }, [])

  const value = useMemo(
    () => ({ user, org, baseUrl, loading, login, logout, refresh }),
    [user, org, baseUrl, loading, login, logout, refresh],
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
