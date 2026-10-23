# deploy/gke — manifests and owner runbook

Owner executes every command themselves. No credentials live in this repo.
Two placeholders used throughout: `https://cam.CHANGE_ME.internal` (set real
`CAM_BASE_URL`) and the Cloud SQL/Zonal instance strings.

## 1. Create the infrastructure (sign in once)

```sh
gcloud auth login && gcloud config set project PROJECT_ID
gcloud services enable artifactregistry.googleapis.com gke.googleapis.com \
  sqladmin.googleapis.com compute.googleapis.com secretmanager.googleapis.com
```

## 2. Build and push the single-container image

The repo's `Dockerfile` is multi-stage: node stage builds the React dashboard
(`web/` → `web/dist`), python stage copies it in and serves everything from one
uvicorn process. Master keys, DB URL and session secret must be env-injected.

```sh
gcloud artifacts repositories create cam \
  --repository-format=docker --location=asia-south1 --description="CAM images"
docker build -t asia-south1-docker.pkg.dev/PROJECT_ID/cam/cam:1.0.0 .
docker push asia-south1-docker.pkg.dev/PROJECT_ID/cam/cam:1.0.0
```

## 3. GKE cluster

```sh
gcloud container clusters create-auto cam-cluster --region=asia-south1
gcloud container clusters get-credentials cam-cluster --region=asia-south1
```

## 4. Database

Cloud SQL is the recommended Postgres home. Follow `deploy/cloudsql.md`, then
the workload identity account:

```sh
gcloud iam service-accounts create cam-proxy
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:cam-proxy@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"
```

## 5. Secrets (8.3)

Follow `deploy/encryption-keys.md` and provision into Google Secret Manager,
then sync to K8s Secrets (the sample below uses ExternalSecrets style; or simply
`kubectl create secret` from the printed keys). Create:

- `cam-secrets` ← `CAM_ENCRYPTION_KEYS`, `CAM_ENCRYPTION_KEY_CURRENT`,
  `CAM_SESSION_SECRET` (from Secret Manager)
- `cam-db` ← `CAM_DATABASE_URL` (Cloud SQL connect string with the password)

Then:

```sh
sed -i 's/PROJECT/PROJECT_ID/g; s/REGION/asia-south1/g; s/REPO/cam/g; s/SET_TAG_AT_DEPLOY/1.0.0/g; s/REPLACE_INSTANCE_NAME/cam-db-instance/g' \
  deploy/gke/deployment.yaml deploy/gke/migration-job.yaml
kubectl apply -f deploy/gke/namespace.yaml \
  && kubectl apply -f deploy/gke/configmap.yaml \
              -f deploy/gke/serviceaccount.yaml \
              -f deploy/gke/service.yaml \
  && kubectl create secret generic cam-secrets -n cam \
       --from-literal CAM_ENCRYPTION_KEYS=... --from-literal CAM_ENCRYPTION_KEY_CURRENT=v1 \
       --from-literal CAM_SESSION_SECRET=... \
  && kubectl create secret generic cam-db -n cam \
       --from-literal CAM_DATABASE_URL="postgresql+asyncpg://cam:pw@127.0.0.1:5432/cam"
```

## 6. Migration, then deploy (8.2)

```sh
kubectl apply -f deploy/gke/migration-job.yaml
kubectl wait --for=condition=complete job/cam-migrate -n cam --timeout=600s
kubectl apply -f deploy/gke/deployment.yaml \
              -f deploy/gke/hpa.yaml \
              -f deploy/gke/ingress.yaml \
              -f deploy/gke/ingress-metrics.yaml
kubectl rollout status deployment/cam -n cam
```

The migration Job carries its **own Cloud SQL proxy sidecar** (R30), so it can
reach the DB at `127.0.0.1:5432` exactly like the Deployment. Migrations run
here, once, *before* the rollout; the app container no longer runs
`alembic upgrade head` at startup (R30), which removes the rollout migration
race. Both the Job and the Deployment use the same Workload Identity service
account for the proxy.

## 7. DNS + TLS (8.1 "genuine" tick needs this)

Point the DNS A/AAAA record for `CAM_BASE_URL` at the ingress address
(`kubectl get ingress cam -n cam`). Then install Ingress NGINX and cert-manager
(once per cluster), or switch `deploy/gke/ingress.yaml` to use a GKE managed
certificate by name instead of the cert-manager secret.

```sh
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx && \
helm install nginx ingress-nginx/ingress-nginx
helm repo add jetstack https://charts.jetstack.io && \
helm install cert-manager jetstack/cert-manager \
  --set installCRDs=true
kubectl apply -f - <<'YAML'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: {name: letsencrypt-prod}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: REPLACE-OWNER-EMAIL
    privateKeySecretRef: {name: letsencrypt-prod-account-key}
    solvers: [{http01: {ingress: {class: nginx}}}]
YAML
```

## 8. Verify

```sh
kubectl -n cam get pod,svc,ingress
curl https://CAM_BASE_URL/api/health
curl https://CAM_BASE_URL/api/metrics | head      # scraped by Prometheus
```

## Deploy manifest notes

- `configmap.yaml`: non-secret `CAM_*` env, matches `docs/architecture.md`
  Configuration. `CAM_DB_POOL_SIZE`/`CAM_DB_MAX_OVERFLOW` added in Phase 8 for
  the load-test pool sizing finding (7.2).
- `deployment.yaml` runs the app as a non-root user (uid 10001) with a
  securityContext (no privilege escalation, all Linux capabilities dropped) and
  a Cloud SQL proxy sidecar. One uvicorn worker per pod: MCP `Mcp-Session-Id`
  sessions, rate-limit counters, upstream pool invalidation and the job
  schedulers all live in process memory, so **the Deployment stays at
  `replicas: 1`** (R15). Use `1` worker; revisit replica scaling only once
  distributed coordination lands.
- `rollingUpdate: maxUnavailable: 0` keeps a pod serving while a new replica
  warms up (in-memory MCP sessions on the old pod are dropped on pod delete —
  agents reconnect transparently, next request opens a fresh session).
- `healthchecks`: liveness/readiness/startupProbe on `/api/health`. NOTE: that
  endpoint is DB-backed, so it is really a readiness signal; splitting a
  dependency-free liveness probe out of `src/cam/api/routes/health.py` is a
  cross-file follow-up (that route is outside this change set).
- Dockerfile bakes `HEALTHCHECK` too for plain-docker use. The image runs
  **non-root** and **no longer runs `alembic upgrade head` at container start**
  (R30) — schema migrations run in the Job below (and, for local compose, a
  one-shot `migrate` service).
- `hpa.yaml` is pinned to `minReplicas: 1` / `maxReplicas: 1` (R15); see the
  deployment note above. Revisit when distributed state lands.
- `migration-job.yaml` runs `alembic upgrade head` before the deploy and exits;
  it now carries its **own Cloud SQL proxy sidecar** so it can reach the DB at
  `127.0.0.1:5432` (R30 — previously only the Deployment had the proxy, so the
  Job could never connect). TTL cleans it up. `CAM_JOBS_ENABLED=false` so it
  doesn't schedule jobs.
- `ingress.yaml` serves `/`; `ingress-metrics.yaml` is a second Ingress that
  matches `/api/metrics` with `pathType: Exact` and a `whitelist-source-range`,
  restricting the unauthenticated metrics route to the monitoring network
  (R29). Set that range to the Prometheus scraper's source IP range, and apply
  both Ingress files.
