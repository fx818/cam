# Encryption keys in a secret manager (Phase 8.3)

CAM's master keys stay environment-injected (`CAM_ENCRYPTION_KEYS` as
`v1:<base64>,v2:<base64>`, `CAM_ENCRYPTION_KEY_CURRENT` naming the active one).
Phase 8 moves the **values** into Google Secret Manager; the code changes
nothing.

## Provisioning — first time only (owner)

```sh
GCP_PROJECT=PROJECT_ID sh scripts/provision_secrets_gsm.sh
```

This generates a fresh 32-byte key (`v1`), the session secret, pushes them as
three Secret Manager entries and prints where they live. `--print-only` prints
the values without touching Secret Manager (to pipe into a K8s Secret or keep
offline first).

**The script refuses to run if any of the three secrets already exists.** It no
longer overwrites an existing key ring: doing so would mint a *new* `v1` and
make every stored credential envelope (connection secrets, OAuth tokens, OAuth
client secrets) undecryptable — a full data-loss event. If you need the current
values, read them, don't re-provision:

```sh
gcloud secrets versions access latest --secret=cam-encryption-keys --project=PROJECT_ID
```

To change keys, use the append-only rotation procedure below — never mint a
second `v1`.

With the keys in Secret Manager, sync them into the `cam-secrets` K8s Secret
the Deployment reads:

```sh
GCP_PROJECT=PROJECT_ID sh scripts/sync_k8s_secrets.sh
```

`sync_k8s_secrets.sh` reads the *latest* version of each secret, so re-run it
after any rotation to refresh the K8s copy. (Re-running the *provisioning*
script is refused as above.)

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

## Master-key rotation (append-only; never overwrite)

Rotation is data-key re-wrapping, **not** re-encryption of credentials, and it
**appends a new key name while retaining the old ones** so existing envelopes
stay readable throughout. Do not re-run the provisioning script — it is
refused precisely because it would replace `v1`.

**Back up keys first; rotation is only safe once the backup is verified.**

1. Read the current ring and append the next key name (here `v2`; always pick
   the next unused name — never reuse or replace an existing one):

   ```sh
   P=PROJECT_ID
   CURRENT_KEYS="$(gcloud secrets versions access latest --secret=cam-encryption-keys --project=$P)"
   NEW_KEY="v2:$(python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')"
   ROTATED_KEYS="$CURRENT_KEYS,$NEW_KEY"
   ```

2. Add the rotated ring and the new current name as **new Secret Manager
   versions** (the previous versions remain and stay readable):

   ```sh
   printf '%s' "$ROTATED_KEYS" | gcloud secrets versions add cam-encryption-keys --data-file=- --project=$P
   printf '%s' "v2"          | gcloud secrets versions add cam-encryption-key-current --data-file=- --project=$P
   ```

3. Sync K8s and restart so the new ring is live:

   ```sh
   GCP_PROJECT=$P sh scripts/sync_k8s_secrets.sh
   kubectl -n cam rollout restart deployment/cam
   ```

   New credential writes are wrapped under `v2`.
   `connections/crypto.py:rewrap_data_key` re-wraps an org's `data_key_enc`
   under the current master key without touching the data key itself, so
   existing `connections.secret_enc` / `refresh_enc` /
   `services.auth_secret_enc` columns (encrypted with per-org data keys) can
   stay untouched until a convenient time — credentials decrypt under the data
   key, which now unwraps under `v2`.

4. **Re-wrap every org before retiring `v1`.** Verify by logging a user in and
   invoking a service (proves decrypt works under the new current key). Only
   then may you drop `v1`: append the ring *without* `v1` as yet another new
   version (`v2:<...>`) and re-sync. Until every org data key has been
   re-wrapped under `v2`, removing `v1` makes the not-yet-rewrapped
   credentials undecryptable — so do the re-wrap first, and update the offline
   backup with the final ring.

Practical hint for step 3: after changing `CAM_ENCRYPTION_KEY_CURRENT`, wait
for every pooled upstream session to be invalidated by rotation/restart or
force `kubectl rollout restart` so no worker keeps working under the old ring
decided by `CAM_ENCRYPTION_KEY_CURRENT`.
