# Grafana wiring (Phase 8.4)

Source data: the minimal Prometheus exposition CAM now serves on
`/api/metrics` (unauthenticated, counts only — restrict at the ingress or
via NetworkPolicy to the monitoring namespace):

- `cam_http_requests_total{method,path,status}` and
  `cam_http_request_duration_seconds*` — request volume and latency by
  route/status
- `cam_audit_queue_depth`, `cam_audit_queue_capacity`,
  `cam_audit_events_dropped_total`, `cam_audit_events_written_total` — the
  async audit writer's buffer state
- `cam_upstream_sessions_active` — live pooled MCP sessions
- `cam_db_pool_size`, `cam_db_pool_checked_out_connections`,
  `cam_db_pool_overflow_connections` — SQLAlchemy async pool occupancy
- `cam_services_by_health{health}` — count of services healthy / failing /
  needs_reconnect (from the Phase 5 health-check job)
- `cam_scheduler_job_last_run_timestamp_seconds{job}`,
  `cam_scheduler_job_errors_total{job}` — background-job heartbeat

Log lines (app logs under `uvicorn`/`cam.*`) also carry audit writer drops
("audit queue full"), background-job failures and scheduler starts — CAM's
container logs are the log source; ship them with the standard GKE
Cloud Logging / LGTM Loki pipeline. Audit-partition create/drop events from
Phase 5.6 appear in `cam-jobs` logs and `cam_log-retention` heartbeats.

## Import (owner)

1. Deploy Prometheus + Grafana (e.g. kube-prometheus-stack) and a scrape
   config/service discovery for `cam` (`http` port, `/api/metrics`).
2. Grafana → Dashboards → Import → upload `cam-dashboard.json`
   (uid `cam-overview`), select the Prometheus datasource, save.
3. Alert rules: `kubectl apply -f prometheus-alerts.yaml` once
   PrometheusRule CRD exists (they are picked up by Prometheus's
   `prometheus.yaml` rule selector if the `release: kube-prometheus-stack`
   label is right). Alternatively import equivalent Grafana-managed rules
   with the same PromQL, into the alerting UI against the same datasource.
4. Verify at least one panel of the dashboard has data (illustration:
   `cam_http_requests_total > 0`). Generate traffic by logging into the
   dashboard or calling `/api/health`.

## Metrics-multiplicity caveat

With `CAM_UVICORN_WORKERS=1` and one pod replica each metric is unique and
truthful. If HPA scales the deployment beyond 1 replica, `cam_*` values
report per-pod values: either use `sum`/`max` over replicas in PromQL and
note the in-memory session affinity caveat (in-session state means the pool
metrics per pod stay valid; queue depth must be summed over pods), or use
per-pod labels already added (`node`, `pod`) by Prometheus relabeling.
