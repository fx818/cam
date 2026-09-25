import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError, apiFetch } from "@/lib/api"
import type { ServiceDetailResponse } from "@/lib/types"

export interface ConnectTarget {
  id: string
  name: string
}

function readString(
  config: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const value = config?.[key]
  return typeof value === "string" && value.trim() ? value : null
}

export function redirectTo(url: string): void {
  window.location.assign(url)
}

interface ConnectResult {
  authorize_url?: string
  connection?: { id: string; status: string }
}

const SECRET_HINTS: Record<string, (config: Record<string, unknown> | null) => string> = {
  "static-header": (config) => {
    const headerName = readString(config, "header_name")
    return headerName
      ? `Header name: ${headerName}. CAM sends the header value exactly as entered — include any prefix such as 'Bearer '.`
      : "CAM sends the header value exactly as entered — include any prefix such as 'Bearer '."
  },
  bearer: () =>
    "CAM sends Authorization: Bearer <value>. Enter the token WITHOUT the 'Bearer ' prefix.",
  "api-key": (config) => {
    const keyName = readString(config, "api_key_name")
    const keyIn = readString(config, "api_key_in")
    const location =
      keyIn === "query" ? "the query string" : keyIn === "header" || keyIn ? "a request header" : null
    if (keyName && location) {
      return `Key '${keyName}' is sent in ${location}; the name is fixed on the template. Enter the key value.`
    }
    return "Enter the API key value. CAM sends it under the name fixed on the template."
  },
  basic: (config) => {
    const username = readString(config, "username")
    return username
      ? `Username '${username}' is fixed on the template. Enter the password.`
      : "The username is fixed on the template. Enter the password."
  },
}

export function ConnectDialog({
  target,
  open,
  onOpenChange,
  detail,
}: {
  target: ConnectTarget
  open: boolean
  onOpenChange: (open: boolean) => void
  detail?: { auth_type: string; auth_config: Record<string, unknown> }
}) {
  const queryClient = useQueryClient()
  const [credential, setCredential] = useState("")

  const { data: fetched } = useQuery({
    queryKey: ["service", target.id],
    queryFn: () => apiFetch<ServiceDetailResponse>(`/api/services/${target.id}`),
    enabled: open && detail === undefined,
  })

  const authType = detail?.auth_type ?? fetched?.service.auth_type ?? null
  const authConfig =
    detail?.auth_config ?? fetched?.service.auth_config ?? null

  const connect = useMutation({
    mutationFn: (value: string | undefined) =>
      apiFetch<ConnectResult>(`/api/services/${target.id}/connect`, {
        method: "POST",
        body: value === undefined ? {} : { credential: value },
      }),
    onSuccess: (result) => {
      if (result.authorize_url) {
        redirectTo(result.authorize_url)
        return
      }
      void queryClient.invalidateQueries({ queryKey: ["services"] })
      void queryClient.invalidateQueries({ queryKey: ["service", target.id] })
      setCredential("")
      onOpenChange(false)
    },
  })

  useEffect(() => {
    if (open && authType === "none" && connect.status === "idle" && !connect.isPending) {
      connect.mutate(undefined)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, authType])

  const needsCredential = authType !== null && authType !== "none" && authType !== "oauth"
  const hint = authType !== null ? SECRET_HINTS[authType]?.(authConfig) ?? "Enter the secret value." : null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Connect “{target.name}”</DialogTitle>
          <DialogDescription>
            This connection is yours alone; nothing is shared here.
          </DialogDescription>
        </DialogHeader>
        {needsCredential && authType !== null && (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault()
              connect.mutate(credential)
            }}
          >
            {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
            <div className="space-y-2">
              <Label htmlFor="connect-credential">
                {authType === "basic" ? "Password" : "Value"}
              </Label>
              <Input
                id="connect-credential"
                type="password"
                value={credential}
                onChange={(event) => setCredential(event.target.value)}
              />
            </div>
            {connect.error && (
              <p className="text-xs text-destructive" role="alert">
                {connect.error instanceof ApiError
                  ? connect.error.message
                  : "Could not connect. Try again."}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={connect.isPending || credential.length === 0}>
                {connect.isPending ? "Connecting…" : "Connect"}
              </Button>
            </DialogFooter>
          </form>
        )}
        {authType === "oauth" && (
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              You will be sent to the provider to authorize CAM, then returned here.
            </p>
            {connect.error && (
              <p className="text-xs text-destructive" role="alert">
                {connect.error instanceof ApiError
                  ? connect.error.message
                  : "Could not connect. Try again."}
              </p>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button disabled={connect.isPending} onClick={() => connect.mutate(undefined)}>
                {connect.isPending ? "Connecting…" : "Authorize"}
              </Button>
            </DialogFooter>
          </div>
        )}
        {authType === "none" && (
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {connect.isPending
                ? "Connecting…"
                : connect.error
                  ? "Could not connect."
                  : "This service does not need any credentials."}
            </p>
            {connect.error && (
              <p className="text-xs text-destructive" role="alert">
                {connect.error instanceof ApiError
                  ? connect.error.message
                  : "Could not connect. Try again."}
              </p>
            )}
            <DialogFooter>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Close
              </Button>
              {connect.error && (
                <Button onClick={() => connect.reset()}>Dismiss</Button>
              )}
            </DialogFooter>
          </div>
        )}
        {authType === null && <p className="text-sm text-muted-foreground">Loading…</p>}
      </DialogContent>
    </Dialog>
  )
}
