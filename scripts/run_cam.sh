#!/usr/bin/env bash
# CAM one-shot launcher (Linux/macOS).
# Usage:
#   scripts/run_cam.sh                # admin: prompted for email, password generated
#   scripts/run_cam.sh me@corp.in     # admin email given, password generated
#   scripts/run_cam.sh me@corp.in s3cret
#   scripts/run_cam.sh install        # install as global "camup" (~/.local/bin)
# Admin email/password can also come from CAM_ADMIN_EMAIL / CAM_ADMIN_PASSWORD.
# Repo location defaults to the sibling of this script; override with CAM_HOME.
set -euo pipefail

www="\\033[1;33m"; grn="\\033[1;32m"; red="\\033[1;31m"; rst="\\033[0m"
log() { printf "${grn}[CAM]${rst} %s\\n" "$1"; }
warn() { printf "${www}[CAM]${rst} %s\\n" "$1"; }
die() { printf "${red}[CAM]${rst} %s\\n" "$1" >&2; exit 1; }

SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
REPO_ROOT="${CAM_HOME:-}"
if [ -z "$REPO_ROOT" ]; then
  REPO_ROOT="$(dirname "$SELF")/.."
fi
cd "$REPO_ROOT"
[ -f docker-compose.yml ] || die "CAM repo not found at $REPO_ROOT - set CAM_HOME=/path/to/cam"

# --- install subcommand -------------------------------------------------------
if [ "${1:-}" = "install" ]; then
  GLOBAL_BIN="${GLOBAL_BIN:-$HOME/.local/bin}"
  mkdir -p "$GLOBAL_BIN"
  ln -sfn "$SELF" "$GLOBAL_BIN/camup"
  log "Installed: $GLOBAL_BIN/camup -> $SELF (symlink, follows repo updates)"
  case ":$PATH:" in
    *":$GLOBAL_BIN:"*) ;;
    *) warn "$GLOBAL_BIN is not on PATH - add: export PATH=\"\$PATH:$GLOBAL_BIN\"" ;;
  esac
  exit 0
fi

log "Directory: $REPO_ROOT"

if ! docker info >/dev/null 2>&1; then
  die "Docker is not running. Install/start it first: https://docs.docker.com/engine/install/"
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose is missing (install the docker-compose plugin)."

# --- Secrets (kept in gitignored .env so they survive restarts) ----------------
if [ ! -f .env ]; then
  : > .env && chmod 600 .env
fi
chmod 600 .env 2>/dev/null || true

gen_b64() { openssl rand -base64 32 2>/dev/null || od -An -N32 -tx1 /dev/urandom | tr -d ' \n' | base64; }

env_get() { awk -F= -v k="$1" '$1==k{print substr($0, length(k)+2)}' .env; }
env_set() { # key value  (replace line, else append)
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

NEWKEYS=0
if [ -z "${CAM_ENCRYPTION_KEYS:-}" ]; then
  if [ -z "$(env_get CAM_ENCRYPTION_KEYS)" ]; then env_set CAM_ENCRYPTION_KEYS "v1:$(gen_b64)"; NEWKEYS=1; fi
fi
[ -n "$(env_get CAM_ENCRYPTION_KEY_CURRENT)" ] || env_set CAM_ENCRYPTION_KEY_CURRENT "v1"
[ -n "$(env_get CAM_SESSION_SECRET)" ] || env_set CAM_SESSION_SECRET "$(gen_b64)"
[ "$NEWKEYS" = 1 ] && log "Generated new encryption key in .env (dev-grade; rotate for production)."

# --- Build & start the stack --------------------------------------------------
log "Building and starting the app + database container..."
docker compose up -d --build

HOST_PORT="$(docker compose port app 8000 2>/dev/null | sed 's/.*://')"
HOST_PORT="${HOST_PORT:-8001}"
BASE_URL="http://localhost:$HOST_PORT"

log "Waiting for CAM to become healthy (first build can take a few minutes)..."
for _ in $(seq 1 120); do
  # Ask the container itself (no host curl dependency), same as the compose healthcheck.
  if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" >/dev/null 2>&1; then
    Healthy=1; break
  fi
  sleep 5
done
[ "${Healthy:-0}" = 1 ] || { docker compose logs --tail 80 app >&2; die "CAM did not become healthy in time (see app logs above)."; }
log "CAM is up."

# --- Admin bootstrap (only if there is no active admin yet) -------------------
ADMIN_EMAIL="${CAM_ADMIN_EMAIL:-${1:-}}"
ADMIN_PASSWORD="${CAM_ADMIN_PASSWORD:-${2:-}}"

ADMIN_COUNT="$(docker compose exec -T db psql -U cam -d cam -tAc \
  "SELECT COUNT(*) FROM users WHERE role='admin' AND status='active';" 2>/dev/null || echo 0)"

if [ "$ADMIN_COUNT" -gt 0 ]; then
  log "An admin already exists - skipping create-admin."
else
  [ -n "$ADMIN_EMAIL" ] || { warn "Active admin needed."; printf "Admin email: "; read -r ADMIN_EMAIL; }
  [ -n "$ADMIN_EMAIL" ] || die "Email is required."
  [ -n "$ADMIN_PASSWORD" ] || { ADMIN_PASSWORD="$(openssl rand -base64 18 2>/dev/null | tr -d '/+=' | cut -c1-18)"; GENERATED=1; }
  docker compose exec -T -e CAM_ADMIN_EMAIL="$ADMIN_EMAIL" -e CAM_ADMIN_PASSWORD="$ADMIN_PASSWORD" app \
    sh -c 'uv run --no-sync cam create-admin --email "$CAM_ADMIN_EMAIL" --password "$CAM_ADMIN_PASSWORD"' \
    || die "Creating the admin failed (see output above)."
  if [ "${GENERATED:-0}" = 1 ]; then
    printf "${grn}[CAM]${rst} Admin password (save it now - shown once): %s\\n" "$ADMIN_PASSWORD"
  fi
fi

# --- Summary ------------------------------------------------------------------
printf "\\n${grn}=== CAM is running ===${rst}\\n"
printf "Dashboard:   %s\\n" "$BASE_URL"
printf "API health:  %s/api/health\\n" "$BASE_URL"
printf "MCP url:     %s/mcp\\n\\n" "$BASE_URL"
printf "Claude Code: claude mcp add --transport http cam %s/mcp --header \"Authorization: Bearer <token>\"\\n" "$BASE_URL"
printf "Cursor/Codex, other commands, docs: docs/onboarding.md\\n"
printf "Login with the admin account created above (or the existing one).\\n"
printf "Stop: docker compose down  (keeps data)  |  Stop+wipe: docker compose down -v\\n"
