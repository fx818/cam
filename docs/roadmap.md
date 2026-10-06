# CAM roadmap — post-launch feature plan

Ideas consolidated from a 4-lens brainstorm (agent experience, admin/ops, security,
scope expansion) after Phase 6. Ordered by value-per-cost. Every item that touches
a locked decision or the `cam` contract needs explicit owner sign-off before its
phase starts (AGENTS.md stop-and-ask rule).

Build in plan order within each wave; waves sequence after Phase 7.5 (pilot).

Legend: S ≤ 1 day · M ≤ 3 days · L longer. `§` = needs owner sign-off.

## Wave 1 — agent effectiveness + admin attention (build first, no spec changes)

Pure additive features; no locked decision touched, no contract change.

1. **Usage notes per tool** (S)
   - New nullable `usage_note` on `tools`; editable in the tool tab; returned when
     listed for `describe` and as a hint in `list_tools` (truncation-safe).
   - Done when: note set via API/UI shows in both agent actions; unset is absent;
     audit unchanged.
2. **Self-healing error enrichment** (S)
   - `INVALID_ARGS`: include failing field name, expected type, offending schema
     fragment. `NOT_FOUND` (tool): suggest the closest tool ID from the caller's
     **already access-filtered** reachable set only — never from the global
     catalog (opacity rule).
   - Done when: unit tests pin suggestion filtering (a forbidden tool is never
     suggested); messages stay single-line, no secret/args echo.
3. **Example invocation in `describe`** (M)
   - One schema-derived sample arg set per tool, generated at import (global +
     REST paths), curatable via the existing override machinery. Manual sample in
     the tool tab for REST hand tools.
   - Done when: import generates valid-against-schema samples (property test);
     agents get `example` in `describe`; display uses the curated override when
     one is set, the generated sample otherwise.
4. **Admin "Needs attention" digest** (M)
   - One Overview pane: review-queue items with age, connections
     `needs_reconnect` (with owner), credentials stale per threshold, services
     `failing` > 24 h, users with no token / no first call (activation tracker).
   - Dashboard-only (no email). Thresholds live in `settings`/`orgs.settings`,
     not code.
   - Done when: pane queries return each category; stale-credential threshold
     configurable per org; end-to-end test seeds states and asserts the list.
5. **Credential hygiene + rotation reminders** (S)
   - Surface `connections.expires_at`, days-since-rotation, last-rotated per
     connection; nudge job + row in the digest. Remind-only (no auto-disable).
   - Done when: weekly job emits digest rows; rotation updates the timestamp.
6. **Tool "Try it" sandbox** (M)
   - Tool page: JSON-schema-guided arg editor + one-click invoke through the
     normal pipeline **as the admin user** (runs their own access; audited as
     `invoke`; nothing stored beyond audit).
   - Done when: invoke goes through `access/` resolution (no shortcut), audit
     row shows the admin as caller; NOT_FOUND opacity preserved for non-admins
     (endpoint stays admin-scoped).
7. **"Explain this failure" replay** (M)
   - Audit row detail: read-only re-run of resolve → access → path stages with a
     per-step report, labeled "as of now" (not time-travel). Admin-only.
   - Done when: each of the ten pipeline stages reports its outcome; no upstream
     call; no audit write for the replay itself.
8. **Audit forensics for admins** (S–M)
   - Activity log: token filter, agent/clientInfo filter, free-text args search
     (JSONB GIN), CSV/JSON export. Add `(token_id, ts)` and `(agent_name, ts)`
     indexes; retention stays 30 days (export is a copy).
   - Done when: filters + export tested; export includes full args; no new
     retention behavior.

Wave 1 exit: all features tick-tested, ruff/pyright/pytest green, web build green.

## Wave 2 — survival of the ecosystem (requires owner sign-off on 1 item)

9. **Skill provenance banner + injection screening** (S + M)
   - `load_skill` prepends author/version/provenance. Upload/save scans skill
     text for exfiltration-style directives (send/also-invoke + credential-ish
     strings or another user's tool IDs) as a **review warning**, not a block;
     tuned against pilot audit data.
   - No contract or pass-through change.
10. **Shared/global skill approval flow** § (S–M)
    - Non-owner-shared versions and global-skill saves become
      `pending_approval` until an admin approves (reuse review queue + diff
      view). Owner's own saves stay instant.
    - Sign-off: approval governance for *content*, not tool calls — confirm the
      spirit of "no approval steps" permits it.
11. **Dry-run `invoke` flag** § (S)
    - Optional `dry_run` on `invoke`: full resolve/validate, skip dispatch,
      return "would dispatch: OK"; audited.
    - Sign-off: extends the locked `cam` schema contract. No approval step
      introduced (agent self-decides).
12. **Post-pilot description-tuning assistant** (M)
    - Per-tool screen: worst NOT_FOUND/error/truncation/latency, oldest
      description; one-click override edit; before/after call-volume comparison
      (counts from audit).
    - Companion to `docs/pilot-plan.md`'s manual workflow.
13. **Anomaly detection (observe-only)** (M–L)
    - Offline job: off-hours bursts, new clientInfo on a token, uncommon tool
      combos, describe-enumeration spikes. Alerts to the admin digest only —
      never blocks (pass-through preserved by construction). Tune hard toward
      false-positive tolerance; note that 30-day retention limits baselining.
14. **SIEM export / durable audit spool** (M)
    - Stream audit events to company SIEM (http webhook/syslog) with a
      disk-backed retry spool. Retention unchanged. Sign-off §: full args land
      in the SIEM — confirm with security team during 7.1; do **not** adopt
      field redaction (locked decision).

## Wave 3 — demand-gated expansion (each waits for its trigger)

| Idea (effort) | Trigger to start | Sign-off / tension |
| --- | --- | --- |
| First-party templates: Salesforce, Slack/Jira, Windmill flows curation (M each) | Pilot teams ask | "Notion first" launch lock — administratively cheapest to relax. Gmail needs Google OAuth verification (external compliance). |
| Keyword `search` action + tool-catalog page (S + S) | Catalog > ~100 tools or agents repeatedly describe-fishing (watch audit) | Pre-negotiated path at architecture.md:110; additive to contract. |
| Scheduled invocations — agentic cron (M) | Teams ask for unattended runs | § Unattended writes run under a human's token; write down that a scheduled run is "a new run", not a retry (never-retry intact). OAuth expiry handling while away. |
| Per-token service presets / reach narrowing (L) | Shared-team-token misuse appears | § Owner decision: arguably reach, not tool scopes — but adds an access axis. Recommend deferring until misuse is real. |
| Agent-to-agent delegation via CAM (L) | Multi-agent teams form | § Long-running tasks vs 60 s timeout (job/poll pattern first); verified agent tokens become near-mandatory. |
| MRTR elicitation / resources / prompts passthrough (L) | A real upstream forces it | § Backs "tool calls only" launch lock. Do NOT chase sampling (deprecated in SEP-2577). |
| Sandbox org / per-org instances (S–M) | Phase 8.6 sister-company rollout | Public-readiness rules executing as designed; needs admin org-management screen + per-org settings UI. |
| SSO via Google Workspace (M) | Account count explodes at 8.6 | § Relaxes admin-accounts/no-SSO and admin-only password locks. AuthProvider interface absorbs most of the cost; directory sync changes identity lifecycle. |
| Spend/cost attribution (S/M) | Renewal-season request | Cheap as counts; credible costs need roll-ups beyond 30-day retention — build last or never. |

## Sequencing summary

- Now → Pilot (7.5): **Wave 1** fully; it is exactly the toolkit the pilot needs.
- Pilot week 1–2 in-flight: import Wave 1 data (notes, overrides) as issues surface.
- After 7.5 sign-offs land: **Wave 2** (security items queued around the 7.1 review).
- Later, per trigger: **Wave 3** table.

## Open questions for the owner (sign-off before the touching phase)

1. Content approval for shared/global skills — OK to add? (Wave 2, item 10)
2. `dry_run` on `invoke` — OK to extend the cam contract? (Wave 2, item 11)
3. Any per-token reach scoping ever wanted? (Wave 3)
4. Global-service registration requiring admin-sign-step? (deferred — not on the
   roadmap unless asked.)
5. SIEM destination + whether full args may stream externally (with 7.1).
