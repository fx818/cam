import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ApiError, apiFetch } from "@/lib/api"
import { useIdentityKey } from "@/lib/auth"
import type {
  AdminUser,
  AdminUsersResponse,
  AuditEntry,
  TokenRow,
} from "@/lib/types"

function statusVariant(status: string): "success" | "destructive" | "muted" {
  return status === "active" ? "success" : "destructive"
}

function UserDetailDialog({
  user,
  open,
  onOpenChange,
}: {
  user: AdminUser | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const identity = useIdentityKey()
  const tokens = useQuery({
    queryKey: ["admin-user-tokens", user?.id, identity],
    enabled: open && user !== null,
    queryFn: () =>
      apiFetch<{ tokens: TokenRow[] }>(`/api/admin/users/${user?.id}/tokens`),
  })
  const activity = useQuery({
    queryKey: ["admin-user-activity", user?.id, identity],
    enabled: open && user !== null,
    queryFn: () =>
      apiFetch<{ entries: AuditEntry[]; next_cursor: string | null }>(
        `/api/admin/users/${user?.id}/activity`,
      ),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{user?.display_name}</DialogTitle>
          <DialogDescription>{user?.email}</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="tokens">
          <TabsList>
            <TabsTrigger value="tokens">Tokens</TabsTrigger>
            <TabsTrigger value="activity">Activity</TabsTrigger>
          </TabsList>
          <TabsContent value="tokens" className="mt-3">
            {tokens.isLoading && (
              <p className="text-sm text-muted-foreground">Loading…</p>
            )}
            {tokens.data && tokens.data.tokens.length === 0 && (
              <p className="text-sm text-muted-foreground">No tokens.</p>
            )}
            {tokens.data && tokens.data.tokens.length > 0 && (
              <ul className="space-y-1 text-sm">
                {tokens.data.tokens.map((token) => (
                  <li key={token.id} className="flex justify-between gap-2">
                    <span>{token.label}</span>
                    <span className="font-mono text-xs text-muted-foreground">
                      cam_{token.token_prefix}…
                      {token.revoked_at ? " (revoked)" : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </TabsContent>
          <TabsContent value="activity" className="mt-3">
            {activity.isLoading && (
              <p className="text-sm text-muted-foreground">Loading…</p>
            )}
            {activity.data && activity.data.entries.length === 0 && (
              <p className="text-sm text-muted-foreground">No activity.</p>
            )}
            {activity.data && activity.data.entries.length > 0 && (
              <ul className="space-y-1 text-sm">
                {activity.data.entries.map((entry) => (
                  <li key={entry.id} className="flex justify-between gap-2">
                    <span>
                      {entry.ts ? new Date(entry.ts).toLocaleString() : "—"} ·{" "}
                      {entry.action}
                    </span>
                    <span className="text-muted-foreground">
                      {entry.service_ref ?? "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

export function AdminUsersPage() {
  const queryClient = useQueryClient()
  const identity = useIdentityKey()
  const [createOpen, setCreateOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<AdminUser | null>(null)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [deactivateTarget, setDeactivateTarget] = useState<AdminUser | null>(null)
  const [detailTarget, setDetailTarget] = useState<AdminUser | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const [newEmail, setNewEmail] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [newRole, setNewRole] = useState("user")
  const [newKind, setNewKind] = useState("person")
  const [newName, setNewName] = useState("")
  const [editName, setEditName] = useState("")
  const [editRole, setEditRole] = useState("user")
  const [editKind, setEditKind] = useState("person")
  const [resetPassword, setResetPassword] = useState("")

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin-users", identity],
    queryFn: () => apiFetch<AdminUsersResponse>("/api/admin/users"),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["admin-users"] })
  }

  const fail = (err: unknown) =>
    setMessage(err instanceof ApiError ? err.message : "Request failed.")

  const createMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ user: AdminUser }>("/api/admin/users", {
        method: "POST",
        body: {
          email: newEmail.trim(),
          password: newPassword,
          role: newRole,
          kind: newKind,
          display_name: newName.trim() || null,
        },
      }),
    onSuccess: () => {
      setCreateOpen(false)
      setNewEmail("")
      setNewPassword("")
      setNewName("")
      setMessage(null)
      invalidate()
    },
    onError: fail,
  })

  const editMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ user: AdminUser }>(`/api/admin/users/${editTarget?.id}`, {
        method: "PATCH",
        body: {
          display_name: editName.trim(),
          role: editRole,
          kind: editKind,
        },
      }),
    onSuccess: () => {
      setEditTarget(null)
      setMessage(null)
      invalidate()
    },
    onError: fail,
  })

  const resetMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ user: AdminUser }>(
        `/api/admin/users/${resetTarget?.id}/reset-password`,
        { method: "POST", body: { password: resetPassword } },
      ),
    onSuccess: () => {
      setResetTarget(null)
      setResetPassword("")
      setMessage("Password reset.")
      invalidate()
    },
    onError: fail,
  })

  const deactivateMutation = useMutation({
    mutationFn: (userId: string) =>
      apiFetch<{ user: AdminUser }>(`/api/admin/users/${userId}/deactivate`, {
        method: "POST",
      }),
    onSuccess: () => {
      setDeactivateTarget(null)
      setMessage(null)
      invalidate()
    },
    onError: fail,
  })

  const users = data?.users ?? []

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Users and teams</h1>
          <p className="text-sm text-muted-foreground">
            Admin-created accounts for your org. Team accounts use kind “team”.
          </p>
        </div>
        <Button onClick={() => { setMessage(null); setCreateOpen(true) }}>
          Create user
        </Button>
      </div>

      {message && <p className="text-sm text-muted-foreground">{message}</p>}
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load users."}
        </p>
      )}

      {data && (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Handle</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Kind</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((user) => (
                <TableRow key={user.id}>
                  <TableCell className="text-sm">{user.email}</TableCell>
                  <TableCell className="text-sm">{user.display_name}</TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {user.handle}
                  </TableCell>
                  <TableCell>
                    <Badge variant={user.role === "admin" ? "default" : "outline"}>
                      {user.role}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">{user.kind}</TableCell>
                  <TableCell>
                    <Badge variant={statusVariant(user.status)}>{user.status}</Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setDetailTarget(user)}
                      >
                        Details
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setMessage(null)
                          setEditName(user.display_name)
                          setEditRole(user.role)
                          setEditKind(user.kind)
                          setEditTarget(user)
                        }}
                      >
                        Edit
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setMessage(null)
                          setResetPassword("")
                          setResetTarget(user)
                        }}
                      >
                        Reset
                      </Button>
                      {user.status === "active" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setDeactivateTarget(user)}
                        >
                          Deactivate
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create user</DialogTitle>
            <DialogDescription>
              Accounts are created by admins; there is no self sign-up.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="space-y-1">
              <Label htmlFor="cu-email">Email</Label>
              <Input
                id="cu-email"
                type="email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="cu-password">Temporary password (min 8)</Label>
              <Input
                id="cu-password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="cu-name">Display name</Label>
              <Input
                id="cu-name"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>Role</Label>
                <Select value={newRole} onValueChange={setNewRole}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="user">user</SelectItem>
                    <SelectItem value="admin">admin</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label>Kind</Label>
                <Select value={newKind} onValueChange={setNewKind}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="person">person</SelectItem>
                    <SelectItem value="team">team</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={
                createMutation.isPending || !newEmail.trim() || newPassword.length < 8
              }
              onClick={() => createMutation.mutate()}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editTarget !== null} onOpenChange={(o) => !o && setEditTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit {editTarget?.email}</DialogTitle>
          </DialogHeader>
          <div className="grid gap-3">
            <div className="space-y-1">
              <Label htmlFor="eu-name">Display name</Label>
              <Input
                id="eu-name"
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label>Role</Label>
                <Select value={editRole} onValueChange={setEditRole}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="user">user</SelectItem>
                    <SelectItem value="admin">admin</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label>Kind</Label>
                <Select value={editKind} onValueChange={setEditKind}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="person">person</SelectItem>
                    <SelectItem value="team">team</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditTarget(null)}>
              Cancel
            </Button>
            <Button
              disabled={editMutation.isPending || !editName.trim()}
              onClick={() => editMutation.mutate()}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={resetTarget !== null} onOpenChange={(o) => !o && setResetTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reset password</DialogTitle>
            <DialogDescription>
              Set a new password for {resetTarget?.email}.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="rp-password">New password (min 8)</Label>
            <Input
              id="rp-password"
              type="password"
              value={resetPassword}
              onChange={(e) => setResetPassword(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setResetTarget(null)}>
              Cancel
            </Button>
            <Button
              disabled={resetMutation.isPending || resetPassword.length < 8}
              onClick={() => resetMutation.mutate()}
            >
              Reset
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={deactivateTarget !== null}
        onOpenChange={(o) => !o && setDeactivateTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Deactivate {deactivateTarget?.email}?</DialogTitle>
            <DialogDescription>
              Their tokens are revoked, connections disabled and sessions closed. The row
              is kept for audit history.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeactivateTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deactivateMutation.isPending}
              onClick={() =>
                deactivateTarget && deactivateMutation.mutate(deactivateTarget.id)
              }
            >
              Deactivate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <UserDetailDialog
        user={detailTarget}
        open={detailTarget !== null}
        onOpenChange={(o) => !o && setDetailTarget(null)}
      />
    </div>
  )
}
