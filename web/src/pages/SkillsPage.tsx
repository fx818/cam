import { useQuery } from "@tanstack/react-query"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ApiError, apiFetch } from "@/lib/api"
import type { SkillsResponse } from "@/lib/types"

export function SkillsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: () => apiFetch<SkillsResponse>("/api/skills"),
  })

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Skills</h1>
        <p className="text-sm text-muted-foreground">
          Reusable instructions agents load next to the tools they use.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Browse skills</CardTitle>
          <CardDescription>
            Global and personal skills will appear here once skills ship.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {error && (
            <p className="text-sm text-destructive">
              {error instanceof ApiError ? error.message : "Could not load skills."}
            </p>
          )}
          {data && data.skills.length === 0 && (
            <div className="rounded-md border border-dashed p-6 text-center">
              <p className="text-sm font-medium">No skills yet</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Skills arrive with Phase 6: upload, edit with preview, version history and
                sharing.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
