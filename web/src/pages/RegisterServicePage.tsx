import { useState } from "react"
import { useNavigate } from "react-router-dom"

import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, apiFetch } from "@/lib/api"
import type { ServiceDetail } from "@/lib/types"

interface Draft {
  kind: "mcp" | "rest"
  slug: string
  name: string
  url: string
  specUrl: string
  transport: string
  description: string
  authType: string
  headerName: string
  credential: string
  apiKeyName: string
  apiKeyIn: string
  username: string
  timeoutMs: string
  responseCap: string
}

const initialDraft: Draft = {
  kind: "mcp",
  slug: "",
  name: "",
  url: "",
  specUrl: "",
  transport: "streamable-http",
  description: "",
  authType: "none",
  headerName: "X-Api-Key",
  credential: "",
  apiKeyName: "X-Api-Key",
  apiKeyIn: "header",
  username: "",
  timeoutMs: "15000",
  responseCap: "8192",
}

const steps = ["Type", "Details", "Auth", "Test", "Tools", "Limits", "Save"] as const

interface TestResult {
  ok: boolean
  tools?: { name: string; description?: string | null }[]
  message?: string
}

const trunc = (text: string, max = 120) => (text.length > max ? `${text.slice(0, max)}…` : text)

export function RegisterServicePage() {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<Draft>(initialDraft)
  const [error, setError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [testRunning, setTestRunning] = useState(false)
  const [selectedTools, setSelectedTools] = useState<string[]>([])
  const [saving, setSaving] = useState(false)

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }))

  const validateStep = (): string | null => {
    if (step === 1) {
      if (!/^[a-z0-9][a-z0-9-]*$/.test(draft.slug))
        return "Slug must be lowercase letters, digits and hyphens."
      if (!draft.name.trim()) return "Name is required."
      if (!/^https?:\/\//.test(draft.url)) return "URL must start with http(s)://."
    }
    if (step === 2) {
      if (draft.authType !== "none" && !draft.credential)
        return "A credential is required for this auth type."
      if (draft.authType === "api-key" && !draft.apiKeyName.trim())
        return "API key auth requires a key name."
      if (draft.authType === "basic" && !draft.username.trim())
        return "Basic auth requires a username."
    }
    if (step === 1 && draft.kind === "rest" && draft.specUrl && !/^https?:\/\//.test(draft.specUrl))
      return "OpenAPI spec URL must start with http(s)://."
    return null
  }

  const next = () => {
    const problem = validateStep()
    if (problem) {
      setError(problem)
      return
    }
    setError(null)
    setStep((prev) => Math.min(prev + 1, steps.length - 1))
  }

  const back = () => {
    setError(null)
    setStep((prev) => Math.max(prev - 1, 0))
  }

  const requestBody = () => {
    const selected = testResult?.tools?.length
      ? selectedTools.filter((name) => testResult.tools?.some((tool) => tool.name === name))
      : []
    const deselected = draft.kind === "rest" && testResult?.tools
      ? testResult.tools.length - selected.length
      : 0
    return {
      kind: draft.kind,
      slug: draft.slug,
      name: draft.name,
      url: draft.url,
      transport: draft.kind === "mcp" ? draft.transport : null,
      spec_url: draft.kind === "rest" && draft.specUrl ? draft.specUrl : null,
      auth_type: draft.authType,
      header_name: draft.authType === "static-header" ? draft.headerName : null,
      credential: draft.authType === "none" ? null : draft.credential,
      api_key_name: draft.authType === "api-key" ? draft.apiKeyName : null,
      api_key_in: draft.authType === "api-key" ? draft.apiKeyIn : null,
      username: draft.authType === "basic" ? draft.username : null,
      tools: deselected > 0 ? selected : undefined,
      description: draft.description,
      timeout_ms: Number(draft.timeoutMs),
      response_cap_tokens: Number(draft.responseCap),
    }
  }

  const runTest = async () => {
    setTestRunning(true)
    setError(null)
    try {
      const result = await apiFetch<TestResult>("/api/services/test", {
        method: "POST",
        body: requestBody(),
      })
      setTestResult(result)
      setSelectedTools((result.tools ?? []).map((tool) => tool.name))
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Test failed. You can continue and save; test later from the service page.",
      )
      setTestResult(null)
    } finally {
      setTestRunning(false)
    }
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const created = await apiFetch<{ service: ServiceDetail }>("/api/services", {
        method: "POST",
        body: requestBody(),
      })
      navigate(`/services/${created.service.id}`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not register service.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Register service</h1>
        <p className="text-sm text-muted-foreground">
          Connect an MCP server or REST API so your agents can use its tools through CAM.
        </p>
      </div>

      <ol className="flex flex-wrap gap-2">
        {steps.map((label, index) => (
          <li key={label}>
            <Badge variant={index === step ? "default" : index < step ? "success" : "muted"}>
              {index + 1}. {label}
            </Badge>
          </li>
        ))}
      </ol>

      {error && <Alert variant="destructive">{error}</Alert>}

      {step === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Service type</CardTitle>
            <CardDescription>How does CAM reach this service?</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <button
              type="button"
              className={`w-full rounded-md border p-4 text-left ${
                draft.kind === "mcp" ? "border-primary" : ""
              }`}
              onClick={() => set("kind", "mcp")}
            >
              <p className="font-medium">MCP server</p>
              <p className="text-sm text-muted-foreground">
                Streamable HTTP or SSE endpoint with an OpenAPI-adjacent tool list.
              </p>
            </button>
            <button
              type="button"
              className={`w-full rounded-md border p-4 text-left ${
                draft.kind === "rest" ? "border-primary" : ""
              }`}
              onClick={() => set("kind", "rest")}
            >
              <p className="font-medium">REST API</p>
              <p className="text-sm text-muted-foreground">
                Register a REST API from its OpenAPI spec; each operation becomes a tool for
                your agents.
              </p>
            </button>
          </CardContent>
        </Card>
      )}

      {step === 1 && (
        <Card>
          <CardHeader>
            <CardTitle>Details</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="space-y-1">
              <Label htmlFor="reg-slug">Slug</Label>
              <Input
                id="reg-slug"
                placeholder="redash"
                value={draft.slug}
                onChange={(event) => set("slug", event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="reg-name">Name</Label>
              <Input
                id="reg-name"
                placeholder="Redash"
                value={draft.name}
                onChange={(event) => set("name", event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="reg-url">URL</Label>
              <Input
                id="reg-url"
                placeholder={
                  draft.kind === "rest" ? "https://api.example.com" : "https://mcp.example.com/mcp"
                }
                value={draft.url}
                onChange={(event) => set("url", event.target.value)}
              />
            </div>
            {draft.kind === "rest" && (
              <div className="space-y-1">
                <Label htmlFor="reg-spec">OpenAPI spec URL (optional)</Label>
                <Input
                  id="reg-spec"
                  placeholder="https://api.example.com/openapi.json"
                  value={draft.specUrl}
                  onChange={(event) => set("specUrl", event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  Leave blank to use the service URL as the spec location.
                </p>
              </div>
            )}
            {draft.kind === "mcp" && (
              <div className="space-y-1">
                <Label>Transport</Label>
                <Select value={draft.transport} onValueChange={(v) => set("transport", v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="streamable-http">streamable-http</SelectItem>
                    <SelectItem value="sse">sse</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
            <div className="space-y-1">
              <Label htmlFor="reg-desc">Description</Label>
              <Textarea
                id="reg-desc"
                rows={3}
                value={draft.description}
                onChange={(event) => set("description", event.target.value)}
              />
            </div>
          </CardContent>
        </Card>
      )}

      {step === 2 && (
        <Card>
          <CardHeader>
            <CardTitle>Authentication</CardTitle>
            <CardDescription>
              Credentials stay server-side; the browser never stores them.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="space-y-1">
              <Label>Type</Label>
              <Select value={draft.authType} onValueChange={(v) => set("authType", v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">None</SelectItem>
                  <SelectItem value="static-header">Static header</SelectItem>
                  <SelectItem value="bearer">Bearer token</SelectItem>
                  <SelectItem value="api-key">API key</SelectItem>
                  <SelectItem value="basic">Basic</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {draft.authType === "static-header" && (
              <div className="space-y-1">
                <Label htmlFor="reg-header">Header name</Label>
                <Input
                  id="reg-header"
                  value={draft.headerName}
                  onChange={(event) => set("headerName", event.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  The credential below is sent as the full value of this header.
                </p>
              </div>
            )}
            {draft.authType === "api-key" && (
              <>
                <div className="space-y-1">
                  <Label htmlFor="reg-apikey-name">API key name</Label>
                  <Input
                    id="reg-apikey-name"
                    value={draft.apiKeyName}
                    onChange={(event) => set("apiKeyName", event.target.value)}
                  />
                </div>
                <div className="space-y-1">
                  <Label>Send in</Label>
                  <Select value={draft.apiKeyIn} onValueChange={(v) => set("apiKeyIn", v)}>
                    <SelectTrigger className="w-40">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="header">header</SelectItem>
                      <SelectItem value="query">query</SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    The credential below is sent as "{draft.apiKeyName}" in the{" "}
                    {draft.apiKeyIn === "query" ? "URL query string" : "HTTP header"}.
                  </p>
                </div>
              </>
            )}
            {draft.authType === "basic" && (
              <div className="space-y-1">
                <Label htmlFor="reg-username">Username</Label>
                <Input
                  id="reg-username"
                  value={draft.username}
                  onChange={(event) => set("username", event.target.value)}
                />
              </div>
            )}
            {draft.authType !== "none" && (
              <div className="space-y-1">
                <Label htmlFor="reg-cred">
                  {draft.authType === "basic" ? "Password" : "Credential"}
                </Label>
                <Input
                  id="reg-cred"
                  type="password"
                  value={draft.credential}
                  onChange={(event) => set("credential", event.target.value)}
                />
                {draft.authType === "bearer" && (
                  <p className="text-xs text-muted-foreground">
                    The value is sent as `Authorization: Bearer &lt;value&gt;`.
                  </p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {step === 3 && (
        <Card>
          <CardHeader>
            <CardTitle>Test connection</CardTitle>
            <CardDescription>
              CAM reaches the endpoint and lists the operations it can call.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Button disabled={testRunning} onClick={() => void runTest()}>
              {testRunning ? "Testing…" : "Run test"}
            </Button>
            {testResult && (
              <div className="space-y-2">
                <Badge variant={testResult.ok ? "success" : "destructive"}>
                  {testResult.ok ? "reachable" : "not verified"}
                </Badge>
                {testResult.message && (
                  <p className="text-sm text-muted-foreground">{testResult.message}</p>
                )}
                {testResult.tools && testResult.tools.length > 0 && (
                  <ul className="list-inside list-disc space-y-1 text-sm text-muted-foreground">
                    {testResult.tools.map((tool) => (
                      <li key={tool.name}>
                        {tool.name}
                        {draft.kind === "rest" && tool.description
                          ? ` — ${trunc(tool.description)}`
                          : null}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {step === 4 && (
        <Card>
          <CardHeader>
            <CardTitle>Tools</CardTitle>
            <CardDescription>
              Pick which tools to expose after the test runs. All discovered tools are
              exposed when no test has run yet; they appear after the first sync.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {testResult?.tools && testResult.tools.length > 0 ? (
              <ul className="space-y-2">
                {testResult.tools.map((tool) => (
                  <li key={tool.name} className="flex items-baseline gap-2">
                    <input
                      type="checkbox"
                      checked={selectedTools.includes(tool.name)}
                      onChange={(event) =>
                        setSelectedTools((prev) =>
                          event.target.checked
                            ? [...prev, tool.name]
                            : prev.filter((name) => name !== tool.name),
                        )
                      }
                    />
                    <span className="text-sm">{tool.name}</span>
                    {draft.kind === "rest" && tool.description && (
                      <span className="text-xs text-muted-foreground">
                        {trunc(tool.description)}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">
                No tool list yet. Continue; tools appear after the first successful test or
                sync.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {step === 5 && (
        <Card>
          <CardHeader>
            <CardTitle>Limits</CardTitle>
          </CardHeader>
          <CardContent className="grid max-w-sm gap-4">
            <div className="space-y-1">
              <Label htmlFor="reg-timeout">Timeout (ms)</Label>
              <Input
                id="reg-timeout"
                inputMode="numeric"
                value={draft.timeoutMs}
                onChange={(event) => set("timeoutMs", event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="reg-cap">Response cap (tokens)</Label>
              <Input
                id="reg-cap"
                inputMode="numeric"
                value={draft.responseCap}
                onChange={(event) => set("responseCap", event.target.value)}
              />
            </div>
          </CardContent>
        </Card>
      )}

      {step === 6 && (
        <Card>
          <CardHeader>
            <CardTitle>Save</CardTitle>
            <CardDescription>Review and register the service.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <dl className="grid grid-cols-3 gap-2 text-sm">
              <dt className="text-muted-foreground">Kind</dt>
              <dd className="col-span-2">{draft.kind}</dd>
              <dt className="text-muted-foreground">Slug</dt>
              <dd className="col-span-2">{draft.slug}</dd>
              <dt className="text-muted-foreground">Name</dt>
              <dd className="col-span-2">{draft.name}</dd>
              <dt className="text-muted-foreground">URL</dt>
              <dd className="col-span-2 break-all">{draft.url}</dd>
              <dt className="text-muted-foreground">Auth</dt>
              <dd className="col-span-2">{draft.authType}</dd>
            </dl>
            <Button disabled={saving} onClick={() => void save()}>
              {saving ? "Saving…" : "Register service"}
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="flex justify-between">
        <Button variant="outline" disabled={step === 0} onClick={back}>
          Back
        </Button>
        {step < steps.length - 1 && <Button onClick={next}>Next</Button>}
      </div>
    </div>
  )
}
