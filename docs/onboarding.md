# CAM onboarding guide

For IndiaMART employees joining CAM. This guide matches the actual dashboard
(the pages under `/`, `/services/new`, `/agent-setup`, `/activity`, `/skills`)
and the `/api` routes as built.

## 1. Getting an account

Accounts are created by a CAM admin — there is no self-signup.

1. Ask your CAM admin for an account. Team accounts are also one email with
   one shared login; token labels (step 4) identify who is calling.
2. Log in at the CAM base URL with the email and password the admin gave you.
   Passwords are set by admins only; ask them if it needs changing.
3. Five failed logins within 15 minutes lock the account for 15 minutes.

## 2. First login tour

After login you land on **My services**, which lists everything you can reach:

- **Global** — services registered by admins with one shared credential
  (everyone or specific users).
- **Connected templates** — admin-defined services (e.g. Notion) where you
  connect your own credentials; unconnected ones show a **Connect** button.
- **My services** — services you registered yourself.
- **Shared with me** — services other users exposed to you.

Other pages:

| Page | What it does |
| --- | --- |
| Agent setup (`/agent-setup`) | Generate CAM tokens and copy ready-made agent configs |
| Service detail (`/services/<owner>/<slug>`) | Tools, Connection, Sharing, Activity, Settings tabs (some tabs only for the owner) |
| Register service (`/services/new`) | The 7-step wizard: Type → Details → Auth → Test → Tools → Limits → Save |
| Skills (`/skills`) | Browse, upload, edit, version and share text skills |
| Activity log (`/activity`) | Your calls plus calls others made through services you own; full args per row |
| Admin pages (`/admin/…`) | Admins only: users, global services, templates, review queue, overview, settings |

## 3. Connecting a template (example: Notion)

Templates let one admin definition serve many users:

1. On **My services**, find the template under *Connected templates* and click
   **Connect** (or open the service and use the **Connection** tab).
2. If the template uses OAuth, the dashboard sends you to the provider's
   consent screen and returns to CAM; your connection is stored encrypted — the
   key is shared; agents never see the token.
3. Health badges on the listings show healthy / failing / needs reconnect.
   Over time an OAuth token can expire or a refresh can fail; the service then
   shows **needs reconnect** and a prompt for the owner to reconnect.
4. Every service you register (including template connections and personal
   services) works out of the box for you. CAM never shows your secret to
   agents — only a short prefix is kept, and the full credential is never
   visible in the dashboard.

## 4. Generating tokens and connecting your agent

On **Agent setup**:

1. **Generate token** and give it a label naming the *machine/harness*, e.g.
   `rahul-claude-laptop` or `priya-codex`. The label appears in audit logs.
2. The raw token is shown **once**. Copy it now; it cannot be recovered. Only a
   short prefix is stored.
3. Copy the matching snippet:

   - Claude Code:
     `claude mcp add --transport http cam <BASE>/mcp --header "Authorization: Bearer <token>"`
     (or add `"type": "http", "url": "<BASE>/mcp", "headers": {"Authorization": "Bearer <token>"}`
     to your Claude MCP JSON).
   - Cursor and other JSON-config agents:
     `~/.cursor/mcp.json` →
     `{"mcpServers":{"cam":{"url":"<BASE>/mcp","headers":{"Authorization":"Bearer <token>"}}}}`
   - Codex: `~/.codex/config.toml` →
     `[mcp_servers.cam]` with `url = "<BASE>/mcp"` and
     `bearer_token_env_var = "CAM_TOKEN"` (export the token) or a static
     `http_headers` entry.

4. Use **one token per machine**. Revoke tokens you stop using — revocation is
   immediate. Remove any *direct* MCP servers that duplicate CAM services
   (e.g. your own Gmail MCP) so those calls aren't sneakily bypassing CAM's
   own audit log.

First call checklist: in your agent, say "use the cam tool to list services" —
you should get your accessible list straight from CAM.

## 5. Using the cam tool

Agents see exactly one MCP tool, `cam`, with these actions:

| Action | Input | Result |
| --- | --- | --- |
| `list_services` | — | Every service you can reach |
| `list_tools` | service | That service's enabled tools |
| `describe` | tool id | Full input schema |
| `invoke` | tool id, args | The tool's result (or a clear error) |
| `list_skills` / `load_skill` / `read_skill_file` | skill id | Text skills with version |

Tool IDs are `global/<service>/<tool>` or `<handle>/<service>/<tool>`.

## 6. Everyday rules worth knowing

- **Sharing is all-or-nothing**: if someone shares their service with you, you
  act with *their* credential. Person-to-person shares come from the owner only
  and cannot be re-shared.
- **Audit log keeps 30 days** of full tool-call arguments; response bodies are
  never logged. Admins see all; you see yours and calls on your services.
- **Rate limit**: 120 invokes/minute per token. Beyond that you get a
  retryable rate-limit error.
- **Large results** are truncated at 50,000 tokens (~200 KB of text); the reply
  tells you how to narrow the request.
- Deactivation of a user (including a team account) takes effect immediately:
  their tokens stop and services shared by them become unavailable.
