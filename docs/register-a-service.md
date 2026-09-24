# Registering a service in CAM

How the three tiers work, how registration works end-to-end in the current
dashboard, and the lifecycle you manage as an owner or admin. This reflects the
actual wizard (`/services/new`: Type → Details → Auth → Test → Tools → Limits →
Save) and the `/api/services*` routes.

## Tiers: global, template, personal

| Tier | Registered by | Credential | Who can use it |
| --- | --- | --- | --- |
| **Global** | Admin (`POST /api/admin/services`) | One org credential stored by the admin | Everyone, or specific users the admin lists |
| **Template** | Admin defines it once | Each user connects their own | Users the admin allows; each user can share their own connection |
| **Personal** | Any user (`POST /api/services`) | The owner's credential | The owner plus people they share with |

Naming: a global service is `global/<slug>` in tool IDs; a template or personal
service is `<owner-handle>/<slug>`. Personal slugs cannot reuse a template slug
in the same org.

## Step by step (wizard)

1. **Type** — MCP server or REST API.
2. **Details**
   - *MCP*: URL of the Streamable HTTP endpoint (or the older SSE transport).
     `stdio` MCPs cannot be registered directly — an admin runs them behind a
     stdio→HTTP bridge (`mcp-proxy` or `supergateway`) and registers that URL.
   - *REST*: if the API has an OpenAPI/Swagger spec, give the spec URL and each
     operation becomes a tool (large specs let you pick which endpoints to
     import); otherwise define tools by hand (method, path, parameters, body
     schema, description) via
     `POST /api/services/{owner}/{slug}/tools`.
3. **Auth** — one of:
   - **None** — trusted internal network only.
   - **Static header / Bearer** — the secret is stored encrypted and injected
     at call time; never displayed again.
   - **API key** — header or query parameter, name chosen in the wizard.
   - **Basic** — username plus secret.
   - **OAuth (authorization code + PKCE)** — used by templates like Notion;
     CAM does discovery or uses a pre-registered app and stores tokens
     encrypted, refreshing them automatically.
4. **Test** — CAM opens the connection (`POST /api/services/test`) and shows
   the exact error on failure; nothing is saved when the test fails.
5. **Tools** — every discovered/imported tool is listed with checkboxes;
   unchecking a tool excludes it (it cannot be listed, described or invoked
   later). Descriptions can be overridden since upstream descriptions are
   often weak. Excluded tools can be re-enabled later from the Tools tab.
6. **Limits** — per-service invoke timeout (default 60 s) and response cap
   (default 50,000 tokens ≈ 200 KB). Slow tools (Redash-style queries) may
   justify a larger timeout; harness tool-call timeouts also matter, see
   `docs/harness-checks.md`.
7. **Save** — published immediately. URLs are validated at registration and
   again on every request (see URL policy below).

## After publishing

- **Health** — every service shows healthy / failing / needs reconnect. Health
  updates from periodic probes, re-syncs and invoke results.
- **Re-sync** — from the Tools tab or `POST /api/services/{owner}/{slug}/resync`,
  on a schedule, or when the upstream announces `tools/list_changed`.
  New tools land as **pending review** on global services and templates (an
  admin approves them in the review queue) and are **enabled automatically** on
  personal services. Tools removed upstream are marked `removed_upstream`, not
  deleted, so audit rows still resolve.
- **Credential rotation** — Connection tab (or
  `POST /api/connections/{id}/rotate`) re-encrypts with the latest key and
  closes the pooled session, no re-registration needed. OAuth connections
  rotate by re-running the connect flow.
- **Disable / delete** — a deleted service keeps audit history but exposes
  nothing: "unknown or disabled tool" for agents; the slug stays reserved.

## URL policy (personal services)

Personal services may not target private, metadata or loopback addresses; only
`http(s)` is accepted and the check runs at registration **and on every
request**, with the connection pinned to the validated IP. The admin allowlist
(admin Settings → URL allowlist) can exempt specific hosts or CIDR ranges.
Template and global services are not restricted because admins register those
URLs deliberately. Redirects are never auto-followed; each hop is re-validated
(up to 3 hops).

## Sharing rules (owner only, Sharing tab)

- Sharing is **all-or-nothing per service**: the grantee can do anything your
  credential can, including acting as you. The dashboard states this warning.
- Shares are made on a connection and can be revoked at any time; effect is
  immediate (next call fails with "not found or not available").
- Grantees cannot pass the share on to other users.
- Removing the share, rotating your credential, or your deactivation stops
  grantee access right away.

## Lifecycle quick reference

| You asked | What happens |
| --- | --- |
| Delete a service | Status `deleted`, secrets wiped, shares revoked, sessions closed; audit rows kept |
| New tool upstream | Pending review (global/template) or auto-enabled (personal) |
| Tool gone upstream | Marked removed, never deleted |
| Owner deactivated | Everything they shared stops immediately with a clear error |
| Health shows failing | Re-check the credential or URL; after fixing, re-sync or rotate |
| OAuth token expired | The service shows *needs reconnect*; the owner reconnects from the Connection tab |
