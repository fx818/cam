# Harness checks (Phase 7.3) — Claude Code, Codex, Cursor

Verified against each harness's current documentation (Sep 2026). Live runs of
real Claude Code / Codex / Cursor against a running CAM are manual checks listed
in the pilot plan; cam's `/mcp` surface is harness-agnostic Streamable HTTP with
`Authorization: Bearer cam_<token>` on every request.

## Config snippets (as shown in the dashboard "Agent setup")

| Harness | Syntax | Source checked | Dashboard snippet |
| --- | --- | --- | --- |
| Claude Code | `claude mcp add --transport http cam <BASE>/mcp --header "Authorization: Bearer <token>"` | code.claude.com/docs/en/mcp | yes, matches docs |
| Cursor (and generic MCP JSON clients) | `~/.cursor/mcp.json` → `{"mcpServers":{"cam":{"url":"<BASE>/mcp","headers":{"Authorization":"Bearer <token>"}}}}` | cursor.com/docs/mcp (Remote Server example: `url` + `headers`) | yes, matches docs |
| Codex | `~/.codex/config.toml` → `[mcp_servers.cam] url = "<BASE>/mcp"`, `bearer_token_env_var = "CAM_TOKEN"` or static `http_headers = { Authorization = "Bearer <token>" }` | developers.openai.com/codex/config-reference; imagine.openai.com Codex MCP docs (`codex mcp add <name> --url <url>`) | added this phase |

Notes from the verification:

- Claude Code's JSON config requires `"type": "http"` (or `streamable-http`
  alias) next to `url`; the dashboard's JSON snippet is documented as the
  Cursor/generic shape (Cursor docs do not need `type`). Claude Code users get
  the CLI snippet with `--transport http`, which is authoritative.
- Codex rejects a server entry that mixes stdio and streamable-HTTP fields; the
  snippet keeps one transport with one auth source (bearer takes precedence over
  the static header where both are present, so the header is commented out).
- Cursor resolves `${env:VAR}` interpolations in `url`/`headers` if you prefer
  not to paste tokens (`docs/mcp` "Config interpolation"). Claude Code supports
  `${VAR}` in `headers` too, so agents can be pointed at `CAM_TOKEN` instead of
  inlining the token.
- Header propagation into CAM was already covered by tests (the bearer
  middleware reads `Authorization` on both tools and initialize requests;
  `X-CAM-Agent` supplies the agent name for non-harness callers).

## Tool-call timeout behavior per harness (evidence-based, from docs)

CAM's invoke timeout defaults to 60 s (per-service override available). Findings:

| Harness | Tool-call timeout | What it means for CAM |
| --- | --- | --- |
| Claude Code CLI | `MCP_TOOL_TIMEOUT` env (docs default ≈ 28 h) plus a per server `timeout` in `.mcp.json`; since v2.1.187 remote calls abort after 5 min of **no response and no progress** (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, override) | A 60 s CAM timeout completes well inside the harness window. Since CAM answers (result or CAM error envelope) within its own timeout, the never-silent behavior also avoids the idle-abort. |
| Claude Code desktop app | BUG (anthropics/claude-code#63379, open as of May 2026): hard ~60 s cancel that ignores `MCP_TOOL_TIMEOUT` and per-server `timeout` | Tools near CAM's 60 s ceiling can be cancelled by the desktop app before CAM answers. Keep pilot services' slow tools under ~50 s or point users at the CLI. |
| Codex | Per-server `tool_timeout_sec` in `config.toml` (configurable; docs' examples use 60); also `startup_timeout_sec` | If a team needs calls over 60 s, Codex lets them raise `tool_timeout_sec`, and CAM can raise the service timeout to match. |
| Cursor | No user-configurable tools/call timeout. IDE path uses a much longer internal limit (community: ~60 min); `cursor-agent`/ACP path hardcodes the TypeScript SDK's 60 s and does not reset on progress (tracked forum bug, confirmed by Cursor staff Jul 2026) | The same practical rule as Claude desktop: tools that regularly take >60 s should return a job/poll pattern (quick response + poll) or shorter CAM timeouts. |

Bottom line for the open architecture question ("is any harness timeout shorter
than CAM's 60 s?"): yes — the Claude Code desktop app and Cursor's ACP/cli path
cancel at ~60 s regardless of configuration; Codex and the Claude Code CLI allow
more. Recommendation recorded for the owner: keep CAM's per-service default at
60 s, prefer sub-60 s tools for interactive services, and document the
"return-fast-job-then-poll" pattern for slow workflows in onboarding.
The architecture "Open items" checkbox is left for the owner to tick after this
sign-off.

## Routing checks (docs-based)

- CAM sends **server instructions** at `initialize` telling the agent to use
  `cam` for anything external — Codex docs confirm Codex reads the MCP
  `instructions` field as server-wide guidance; Claude Code and Cursor surface
  MCP server instructions to the model (server tools load with descriptions).
- CAM builds a **per-user `cam` tool description** listing the caller's services
  and skills on every `tools/list`; both Claude Code and Cursor fetch tool
  descriptions per session so the agent always sees its reachable set.
- Remove direct MCP servers that duplicate CAM services from the harness config
  (`claude mcp remove ...`, delete the Cursor `mcp.json` entry, or set
  `enabled_tools`/`disabled_tools` in Codex) — otherwise the agent can bypass
  CAM's audit.
- Per-token auth: each harness entry should use its own labelled CAM token so
  audit logs attribute calls per machine (e.g. `priya-codex`, `cursor-laptop`).
