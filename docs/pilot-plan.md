# CAM pilot plan (Phase 7.5)

The pilot needs two real teams; this document is the playbook. The owner runs
the meeting and the pilot; nothing here is automated yet.

## Goal

Two teams use CAM daily instead of keeping per-agent MCP configs, for two
weeks. Prove: no blocking, no credential-visibility issues, and produce a first
batch of tool-description improvements mined from audit logs.

## Success criteria (tick at the end of the pilot)

- [ ] Both teams connect Claude Code (and at least one other harness) via the
      dashboard-generated config without engineer hand-holding.
- [ ] No event-loop blocking complaints; load test (`scripts/load_test.py`)
      still green before rollout.
- [ ] Security review (7.1) signed off using `docs/security-review.md` §5
      questions.
- [ ] Agent usage shows up in the audit log with token labels and agent
      names; owners can explain their team's own activity.
- [ ] Every error seen by agents has a clear owner-fix flow (reconnect,
      rotate, share, timeout) — no silent failures recurring.
- [ ] Tool-description improvements from audit review are applied and verified
      by usage change (more successful invokes, fewer retry flails) on at least
      two services.
- [ ] No raw token or credential ever surfaced in dashboards or agent output
      (checked in pilot retrospectives + hygiene tests).

## Enrolling two teams (owner, ~1 hour)

Everything through the dashboard's admin screens, no CLI:

1. **Users and teams** (`/admin/users`): create one person/team account per
   team, e.g. `pns-team@indiamart.com`. Admin sets the initial password and
   hands it over out-of-band.
2. Team members log in, follow `docs/onboarding.md`, connect templates (Notion
   first), and register their personal services via
   `docs/register-a-service.md`.
3. Generate one labelled token per machine/harness (`/agent-setup`), paste a
   snippet per harness per `docs/harness-checks.md`. One token per agent, one
   label each.
4. Register or verify the global services the pilot teams need with the org
   credential in **Global services**; set each service's visibility to the
   specific pilot users before the pilot starts (not "everyone" yet).
5. Upload or author at least one cross-team skill (e.g. a pns-analytics skill
   pointing at `global/pns/run_query`) so skills are exercised too.

## What to gather (weekly)

From the **Activity log** and org audit queries (`GET /api/audit`):

| Signal | Where | What to look for |
| --- | --- | --- |
| Agent usage mix | `agent_name`/`agent_version`, token label | Which harnesses are actually used; token-label hygiene |
| Top services/tools | rows grouped by service & tool | The tools that matter; where descriptions fail |
| Error rate per service | status=error grouped by service | Recurring CONNECTION_EXPIRED (reconnect UX), UPSTREAM_ERROR (weak tools), TIMEOUT (slow tools >55-60 s), CAM_RATE_LIMITED (raise limit?) |
| NOT_FOUND | invocations denied | Wrong tool IDs guessability — an onboarding gap, not a bug |
| Truncated calls | truncated=true | Per-service cap tuning or instructions to filter |
| Latency | latency_ms p50/p95 per service | Services over ~50 s need the job/poll pattern (harness limits) |
| Description-tuning candidates | list_tools then many describe/INVALID_ARGS on a tool | Descriptions that agents misread; candidates for description_override |

Feed table: for each of the above, list (a) service/tool, (b) pattern seen in
the audit sample, (c) proposed description change, (d) date applied. Verify
digit with usage deltas after each change.

## Tool-description tuning workflow

1. Pull the last 7 days of invokes per tool from the Activity log
   (`GET /api/audit?service=&status=...`).
2. Flag tools with any of: error rate > 10 %, repeated INVALID_ARGS on the
   same field, repeated parameter guessing (args missing or padded), or
   describe-before-invoke chains (the agent is fishing for the schema).
3. Read the upstream description; write a better one: one sentence on what it
   does, the **when to use** case, hard input constraints (required fields,
   units, format), and one worked example line.
4. Apply the `description_override` in the Tools tab (owner or admin), keeping
   upstream description intact; the override wins at list time.
5. Ask the pilot team to re-run their usual tasks; compare success rate for
   the tool over the following week. Roll back the override if usage got
   worse.
6. Log every override in this document's pilot notes table so the improvements
   are traceable.

## Feedback template (per pilot member, weekly)

```
Team / person:
Harness + version (claude-code/codex/cursor):
Token label:
What you built with CAM this week:
Worked well:
Failed / confusing (paste agent error text):
Which tools' guides were awkward?:
Next week:
```

## Pilot rhythm

- Week 1: enroll, onboarding, first real invocations; owners watch the audit
  log daily instead of building new features.
- Weeks 2–3: description tuning per the workflow; token hygiene enforced;
  first `docs/security-review.md` sign-off meeting.
- End of pilot: fill the success-criteria checklist in PROGRESS, propose
  Phase 8 metrics (org overview) from the report, and set the rollout list.
