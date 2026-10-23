import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate, Link, useParams } from "react-router-dom"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, apiFetch } from "@/lib/api"
import { useIdentityKey } from "@/lib/auth"
import type { SkillEntry } from "@/pages/SkillsPage"

interface SkillFileData {
  path: string
  content: string
  bytes: number
}

interface SkillVersionRow {
  id: string
  version: number
  notes: string | null
  created_at: string | null
  created_by_name: string | null
  created_by_email: string | null
}

interface SkillViewer {
  is_owner: boolean
  can_manage: boolean
}

export interface SkillDetail extends SkillEntry {
  visibility: string
  version: number | null
  created_at: string | null
  files: SkillFileData[]
  viewer: SkillViewer
}

export interface SkillDetailResponse {
  skill: SkillDetail
  versions: SkillVersionRow[]
}

interface DiffResponse {
  path: string
  from_version: number
  to_version: number
  diff: string[]
  truncated: boolean
}

interface SkillShareRow {
  id: string
  created_at: string | null
  revoked_at: string | null
  email: string | null
  display_name: string | null
}

export interface SaveSkillBody {
  name?: string
  description?: string
  requires: string[]
  body: string
  files?: { path: string; content: string }[]
  notes?: string
}

export function bodyOf(skillMd: string): string {
  return skillMd.replace(/^---[\s\S]*?---\n*/, "")
}

export function skillsPath(ref: string): string {
  return `/api/skills/${ref}`
}

export function SkillDetailPage() {
  const params = useParams()
  const ref = `${params.owner}/${params.slug}`
  const queryClient = useQueryClient()
  const identity = useIdentityKey()
  const [tab, setTab] = useState("preview")
  const [editedName, setEditedName] = useState<string | null>(null)
  const [editedDescription, setEditedDescription] = useState<string | null>(null)
  const [editedBody, setEditedBody] = useState<string | null>(null)
  const [diffVersion, setDiffVersion] = useState<number | null>(null)
  const [shareEmail, setShareEmail] = useState("")
  const [actionError, setActionError] = useState<string | null>(null)

  const detail = useQuery({
    queryKey: ["skill", ref, identity],
    queryFn: () => apiFetch<SkillDetailResponse>(skillsPath(ref)),
  })

  const skill = detail.data?.skill
  const skillMd = (skill?.files ?? []).find((file) => file.path === "SKILL.md")

  const navigate = useNavigate()
  const [deleteOpen, setDeleteOpen] = useState(false)

  const deleteSkill = useMutation({
    mutationFn: async () => apiFetch(skillsPath(ref), { method: "DELETE" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["skills"] })
      navigate("/skills")
    },
    onError: (err) => {
      setDeleteOpen(false)
      setActionError(err instanceof ApiError ? err.message : "Could not delete the skill.")
    },
  })

  const save = useMutation({
    mutationFn: async (body: SaveSkillBody) =>
      apiFetch<{ version: { id: string; version: number } }>(
        `${skillsPath(ref)}/versions`,
        { method: "POST", body },
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["skill", ref] })
      await queryClient.invalidateQueries({ queryKey: ["skills"] })
      setActionError(null)
      setEditedName(null)
      setEditedDescription(null)
      setEditedBody(null)
    },
    onError: (err) =>
      setActionError(err instanceof ApiError ? err.message : "Could not save the skill."),
  })

  const rollback = useMutation({
    mutationFn: async (version: number) =>
      apiFetch<{ version: { id: string; version: number } }>(
        `${skillsPath(ref)}/rollback`,
        { method: "POST", body: { version } },
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["skill", ref] })
      await queryClient.invalidateQueries({ queryKey: ["skills"] })
      setActionError(null)
      setTab("history")
    },
    onError: (err) =>
      setActionError(err instanceof ApiError ? err.message : "Rollback failed."),
  })

  const diff = useQuery({
    queryKey: ["skill-diff", ref, diffVersion, identity],
    queryFn: () =>
      apiFetch<DiffResponse>(
        `${skillsPath(ref)}/diff?from_version=${String(diffVersion)}`,
      ),
    enabled: diffVersion !== null,
  })

  const shares = useQuery({
    queryKey: ["skill-shares", ref, identity],
    queryFn: () => apiFetch<{ shares: SkillShareRow[] }>(`${skillsPath(ref)}/shares`),
    enabled: skill?.viewer.is_owner === true,
  })

  function resetError() {
    setActionError(null)
  }

  if (detail.isLoading) {
    return <p className="p-6 text-sm text-muted-foreground">Loading…</p>
  }
  if (detail.error || !(skill && skillMd)) {
    return (
      <p className="p-6 text-sm text-destructive">
        {detail.error instanceof ApiError ? detail.error.message : "Skill not found."}
      </p>
    )
  }

  const shownName = editedName ?? skill.name
  const shownDescription = editedDescription ?? skill.description
  const shownBody = editedBody ?? bodyOf(skillMd.content)
  const canManage = skill.viewer.can_manage

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            {skill.name}
            <Badge variant="outline">{skill.tier}</Badge>
            {skill.available ? (
              <Badge variant="success">available</Badge>
            ) : (
              <Badge variant="warning">unavailable</Badge>
            )}
          </h1>
          <p className="text-sm text-muted-foreground">
            {skill.id} · v{skill.version ?? "-"}
            {skill.group !== "mine" && skill.author
              ? ` · shared by ${skill.author}`
              : ""}
          </p>
          {!skill.available && (
            <p className="mt-1 text-sm text-destructive">
              unavailable: {skill.unavailable_reason}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {canManage && (
            <Button
              variant="destructive"
              onClick={() => setDeleteOpen(true)}
              aria-label={`Delete ${skill.name}`}
            >
              Delete skill
            </Button>
          )}
          <Button variant="outline" asChild>
            <Link to="/skills">All skills</Link>
          </Button>
        </div>
      </div>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{skill.name}”?</DialogTitle>
            <DialogDescription>
              Removes the skill and its version history for this workspace. Agents stop seeing it
              on their next tools/list. Audit history is kept.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" disabled={deleteSkill.isPending} onClick={() => deleteSkill.mutate()}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Tabs value={tab} onValueChange={(value) => setTab(value)}>
        <TabsList>
          <TabsTrigger value="preview">Preview</TabsTrigger>
          {canManage && <TabsTrigger value="edit">Edit</TabsTrigger>}
          <TabsTrigger value="history">History</TabsTrigger>
          {skill.tier === "personal" && skill.viewer.is_owner && (
            <TabsTrigger value="sharing">Sharing</TabsTrigger>
          )}
          {skill.tier === "global" && canManage && (
            <TabsTrigger value="settings">Settings</TabsTrigger>
          )}
        </TabsList>

        <TabsContent value="preview" className="space-y-2">
          <pre className="whitespace-pre-wrap rounded-md border p-4 text-xs">
            {skillMd.content}
          </pre>
          {skill.requires.length > 0 && (
            <p className="text-xs text-muted-foreground">
              requires: {skill.requires.join(", ")}
            </p>
          )}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Reference files</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {skill.files
                .filter((file) => file.path !== "SKILL.md")
                .map((file) => (
                  <details key={file.path} className="rounded-md border p-3">
                    <summary className="cursor-pointer text-sm font-medium">
                      {file.path} ({file.bytes} bytes)
                    </summary>
                    <pre className="mt-2 whitespace-pre-wrap text-xs">{file.content}</pre>
                  </details>
                ))}
              {skill.files.filter((file) => file.path !== "SKILL.md").length === 0 && (
                <p className="text-sm text-muted-foreground">No reference files.</p>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {canManage && (
          <TabsContent value="edit" className="grid gap-3">
            <div className="grid gap-1">
              <Label htmlFor="edit-name">Name</Label>
              <Input
                id="edit-name"
                value={shownName}
                onChange={(e) => setEditedName(e.target.value)}
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="edit-description">Description</Label>
              <Input
                id="edit-description"
                value={shownDescription}
                onChange={(e) => setEditedDescription(e.target.value)}
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="edit-body">Instructions</Label>
              <Textarea
                id="edit-body"
                className="h-48 font-mono text-xs"
                value={shownBody}
                onChange={(e) => setEditedBody(e.target.value)}
              />
            </div>
            {actionError && tab === "edit" && (
              <p className="text-sm text-destructive">{actionError}</p>
            )}
            <div className="flex gap-2">
              <Button
                disabled={
                  editedName === null &&
                  editedDescription === null &&
                  editedBody === null
                }
                onClick={() => {
                  resetError()
                  save.mutate({
                    name: skill.name,
                    description: skill.description,
                    requires: skill.requires,
                    body: shownBody,
                    ...(editedName !== null ? { name: shownName } : {}),
                    ...(editedDescription !== null
                      ? { description: shownDescription }
                      : {}),
                  })
                }}
              >
                Save as new version
              </Button>
              <Button
                variant="outline"
                onClick={() => {
                  setEditedName(null)
                  setEditedDescription(null)
                  setEditedBody(null)
                }}
              >
                Reset
              </Button>
            </div>
          </TabsContent>
        )}

        <TabsContent value="history" className="grid gap-2">
          {(detail.data?.versions ?? []).map((versionRow) => (
            <div
              key={versionRow.id}
              className="flex items-center justify-between gap-4 rounded-md border p-3"
            >
              <div className="min-w-0">
                <p className="font-medium">
                  v{versionRow.version}
                  {versionRow.version === skill.version && (
                    <span className="text-xs text-muted-foreground"> (current)</span>
                  )}
                </p>
                <p className="text-xs text-muted-foreground">
                  {versionRow.created_at?.slice(0, 19).replace("T", " ") ?? ""}
                  {versionRow.created_by_name
                    ? ` · by ${versionRow.created_by_name}`
                    : ""}
                  {versionRow.notes ? ` · ${versionRow.notes}` : ""}
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setDiffVersion(versionRow.version)}
                >
                  Diff against current
                </Button>
                {canManage && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={rollback.isPending}
                    onClick={() => rollback.mutate(versionRow.version)}
                  >
                    Rollback to this
                  </Button>
                )}
              </div>
            </div>
          ))}
          {diffVersion !== null && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center justify-between text-base">
                  <span>
                    Diff: v{diff.data?.from_version ?? diffVersion} →
                    v{diff.data?.to_version ?? "…"}
                  </span>
                  <Button size="sm" variant="outline" onClick={() => setDiffVersion(null)}>
                    Close
                  </Button>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="max-h-96 overflow-auto whitespace-pre text-xs">
                  {diff.data?.diff.join("\n") ?? "Loading diff…"}
                </pre>
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {skill.viewer.is_owner && (
          <TabsContent value="sharing" className="grid gap-3">
            <ul className="grid gap-2">
              {(shares.data?.shares ?? [])
                .filter((share) => share.revoked_at === null)
                .map((share) => (
                  <li key={share.id} className="rounded-md border p-3 text-sm">
                    {share.display_name ?? share.email}
                    {share.created_at
                      ? ` · shared ${share.created_at.slice(0, 10)}`
                      : ""}
                  </li>
                ))}
              {(shares.data?.shares ?? []).filter((share) => share.revoked_at === null)
                .length === 0 && (
                <p className="text-sm text-muted-foreground">Not shared with anyone yet.</p>
              )}
            </ul>
            <div className="flex items-end gap-2">
              <div className="grid gap-1">
                <Label htmlFor="share-email">Share with (email)</Label>
                <Input
                  id="share-email"
                  value={shareEmail}
                  onChange={(e) => setShareEmail(e.target.value)}
                />
              </div>
              <Button
                disabled={shareEmail.trim() === ""}
                onClick={async () => {
                  try {
                    await apiFetch(`${skillsPath(ref)}/shares`, {
                      method: "POST",
                      body: { email: shareEmail.trim() },
                    })
                    setShareEmail("")
                    resetError()
                    await shares.refetch()
                  } catch (err) {
                    setActionError(
                      err instanceof ApiError
                        ? err.message
                        : "Could not share the skill.",
                    )
                  }
                }}
              >
                Share
              </Button>
            </div>
          </TabsContent>
        )}

        {skill.tier === "global" && canManage && (
          <TabsContent value="settings" className="grid gap-3">
            <div className="grid gap-1">
              <Label htmlFor="skill-visibility">Visibility</Label>
              <select
                id="skill-visibility"
                className="rounded-md border bg-transparent p-2 text-sm"
                defaultValue={skill.visibility}
                onChange={async (e) => {
                  try {
                    await apiFetch(skillsPath(ref), {
                      method: "PATCH",
                      body: { visibility: e.target.value },
                    })
                    resetError()
                    await detail.refetch()
                  } catch (err) {
                    setActionError(
                      err instanceof ApiError
                        ? err.message
                        : "Could not update visibility.",
                    )
                  }
                }}
              >
                <option value="all">everyone</option>
                <option value="restricted">specific users (admin)</option>
              </select>
            </div>
          </TabsContent>
        )}
      </Tabs>

      {actionError && tab !== "edit" && (
        <p className="text-sm text-destructive">{actionError}</p>
      )}
    </div>
  )
}
