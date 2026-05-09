# Onboarding additional users

This server is multi-tenant. Each user has their own bearer and their own
Garmin tokens, isolated on the persistent volume. This guide is for the
**admin** (whoever controls the Fly app) provisioning a new user.

For the architectural reasoning behind multi-tenancy, see
[`multitenant-design.md`](./multitenant-design.md).

## Prerequisites

You (admin) need:

- `flyctl` installed and authenticated to the app's Fly account
- SSH access to the deployed machine: `fly ssh console --app <app-name>`
- The new user's Garmin email + password — or, ideally, the user on a call
  so they enter password and MFA code themselves

The new user needs:

- A Garmin Connect account with the data they want to access (their own
  activities, sleep, etc.)
- Access to the email inbox tied to that account (for the MFA code)

## Provisioning flow

### 1. Run `admin provision` over SSH

```bash
fly ssh console --app garmin-mcp-nrlo2w
cd /app
uv run garmin-mcp admin provision --user-id <username>
```

`<username>` is an opaque identifier you choose. Recommended:
lowercase, alphanumeric plus `-`/`_`, no leading `.`. Examples:
`alice`, `bob`, `team-1`. The path-traversal guard rejects anything
containing `/`, `..`, or starting with `.`.

The CLI prompts for:

1. **Email** — accepts `GARMIN_EMAIL` env var as override
2. **Password** (hidden) — accepts `GARMIN_PASSWORD`
3. **MFA code** — Garmin emails it after step 2; the user reads it from
   their inbox. Accepts `GARMIN_MFA`.

On success it:

- Writes the user's OAuth tokens to `/data/tokens/<username>/garmin_tokens.json`
  (mode 0600, owned by the `mcp` runtime user)
- Generates a fresh bearer like `gmcp_<43-char-base64url>`
- Atomically appends a `{user_id, bearer_sha256}` entry to
  `/data/tokens/registry.json`
- **Prints the bearer once on stdout.** It's never written to disk in
  plaintext, only as a SHA-256 hash in the registry. Copy it before
  closing the terminal.

### 2. Hand the bearer to the user securely

Don't paste in shared chat history. Use a password manager, a one-time
secret link (1ty.me, OneTimeSecret), or share in person.

### 3. The user adds it to their MCP client

#### Agor

Settings → MCP Servers → Add server (or edit existing entry):

| Field | Value |
|---|---|
| Name | `garmin-mcp` |
| URL | `https://<your-app>.fly.dev/mcp` |
| Transport | `http` (streamable HTTP) |
| Auth type | `Bearer` |
| Bearer token | the `gmcp_*` value |

> **Direct-paste the bearer**, don't use Agor's `{{ user.env.X }}`
> template syntax. As of 2026-05-09 the substitution is broken for the
> MCP-server bearer field — it sends the literal template string and
> the server rejects it with 401. See the orchestration memory at
> `reference_agor_mcp_bearer_template.md` for context.

#### Claude Desktop / Cursor / other MCP clients

Standard streamable-HTTP MCP config with:

```json
{
  "url": "https://<your-app>.fly.dev/mcp",
  "headers": { "Authorization": "Bearer gmcp_<...>" }
}
```

### 4. Verify

The user can sanity-check by spawning a session and asking the model to
"summarize my last training session" or list recent activities. If real
Garmin data comes back, the per-user routing is working.

If 401: bearer pasted incorrectly (look for stray whitespace) or registry
doesn't contain the entry. SSH in and run `admin list` to confirm the
user_id exists.

## Lifecycle operations

All `admin` commands run via `fly ssh console --command 'cd /app && uv run garmin-mcp admin <subcommand>'` or interactively from a SSH shell.

### List users

```bash
uv run garmin-mcp admin list
```

Prints user_ids, sha256 prefixes (first 8 chars), and last-modified
timestamps. Doesn't leak bearer values. All output goes to stderr.

### Revoke a user

```bash
uv run garmin-mcp admin revoke --user-id <username>
```

Removes the registry entry. Their bearer immediately stops working —
next request returns 401. Tokens dir at `/data/tokens/<username>/`
remains intact in case you want to inspect or re-provision.

To wipe tokens too:

```bash
uv run garmin-mcp admin revoke --user-id <username> --purge-tokens
```

`rm -rf` on the tokens dir. The same path-traversal guard as `provision`
rejects malformed user_ids before deleting anything.

### Re-provision (rotate bearer or refresh tokens)

If a bearer is suspected leaked, or stored Garmin tokens have expired
beyond the OAuth1 refresh window (~1 year):

```bash
uv run garmin-mcp admin revoke --user-id <username>
uv run garmin-mcp admin provision --user-id <username>
```

User provides MFA again. Old bearer invalid; new bearer takes its place.
The user updates their MCP client config with the new value.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 unauthorized` on every request | Bearer wrong, or registry doesn't contain the user | `admin list` to confirm user_id; verify bearer pasted exactly (no whitespace) |
| `auth_expired` on tool calls but bearer is correct | User's Garmin OAuth1 token expired (~1 year) | Re-provision (revoke → provision) |
| `503 registry_corrupt` on every request | `/data/tokens/registry.json` was hand-edited and broke the format | SSH in, restore from a copy, or `admin provision` a user (atomically rewrites the file) |
| Tool returns wrong user's data | Shouldn't be possible — cache key includes `user_id`. If you see this, file an issue with reproduction steps |

## Backup / disaster recovery

The registry and all per-user tokens live on the Fly volume mount
(`/data/tokens/`). Fly takes daily volume snapshots (5-day retention by
default per `fly.toml`); roll back via:

```bash
fly volumes snapshots list <volume-id>
fly volumes snapshots restore <snapshot-id>
```

For a manual backup: `fly ssh sftp get -r /data/tokens ./backup-$(date +%F)/`.
The directory is small (a few KB per user); a flat copy is fine.
