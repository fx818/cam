import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "react-router-dom"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, apiFetch } from "@/lib/api"
import { useAuth } from "@/lib/auth"

export interface SkillEntry {
  skill_id: string
  id: string
  tier: string
  slug: string
  name: string
  description: string
  group: string
  requires: string[]
  available: boolean
  unavailable_reason: string | null
  author: string | null
  owner: string | null
  version: number | null
}

export interface SkillListResponse {
  skills: SkillEntry[]
  next_cursor: string | null
}

const ALLOWED_TYPES = new Set(["md", "txt", "json", "yaml", "csv"])

type CreatedSkill = { skill: { skill_id: string; id: string; version: number } }

function slugify(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "")
}

async function filePayload(file: File): Promise<{ path: string; content: string }> {
  const relative = (file as File & { webkitRelativePath: string }).webkitRelativePath
  const content: string = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ""))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(file)
  })
  return { path: relative || file.name, content }
}

function isAllowedPath(path: string): boolean {
  const dot = path.lastIndexOf(".")
  return dot >= 0 && ALLOWED_TYPES.has(path.slice(dot + 1).toLowerCase())
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result ?? "")
      resolve(result.slice(result.indexOf(",") + 1))
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

function entrySubtitle(entry: SkillEntry): string {
  const bits: string[] = [entry.id]
  if (entry.group !== "mine" && entry.author) bits.push(`shared by ${entry.author}`)
  if (entry.version !== null) bits.push(`v${entry.version}`)
  return bits.join(" · ")
}

function UploadDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [tab, setTab] = useState<"editor" | "files" | "zip">("editor")
  const [tier, setTier] = useState<"personal" | "global">("personal")
  const [name, setName] = useState("")
  const [slug, setSlug] = useState("")
  const [description, setDescription] = useState("")
  const [requires, setRequires] = useState("")
  const [body, setBody] = useState("Instructions, steps, references…")
  const [filesPayload, setFilesPayload] = useState<{ path: string; content: string }[]>([])
  const [zipPayload, setZipPayload] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: async (payloadToUse: Record<string, unknown>) =>
      apiFetch<CreatedSkill>("/api/skills", { method: "POST", body: payloadToUse }),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["skills"] })
      setError(null)
      onOpenChange(false)
      navigate(`/skills/${data.skill.id}`)
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : "Could not create the skill."),
  })

  function reset() {
    setName("")
    setSlug("")
    setDescription("")
    setRequires("")
    setBody("Instructions, steps, references…")
    setFilesPayload([])
    setZipPayload(null)
    setError(null)
    setTab("editor")
    setTier("personal")
  }

  function submit() {
    const slugPart = slug.trim() ? { slug: slug.trim() } : {}
    if (zipPayload !== null) {
      create.mutate({ tier, ...slugPart, zip_base64: zipPayload })
    } else if (filesPayload.length > 0) {
      create.mutate({ tier, ...slugPart, files: filesPayload })
    } else if (name.trim() && description.trim()) {
      create.mutate({
        tier,
        ...slugPart,
        name: name.trim(),
        description: description.trim(),
        requires: requires.split(",").map((part) => part.trim()).filter(Boolean),
        body,
      })
    } else {
      setError("Name and description are required.")
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v)
        if (!v) {
          reset()
        }
      }}
    >
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>New skill</DialogTitle>
          <DialogDescription>
            Skills are text only (.md, .txt, .json, .yaml, .csv). A SKILL.md with a name and
            description is required.
          </DialogDescription>
        </DialogHeader>
        <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
          <TabsList>
            <TabsTrigger value="editor">Editor</TabsTrigger>
            <TabsTrigger value="files">Folder</TabsTrigger>
            <TabsTrigger value="zip">Zip</TabsTrigger>
          </TabsList>
        </Tabs>
        {tab === "editor" && (
          <div className="grid gap-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-1">
                <Label htmlFor="skill-name">Name</Label>
                <Input
                  id="skill-name"
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value)
                    if (slug === "") {
                      setSlug(slugify(e.target.value))
                    }
                  }}
                />
              </div>
              {user?.role === "admin" && (
                <div className="grid gap-1">
                  <Label htmlFor="skill-tier">Visibility</Label>
                  <div className="flex gap-4 text-sm">
                    <label className="flex items-center gap-1.5">
                      <input
                        type="radio"
                        name="skill-tier"
                        checked={tier === "personal"}
                        onChange={() => setTier("personal")}
                      />
                      Personal (yours)
                    </label>
                    <label className="flex items-center gap-1.5">
                      <input
                        type="radio"
                        name="skill-tier"
                        checked={tier === "global"}
                        onChange={() => setTier("global")}
                      />
                      Global (everyone)
                    </label>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Global skills appear for every user; visibility can still be restricted later.
                  </p>
                </div>
              )}
              <div className="grid gap-1">
                <Label htmlFor="skill-slug">Slug (optional)</Label>
                <Input
                  id="skill-slug"
                  value={slug}
                  onChange={(e) => setSlug(e.target.value)}
                  placeholder="pns-analytics"
                />
              </div>
            </div>
            <div className="grid gap-1">
              <Label htmlFor="skill-description">Description</Label>
              <Input
                id="skill-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="skill-requires">Requires (service refs, comma separated)</Label>
              <Input
                id="skill-requires"
                value={requires}
                onChange={(e) => setRequires(e.target.value)}
                placeholder="global/pns"
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="skill-body">Instructions</Label>
              <Textarea
                id="skill-body"
                className="h-40 font-mono text-xs"
                value={body}
                onChange={(e) => setBody(e.target.value)}
              />
            </div>
            <div className="rounded-md border bg-muted/40 p-3">
              <p className="text-xs font-medium text-muted-foreground">Preview</p>
              <p className="mt-1 font-medium">{name || "Untitled skill"}</p>
              <p className="text-sm text-muted-foreground">{description}</p>
              {requires.trim() && (
                <p className="mt-1 text-xs text-muted-foreground">requires: {requires}</p>
              )}
              <pre className="mt-2 whitespace-pre-wrap text-xs">{body}</pre>
            </div>
          </div>
        )}
        {tab === "files" && (
          <div className="grid gap-3">
            <input
              type="file"
              multiple
              onChange={async (e) => {
                if (!e.target.files) return
                const items = Array.from(e.target.files)
                setFilesPayload([])
                if (!items.some((f) => f.name === "SKILL.md")) {
                  setError("A SKILL.md file is required.")
                  return
                }
                const bundle = await Promise.all(items.map(filePayload))
                const bad = bundle.some((item) => !isAllowedPath(item.path))
                if (bad) {
                  setError("Only text files (.md, .txt, .json, .yaml, .csv) are accepted.")
                  return
                }
                setError(null)
                setFilesPayload(bundle)
              }}
            />
            <ul className="list-inside list-disc text-xs text-muted-foreground">
              {filesPayload.map((item) => (
                <li key={item.path}>{item.path}</li>
              ))}
            </ul>
          </div>
        )}
        {tab === "zip" && (
          <div className="grid gap-3">
            <input
              type="file"
              accept=".zip"
              onChange={async (e) => {
                const file = e.target.files?.[0]
                if (!file) return
                setZipPayload(await fileToBase64(file))
              }}
            />
            {zipPayload && <p className="text-xs text-muted-foreground">Zip loaded.</p>}
          </div>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={create.isPending} onClick={submit}>
            Create skill
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function SkillsPage() {
  const [uploadOpen, setUploadOpen] = useState(false)
  const { user } = useAuth()
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: () => apiFetch<SkillListResponse>("/api/skills"),
  })
  const skills = data?.skills ?? []
  const groups = [
    { key: "global", title: "Global" },
    { key: "mine", title: "My skills" },
    { key: "shared", title: "Shared with me" },
  ]

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Skills</h1>
          <p className="text-sm text-muted-foreground">
            Reusable instructions agents load next to the tools they use.
          </p>
        </div>
        {user && (
          <Button onClick={() => setUploadOpen(true)}>New skill</Button>
        )}
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load skills."}
        </p>
      )}

      {data && skills.length === 0 && (
        <div className="rounded-md border border-dashed p-6 text-center">
          <p className="text-sm font-medium">No skills yet</p>
          <p className="mt-1 text-sm text-muted-foreground">
            Create one with the editor, a folder upload or a zip bundle.
          </p>
        </div>
      )}

      {groups.map((group) => {
        const entries = skills.filter((entry) => entry.group === group.key)
        if (entries.length === 0) {
          return null
        }
        return (
          <Card key={group.key}>
            <CardHeader>
              <CardTitle className="text-base">{group.title}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {entries.map((entry) => (
                <div
                  key={entry.id}
                  className="flex items-center justify-between gap-4 rounded-md border p-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Link
                        to={`/skills/${entry.id}`}
                        className="truncate font-medium hover:underline"
                      >
                        {entry.name}
                      </Link>
                      <Badge variant="outline">{entry.tier}</Badge>
                      {entry.group !== "mine" && entry.author && (
                        <span className="text-xs text-muted-foreground">
                          by {entry.author}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">
                      {entrySubtitle(entry)}
                    </p>
                    {!entry.available && (
                      <p className="mt-1 text-xs text-destructive">
                        unavailable: {entry.unavailable_reason}
                      </p>
                    )}
                  </div>
                  <Button size="sm" variant="outline" asChild>
                    <Link to={`/skills/${entry.id}`}>Open</Link>
                  </Button>
                </div>
              ))}
            </CardContent>
          </Card>
        )
      })}

      <UploadDialog open={uploadOpen} onOpenChange={setUploadOpen} />
    </div>
  )
}
