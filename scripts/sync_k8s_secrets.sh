#!/bin/sh
set -eu

PROJECT="${GCP_PROJECT:?usage: GCP_PROJECT=<project-id> SECRETS_PREFIX=cam- NAMESPACE=cam $0}"
SECRETS_PREFIX="${SECRETS_PREFIX:-cam-}"
NAMESPACE="${NAMESPACE:-cam}"

KEYS="$(gcloud secrets versions access latest --secret="${SECRETS_PREFIX}encryption-keys" --project="$GCP_PROJECT")"
CURRENT="$(gcloud secrets versions access latest --secret="${SECRETS_PREFIX}encryption-key-current" --project="$GCP_PROJECT")"
SESSION="$(gcloud secrets versions access latest --secret="${SECRETS_PREFIX}session-secret" --project="$GCP_PROJECT")"

cat <<'NOTE'
Syncing the K8s Secret cam-secrets from Google Secret Manager. Re-run this
script whenever the GSM secret versions change (master-key rotation). Google
Secret Manager stays the source of truth; the K8s Secret is an ephemeral copy.
NOTE

kubectl -n "$NAMESPACE" create secret generic cam-secrets \
  --from-literal "CAM_ENCRYPTION_KEYS=$KEYS" \
  --from-literal "CAM_ENCRYPTION_KEY_CURRENT=$CURRENT" \
  --from-literal "CAM_SESSION_SECRET=$SESSION" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" rollout restart deployment/cam 2>/dev/null || true
kubectl -n "$NAMESPACE" describe secret cam-secrets
