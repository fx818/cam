# Encryption keys in a secret manager (Phase 8.3)

CAM's master keys stay environment-injected (`CAM_ENCRYPTION_KEYS` as
`v1:<base64>,v2:<base64>`, `CAM_ENCRYPTION_KEY_CURRENT` naming the active one).
Phase 8 moves the **values** into Google Secret Manager; the code changes
nothing.

## Provisioning (owner)

```sh
GCP_PROJECT=PROJECT_ID sh scripts/provision_secrets_gsm.sh
```

This generates a fresh 32-byte key (`v1`), the session secret, pushes them as
three Secret Manager entries and prints where they live. `--print-only` prints
the values without touching Secret Manager (to pipe into a K8s Secret or keep
offline first).

With the keys in Secret Manager, sync them into the `cam-secrets` K8s Secret
the Deployment reads:

```sh
GCP_PROJECT=PROJECT_ID sh scripts/sync_k8s_secrets.sh
```

(Re-running both scripts after a rotation updates the K8s copy.)

## Record the offline backup (owner, mandatory)

**Losing `CAM_ENCRYPTION_KEYS` = every stored credential is gone.** The
per-org data keys in `orgs.data_key_enc` are wrapped with the master keys;
if the master keys are lost, all connection secrets, OAuth access/refresh
tokens and OAuth client secrets are undecryptable and users must re-enter
them in the dashboard. There is no reset that recovers the data.

The provisioning script stages a temporary folder with the values; copy them
to a password manager or printed/encrypted medium outside Google Cloud,
**verify the backup can be read back**, then delete the staging folder. Also
record them in the runbook index (where, who can read them).

Lock Secret Manager access down:

```sh
gcloud secrets add-iam-policy-binding cam-encryption-keys \
  --project=PROJECT_ID --member=serviceAccount:CAM_DEPLOY_SA... \
  --role=roles/secretmanager.secretAccessor
```

Alternative without Secret Manager: skip the GSM script, generate values with
`scripts/provision_secrets_gsm.sh --print-only > keys.env` and `kubectl create
secret generic cam-secrets -n cam --env-literal-file keys.env` (keep an offline
copy — non-secret-manager storage of keys is exactly what Phase 8 is leaving).

## Master-key rotation (re-wrap workflow)

Rotation is data-key re-wrapping, not re-encryption of credentials:

1. Generate `v2:<base64>` and append it to `CAM_ENCRYPTION_KEYS` (keep `v1`
   so old envelopes stay readable).
2. Set `CAM_ENCRYPTION_KEY_CURRENT=v2`.
3. Restart CAM (both the Deployment and the CLI env). New credential writes
   are wrapped under `v2`. `connections/crypto.py:rewrap_data_key` re-wraps
   an org's `data_key_enc` under the current master key without touching the
   data key itself, so existing `connections.secret_enc` / `refresh_enc` /
   `services.auth_secret_enc` columns (encrypted with per-org data keys) can
   stay untouched until a convenient time — credentials decrypt under the
   data key which now unwraps under `v2`.
4. After a verified re-wrap (login a user, invoke a service), remove `v1`
   from `CAM_ENCRYPTION_KEYS` and update the offline backup.
5. Sync K8s (`sync script` above) and `kubectl rollout restart deployment/cam`.

Practical hint for step 3: after changing `CAM_ENCRYPTION_KEY_CURRENT`, wait
for every pooled upstream session to be invalidated by rotation/restart or
force `kubectl rollout restart` so no worker keeps working under the old ring
decided by `CAM_ENCRYPTION_KEY_CURRENT`.

**Back up keys first; rotation is only safe once the backup is verified.**
