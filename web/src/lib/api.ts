export const CSRF_COOKIE = "cam_csrf"
export const CSRF_HEADER = "X-CSRF-Token"

export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.code = code
    this.status = status
  }
}

export function readCookie(name: string): string | null {
  const prefix = `${name}=`
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(prefix)) {
      return decodeURIComponent(part.slice(prefix.length))
    }
  }
  return null
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"])

export interface ApiRequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
}

export async function apiFetch<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET"
  const headers: Record<string, string> = {}
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json"
  }
  if (MUTATING.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) {
      headers[CSRF_HEADER] = csrf
    }
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: "include",
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  })
  if (response.status === 204) {
    return undefined as T
  }
  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = null
    }
  }
  if (!response.ok) {
    const envelope = (payload as { error?: { code?: string; message?: string } } | null)?.error
    throw new ApiError(
      envelope?.code ?? "INTERNAL",
      envelope?.message ?? `Request failed with status ${response.status}`,
      response.status,
    )
  }
  return payload as T
}
