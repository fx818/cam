# CAM security review (Phase 7.1)

Owner-held meeting with IndiaMART's security team signs this off. This document
supports the meeting: it summarises the threat model, lists every
security-relevant surface with a code pointer, records the values verified by
automated tests, and ends with the exact questions the team must answer.

Automated backstop: `tests/test_secret_hygiene.py` greps the source for
credential-like values in logging calls and hard-coded literals, and pins the
documented configuration values. CI fails if any of these drift.

## 1. What CAM holds

CAM is an internal IndiaMART hub for agents. It stores and stores-forward:

- org credentials for global services (Windmill, Grafana tokens…),
- each user's OAuth tokens and static secrets for template/personal services,
- passwords for admin-created accounts (argon2 hashes),
- bearer tokens agents use to call CAM (SHA-256 hashes only),
- full tool-call arguments in the audit log (30-day retention).

The data combinations that matter: full args over internal service data, and a
credential store that can act as many users on many services.

## 2. Threat model (summary)

| Threat | Where it is addressed |
| --- | --- |
| Agent or outsider calls a service they must not reach | Single access-resolution module; nothing else checks access |
| Credentials stolen from the database | AES-256-GCM envelope encryption; master keys in env, never in the DB |
| Credentials appear in logs, errors or API responses | Injection after audit capture; API never returns secrets; automated greps |
| CAM turned into a pivot into the internal network | URL policy (SSRF) at registration and per request with IP pinning; admin allowlist |
| Stolen dashboard cookies / CSRF | Signed HTTP-only session cookie, SameSite=Lax, CSRF double submit |
| Brute-force login | 5 failures/15 min per email → 15-min lockout; per-IP failure cap |
| Runaway agent flooding upstreams | Per-token rate limit on invoke |
| Audit history lost / tampered | Append-only rows with 30-day partition drops; no update path in code |
| Master key loss | Key outside DB; **offline backup is mandatory** (sign-off question below) |

Out of scope (locked decisions): narrowing tool permissions, approval steps,
verified agent identity (`clientInfo` is self-reported). CAM controls reach,
not scopes.

## 3. Surface checklist

Each row: surface → code → why it is safe → automated check.

### 3.1 Credential storage and key handling

| Surface | Code | Notes |
| --- | --- | --- |
| Master keys from env, `v1:<base64>` format, one must be current | `src/cam/config.py` (`parse_encryption_keys`, `generate_encryption_key_entry`); `src/cam/connections/crypto.py` (`MasterKeyRing`) | 32-byte keys; current must name a ring entry; prod startup fails if missing |
| Envelope encryption: per-org data key wrapped by master key (`CAMW1` header, AES-GCM, key name in header) | `src/cam/connections/crypto.py` (`wrap_data_key`, `unwrap_data_key`, `rewrap_data_key`) | `orgs.data_key_enc`; rotating the master re-wraps only data keys |
| Credential blobs (`CAMC1` header + key_version byte + 12-byte nonce + AES-256-GCM ciphertext) | `crypto.py` (`encrypt_secret`, `decrypt_secret`); used in `registry/services.py`, `connections/rotation.py`, `access/credentials.py` | `connections.secret_enc`/`refresh_enc`, `services.auth_secret_enc`; `key_version` supports rotation |
| Passwords: argon2id, hashed and verified in a thread pool (never on the event loop) | `src/cam/connections/passwords.py` | library defaults (argon2id); verified in `tests/test_security.py` |
| CAM tokens: 32 random bytes base62, `cam_` prefix; SHA-256 hash stored, 8-char visible prefix; revocation checked on every call together with user-active and not-deactivated | `src/cam/connections/tokens.py` (`generate_raw_token`, `hash_token`, `authenticate_token`) | raw token returned exactly once at creation (`api/routes/tokens.py`), dashboard shows prefix only |
| Key backup | — | **Manual: owner must copy `CAM_ENCRYPTION_KEYS` offline.** Losing them makes every stored credential unre-recoverable |

### 3.2 Session and login security (dashboard)

| Surface | Code | Notes |
| --- | --- | --- |
| Session cookie: HTTP-only, `Secure` under HTTPS, `SameSite=Lax`, 12-hour idle expiry, signed by `CAM_SESSION_SECRET` | `src/cam/auth/sessions.py` | idle timeout pinned by test |
| CSRF double submit on every mutating `/api` request (`X-CSRF-Token` header vs `cam_csrf` cookie, `hmac.compare_digest`) | `src/cam/api/csrf.py` | covered in `tests/api/test_auth.py` |
| Lockout: 5 failures per email within 15 min → 15-min lockout; 20 failures per IP also lock | `src/cam/auth/lockout.py`; `login_attempts` table | pinned by tests |
| Login goes through the `AuthProvider` interface (session org from server state, never request input) | `src/cam/auth/providers.py`, `api/routes/auth.py` | |
| Password reset only by admins | `api/routes/admin_users.py` | no self-service path (locked decision) |

### 3.3 Access checks (the security boundary)

| Surface | Code | Notes |
| --- | --- | --- |
| Single access-resolution module for every list/describe/invoke/skill path | `src/cam/access/` (`resolution.py`, `shares.py`, `skills.py`) | the gateway and API routes never filter results themselves |
| Unknown ID, excluded tool and no-access all return `NOT_FOUND` without revealing which | `invoke/errors.py` (`NOT_FOUND`) | access matrix tests incl. cross-org isolation |
| Non-re-shareable, all-or-nothing shares; revocation effective next call | `access/shares.py` | |
| Deactivation: tokens revoked, connections disabled, sessions closed, audit kept | `accounts/users.py` (`deactivate_user`), tests `tests/access/test_deactivation.py` | |
| Visibility restricted to the caller's org (`org_id` from token/session) | `cam_tokens.org_id`, every access query | |
| Admins do not implicitly reach restricted services | resolved Phase 2 open question | intended pass-through behavior |

### 3.4 URL policy / SSRF (personal services and all outbound redirect hops)

| Surface | Code | Notes |
| --- | --- | --- |
| Static admission: http/https only, no embedded credentials, blocked hostnames (`localhost`, `metadata.google.internal`, `*.localhost`) and blocked literal IPs | `src/cam/upstream/url.py` (`admit_url`) | ranges per the spec: loopback/private/link-local/ULA/100.64/fe80/fc00/0.0.0.0/8/169.254 |
| Per-request DNS validation + IP pinning (defeats DNS rebinding); every resolved address must pass | `upstream/url.py` (`validate_outbound_url`); pinning applied in `upstream/pinning.py`, `upstream/client.py` | runs at registration **and** on every request (`connections/url_policy.py` loads org + allowlist per call) |
| Admin allowlist: exact hostname (skips DNS pin) or CIDR (covers resolved IPs) | `upstream/url.py`, admin Settings routes `api/routes/admin_settings.py` | |
| Redirects never auto-followed; each hop validated same rules, max 3 hops | `upstream/url.py` (`admit_redirect`); REST executor and MCP client each enforce | |
| Restraint decision: URL-policy rejections surface as `UPSTREAM_UNAVAILABLE` with policy detail in the audit message | Phase 2 coordinator resolution | spec error enum kept closed; flag in sign-off if security wants a distinct code |
| MCP-server-side DNS rebinding protection is explicitly disabled (`TransportSecuritySettings(enable_dns_rebinding_protection=False)`) — inbound, agents are bearer-token authenticated; the outbound `admit_url`/pinning layer protects against rebinding on the sensitive side | `api/main.py`, Phase 1.4 open item | sign-off question 4 |

### 3.5 Secrets never logged or returned

| Surface | Code | Notes |
| --- | --- | --- |
| Credentials injected only after audit args captured | `gateway/server.py` (`cam` tool) → `invoke/pipeline.py` (`handle` captures `args` first; `resolve_credential` later) | |
| Tokens returned once at creation, prefix only thereafter | `api/routes/tokens.py` | |
| CAM tokens never written to CAM's own logs | grep test | BearerAuthMiddleware only places identity on the ASGI scope |
| OAuth client secrets and refresh tokens encrypted at rest | `connections/oauth.py`, `crypto.py` | |
| Skill special case: full args logging applies to invoke; skill loads log ids only | `audit/writer.py` | |

### 3.6 Audit log content

- Full args, no response bodies; `latency_ms`, `response_bytes`, `truncated`,
  agent name/version, connection owner stored directly (`audit_logs`).
- Monthly partitions; retention job drops partitions older than 30 days
  (`db/partitions.py`, `jobs/retention.py`).
- No DEFAULT partition: a clock-skew event (ts far outside the ensured
  months) fails the insert. Events are dropped with a warning at most; calls
  are not affected. Acceptable trade-off — confirm in sign-off.
- Admin sees all; user sees own + calls through services they own
  (`api/routes/audit.py` scope filter).

### 3.7 Rate limit

- 120 `invoke` calls per token per minute by default (`CAM_RATE_LIMIT_PER_MIN`),
  excess returns `CAM_RATE_LIMITED` retryable (`invoke/rate_limit.py`,
  pinned default in the hygiene test).
- Applies to the `cam` MCP tool only — the dashboard JSON API has no per-route
  rate limit beyond login lockout per IP; note for sign-off.

### 3.8 Things deliberately not done (with reasons)

- No per-agent verified identity: tokens identify the account; `clientInfo` /
  `X-CAM-Agent` is self-reported (locked decision; sign-off question 6).
- No approval steps or scope narrowing (pass-through model).
- `.env` never committed: `.gitignore` excludes it, pre-commit hook blocks
  staged `.env` files, `detect-private-key` hook present; tests assert the
  gitignore entries (`tests/test_config.py`).

## 4. Verified current values (asserted by tests)

| Item | Verified value |
| --- | --- |
| Password hashing | argon2id, computed in a thread pool |
| Login lockout | 5 failures / 15 min per email → 15 min; 20 per IP → 15 min |
| Dashboard session | 12 h idle, HTTP-only, `SameSite=Lax`, CSRF double-submit |
| CAM rate limit default | 120 invokes/min/token |
| Invoke defaults | 60 s timeout, 50,000-token cap, 30-day retention |
| Master key env format | `v1:<base64>` (32 bytes), `CAM_ENCRYPTION_KEY_CURRENT` names the active key |
| Credential blob | `CAMC1` magic + version + AES-256-GCM (data key), key_version byte preserved |
| CAM token | `cam_` + 32 random bytes base62 (~192 bits); SHA-256 hash at rest; 8-char prefix |
| `.env` commit guard | pre-commit hook + `.gitignore` + test |

## 5. Questions the security team must sign off

1. **Credential store:** are encrypted Postgres columns with master keys in
   env vars acceptable pre-Phase 8, given the plan to move keys to a secret
   manager (GKE secret) in 8.3? Any requirement (HSM, Vault) we can't meet?
2. **Full-args audit logging:** 30-day retention of complete tool-call
   arguments on internal services — is the retention window and lack of
   PII-aware redaction acceptable? Do we need field-level redaction
   per service?
3. **Audit durability:** the async writer drops events with a warning if its
   bounded queue overflows, and a partition insert failure drops the event
   (no DEFAULT partition). Is "best-effort audit" acceptable for an internal
   rollout, or do we need a durable spool?
4. **DNS rebinding:** outbound requests are pinned after per-request DNS
   validation; the MCP*server-side* rebinding check is off because agents
   authenticate with bearer tokens. Acceptable?
5. **URL-policy error surface:** SSRF rejections return generic
   `UPSTREAM_UNAVAILABLE` with the policy detail only in the audit log. OK, or
   should it be a distinct error code (spec change)?
6. **Agent identity is self-reported** (`clientInfo`, `X-CAM-Agent`). OK for
   internal use?
7. **Admin allowlist powers:** an allowlisted hostname/CIDR bypasses the
   personal-service IP restrictions — including metadata-ish ranges an admin
   might add carelessly. Is admin approver + note + author recorded enough?
8. **Key backup:** confirm the owner has an offline, access-controlled copy of
   `CAM_ENCRYPTION_KEYS` before pilot (recommended, hard requirement
   post-pilot whoever holds it).
9. **Team accounts share one login:** audit shows the team; token labels
   partially identify people. Acceptable?
10. **Login rate limiting:** lockout is per email/IP, no CAPTCHA, no SSO and
    admin-set passwords with no expiry (locked decision). OK for internal?

## 6. Follow-ups after sign-off

- Phase 8.3 moves master keys to a secret manager; re-run the rotating-key
  rewrap path after that change.
- If security requires durable audit, add a disk-backed spool in front of the
  queue (design note in PROGRESS open questions at approval time).
