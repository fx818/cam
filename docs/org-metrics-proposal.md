# Org-overview metrics — proposal for the owner (Phase 8.5, deferred by decision)

The spec defers org-overview metrics until real pilot usage exists ("usage
metrics to be defined later", architecture.md admin screens). This document
proposes candidates so the owner can choose; **nothing is built** until the
owner picks.

## Where the data already is

- `admin_overview_stats` (Phase 4/5) aggregates `users` and `services` by
  tier/status/health from live rows — cheap counts on the admin Overview
  screen today.
- `audit_logs` (30-day partitions) carries user/token, agent name/version,
  action, service/connection/owner, tool name, status, error code, latency,
  response size, truncation flag — all usage history.
- Phase 8.4's `/api/metrics` (deploy/grafana/cam-dashboard.json) shows live
  operational metrics in Grafana.

## Proposed candidate metrics (owner picks N)

1. **Daily active users and agents** — distinct users and distinct
   `agent_name` per day from audit (adoption health; how many of the invited
   accounts actually use CAM).
2. **Invocations per day by service (top 10)** — where the value is going;
   drives the "register more services/knowledge" next step.
3. **Invoke error rate by service** — `status != ok` / total, top 10; keeps
   tool owners honest about quality upstream.
4. **Token cap / truncation rate** — percentage of `truncated=true` invokes
   per service; when a service keeps truncating, its tools need filters.
5. **Latency p50/p95 by service** — slowest tools upstream worth fixing.
6. **Unreachable/needs-reconnect count** — already in admin overview stats
   (`services.by_health`) plus the Grafana gauge; the number that mirrors
   "CAM is down or degraded".
7. **NOT_FOUND rate** — agents asking for tools outside access; the signal
   for description tuning and template creation (per the pilot-plan workflow).
8. **Tokens generated/revoked per week** — security hygiene.
9. **Skill loads** — `load_skill` count per skill; which know-how agents
   actually use.
10. **Rate-limited invokes** — `CAM_RATE_LIMITED` rows; signals contention or
    runaway agents.

Suspected final set for a small admin screen: 1, 2, 3 and 6 as headline
cards; 7, 8 as secondary; the rest live in the Grafana board instead of
needing dashboard screens. When the owner picks the list, the screen is one
Series of queries on the already-indexed `(service_id, ts)`/`(user_id, ts)`
audit indexes (Phase 1.1) plus a per-day roll-up query; no new infra.
