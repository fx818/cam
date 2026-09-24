#!/bin/sh
set -eu

PROJECT="${GCP_PROJECT:?usage: GCP_PROJECT=<project-id> SECRETS_PREFIX=cam- $0 [--print-only]}"
SECRETS_PREFIX="${SECRETS_PREFIX:-cam-}"
KEYS_SECRET="${SECRETS_PREFIX}encryption-keys"
CURRENT_SECRET="${SECRETS_PREFIX}encryption-key-current"
SESSION_SECRET_NAME="${SECRETS_PREFIX}session-secret"

KEYS="$(python3 -c "import base64,secrets; print('v1:'+base64.b64encode(secrets.token_bytes(32)).decode())")"
CURRENT="v1"
SESSION="$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")"

if [ "${1:-}" = "--print-only" ]; then
  printf '%s=%s\n' CAM_ENCRYPTION_KEYS "$KEYS"
  printf '%s=%s\n' CAM_ENCRYPTION_KEY_CURRENT "$CURRENT"
  printf '%s=%s\n' CAM_SESSION_SECRET "$SESSION"
  exit 0
fi


umask 177
BACKUP_DIR="$(mktemp -d /tmp/cam-keys-backup.XXXXXX)"
printf '%s\n' "$KEYS" > "$BACKUP_DIR/CAM_ENCRYPTION_KEYS"
printf '%s\n' "$CURRENT" > "$BACKUP_DIR/CAM_ENCRYPTION_KEY_CURRENT"
printf '%s\n' "$SESSION" > "$BACKUP_DIR/CAM_SESSION_SECRET"
printf '%s\n' "CAM master-key offline backup created $(date -u +%Y-%m-%dT%H%MZ). Losing these values makes every stored credential undecryptable; users and admins must re-enter credentials everywhere. Verify a restored copy can be read, then delete this directory." \
  > "$BACKUP_DIR/README"

gcloud services enable secretmanager.googleapis.com --project="$PROJECT" >/dev/null

put_secret() {
  secret="$1"
  if gcloud secrets describe "$secret" --project="$PROJECT" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$secret" --data-file=- --project="$PROJECT"
  else
    printf '%s' "$2" | gcloud secrets create "$secret" --data-file=- --project="$PROJECT"
  fi
}
put_secret "$KEYS_SECRET" "$KEYS"
put_secret "$CURRENT_SECRET" "$CURRENT"
put_secret "$SESSION_SECRET_NAME" "$SESSION"

cat <<WARN

Values generated and stored in project $PROJECT:
  $KEYS_SECRET          (CAM_ENCRYPTION_KEYS)
  $CURRENT_SECRET       (CAM_ENCRYPTION_KEY_CURRENT)
  $SESSION_SECRET_NAME  (CAM_SESSION_SECRET)

Offline-backup step (do this NOW):
  The temporary directory $BACKUP_DIR holds the same values. Copy it to a
  password manager or an encrypted medium, VERIFY the backup can be read
  back, then delete the directory:
    rm -rf $BACKUP_DIR
  Losing CAM_ENCRYPTION_KEYS means every stored credential (connection
  secrets, OAuth tokens, OAuth client secrets) is undecryptable and every
  user must re-enter their credentials in the dashboard.

Lock down access, then sync the values into the cam-secrets K8s Secret as
documented in deploy/encryption-keys.md.
WARN
