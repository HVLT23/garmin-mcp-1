# garmin-mcp

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes the
user's Garmin Connect data — activities, wellness signals (sleep, HRV, body battery, stress,
steps), and training metrics (training status, readiness, VO2 max, training load) — so an
AI assistant can analyse training sessions in the context of daily-life signals. A small
write surface also lets an assistant push structured running workouts onto the user's
Garmin calendar (Runna-style coaching).

> **Personal-use disclaimer.** Garmin Connect has no public API for this data. This server is
> built on top of [`garminconnect`](https://github.com/cyberjunky/python-garminconnect), which
> reverse-engineers the same private endpoints the official Garmin Connect web app uses. It is
> intended for single-user personal automation against your own account, may break when Garmin
> changes endpoints, and is provided as-is.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (`pipx install uv` or follow the official installer)
- A Garmin Connect account (2FA / MFA supported)

## Install

```bash
uv sync
```

## First-time auth

Garmin uses SSO with a one-time MFA code. Tokens are persisted (~1 year) and refreshed
automatically thereafter.

```bash
uv run garmin-mcp auth login
# prompts for email, password, and the MFA code sent to your email/authenticator
```

Tokens land in `$GARMIN_TOKENS_PATH` (default: `~/.config/garmin-mcp/tokens`). To verify:

```bash
uv run garmin-mcp auth status
```

## Run the server

### stdio (for Claude Desktop / Claude Code / IDE clients)

```bash
uv run garmin-mcp serve
```

### HTTP (for remote clients, deployments)

```bash
uv run garmin-mcp serve --transport http --host 0.0.0.0 --port 8000
```

The HTTP transport requires every request to carry `Authorization: Bearer <token>`.
In multi-tenant mode the bearer is looked up in the registry (see
"Multi-tenant deployment" below); in legacy single-tenant mode (registry
empty) it is compared against `MCP_BEARER_TOKEN`. The single exception is
`GET /healthz`, which returns `{"status":"ok"}` without auth so liveness
probes (Fly.io, Docker `HEALTHCHECK`, etc.) can hit it anonymously.

## Configuration

All settings can be supplied via environment variables (or a `.env` file in the working
directory — see `.env.example`).

| Variable                | Default                              | Purpose                                                                 |
| ----------------------- | ------------------------------------ | ----------------------------------------------------------------------- |
| `GARMIN_EMAIL`          | —                                    | Garmin account email (only needed during `auth login` or `admin provision`). |
| `GARMIN_PASSWORD`       | —                                    | Garmin account password (only needed during `auth login` or `admin provision`). |
| `GARMIN_MFA`            | —                                    | If set, used non-interactively as the MFA code during login/provisioning. |
| `GARMIN_TOKENS_PATH`    | `~/.config/garmin-mcp/tokens`        | **Root** under which each user's tokens live (`<root>/<user_id>/`). In single-tenant mode, the v1 layout (tokens directly under the root) still works as a one-time migration. |
| `GARMIN_REGISTRY_PATH`  | `/data/tokens/registry.json`         | JSON file mapping `sha256(bearer) → user_id`. Lives inside the tokens volume so it survives restarts. Empty/missing file = legacy single-bearer mode. |
| `MCP_TRANSPORT`         | `stdio`                              | `stdio` or `http`.                                                      |
| `MCP_HOST`              | `127.0.0.1`                          | HTTP bind host. The Docker image overrides this to `0.0.0.0`.           |
| `MCP_PORT`              | `8000`                               | HTTP bind port.                                                         |
| `MCP_BEARER_TOKEN`      | —                                    | Legacy single-bearer fallback. Used only when the registry is empty.    |
| `MCP_ALLOW_UNAUTHENTICATED` | `0`                              | Set to `1` to permit HTTP transport without registry or bearer (refused otherwise). |
| `GARMIN_MCP_NO_CACHE`   | —                                    | Set to `1` to disable in-process TTL caching.                           |

`XDG_CONFIG_HOME` is honoured when `GARMIN_TOKENS_PATH` is unset.

## Tools

Most tools are **read-only**; the four `Write` tools below are the only ones that mutate
Garmin state. Date arguments are ISO `YYYY-MM-DD`; date-defaulted tools fall back to today.

### Activities

- `list_recent_activities(limit, start)` — most-recent activities, newest first.
- `get_activity(activity_id)` — full activity details.
- `get_activity_splits(activity_id)` — per-lap breakdown.
- `get_activity_hr_zones(activity_id)` — time-in-HR-zone for an activity.
- `search_activities_by_type(type, start_date, end_date, limit)` — filter by activity type and date range.

### Wellness

- `get_sleep(date)` — sleep stages, duration, score.
- `get_hrv(date)` — HRV summary.
- `get_body_battery(start_date, end_date)` — body battery values across a date range.
- `get_stress(date)` — stress timeline.
- `get_steps(date)` — 15-minute step buckets.
- `get_daily_summary(date)` — daily user summary (steps, calories, RHR, intensity minutes…).

### Training

- `get_training_status(date)` — productive / maintaining / detraining / etc.
- `get_training_readiness(date)` — morning readiness score and contributing factors.
- `get_vo2_max(date)` — running and cycling VO2 max estimates.
- `get_training_load(date)` — ATL/CTL/TSB-equivalent fields distilled from training status.
- `get_race_predictor()` — race-time predictions across standard distances.

### Aggregate

- `get_session_with_context(activity_id)` — one-shot bundle: activity + splits + HR zones +
  prior-night sleep + prior-day HRV + morning body battery + morning readiness + training
  load. The "prior night" date is derived from the activity's start time (if it began before
  04:00, the prior night = night of the day before yesterday).

### Write tools (running workouts)

These tools hit Garmin's unofficial workout endpoints. The endpoints have no SLA, no
published contract, and the schema can drift without notice — treat success as best-effort.
Scope is intentionally narrow: running only, no other sports, no wellness writes, no manual
activity logging.

- `schedule_running_workout(date, name, steps, description="")` — upload a structured
  running workout and place it on the calendar on `date`. Returns
  `{workout_id, scheduled_workout_id, date, name}`.
- `unschedule_workout(scheduled_workout_id)` — remove the calendar entry. The template
  stays in the library.
- `delete_workout(workout_id)` — delete the template (also removes any calendar entry).
- `list_scheduled_workouts(year, month, verbose=False)` — list scheduled calendar items
  for a given month (`month` is 1–12, the natural human form). The response is trimmed
  by default; pass `verbose=True` for the un-modified upstream payload.

**Important:** `unschedule_workout` takes `scheduled_workout_id`; `delete_workout` takes
`workout_id`. They are different IDs returned by `schedule_running_workout` — don't mix
them up.

**Editing a workout.** Garmin's API has no `update_workout` operation. To "edit" a
scheduled workout: `unschedule_workout(scheduled_workout_id)` →
`delete_workout(workout_id)` → call `schedule_running_workout` again with the new
parameters.

#### The `steps` shape

`steps` is a flat list of step dicts. Supported `type` values: `warmup`, `interval`,
`recovery`, `cooldown`, `repeat`. For `repeat`, supply `iterations` and a nested `steps`
list.

Non-repeat step fields:

| Field          | Values                                                                            |
| -------------- | --------------------------------------------------------------------------------- |
| `end_condition`| `time` (seconds), `distance` (meters), `heart_rate` (bpm), `calories`, `cadence`  |
| `end_value`    | numeric — interpreted per `end_condition`                                         |
| `target_type`  | one of the seven values below (optional, default `none`)                          |
| `zone`         | integer 1–5; required when `target_type` is a `*_zone` variant                    |
| `target_low`   | numeric lower bound; required when `target_type` is a range variant               |
| `target_high`  | numeric upper bound; required when `target_type` is a range variant               |
| `description`  | freeform note attached to the step (optional)                                     |

Each `target_type` has exactly one valid input shape. Passing fields from the wrong shape
is rejected with a `bad_argument` error that names the target_type you probably meant:

| `target_type`     | required fields                          | meaning                            |
| ----------------- | ---------------------------------------- | ---------------------------------- |
| `heart_rate_zone` | `zone` (int 1–5)                         | run in HR zone N                   |
| `heart_rate`      | `target_low`, `target_high` (bpm)        | run in a custom bpm range          |
| `pace_zone`       | `zone` (int 1–5)                         | run in pace zone N                 |
| `pace`            | `target_low`, `target_high` (m/s)        | run in a custom m/s pace range     |
| `cadence`         | `target_low`, `target_high` (spm)        | run in a custom cadence range      |
| `open`            | _(none)_                                 | open / freeform target             |
| `none`            | _(none)_                                 | no target                          |

HR-zone and pace-zone encodings (single `zoneNumber` on the wire), HR-range and pace-range
encodings (`targetValueOne/Two` in bpm and m/s respectively) are all verified live against
Garmin Connect. `cadence` is **unverified live** — if Garmin renders a cadence target as
a raw spm range instead of a zone label, it likely needs the same `zone` treatment as HR.

#### Example 1 — easy run (HR zone)

10-min warmup, 30 min easy in HR zone 2, 5-min cooldown:

```python
schedule_running_workout(
    date="2026-05-26",
    name="Easy run",
    steps=[
        {"type": "warmup",   "end_condition": "time", "end_value": 600},
        {"type": "interval", "end_condition": "time", "end_value": 1800,
         "target_type": "heart_rate_zone", "zone": 2},
        {"type": "cooldown", "end_condition": "time", "end_value": 300},
    ],
)
```

#### Example 2 — interval session (custom pace range)

10-min warmup, 6× (800 m at 4:00–4:10 / km pace + 2 min jog recovery), 5-min cooldown.
Pace targets are in metres per second: 4:00/km = 1000 / 240 ≈ 4.17 m/s,
4:10/km = 1000 / 250 = 4.0 m/s.

```python
schedule_running_workout(
    date="2026-05-28",
    name="6 × 800m @ 5k pace",
    steps=[
        {"type": "warmup", "end_condition": "time", "end_value": 600},
        {
            "type": "repeat",
            "iterations": 6,
            "steps": [
                {"type": "interval", "end_condition": "distance", "end_value": 800,
                 "target_type": "pace", "target_low": 4.0, "target_high": 4.17},
                {"type": "recovery", "end_condition": "time", "end_value": 120},
            ],
        },
        {"type": "cooldown", "end_condition": "time", "end_value": 300},
    ],
)
```

#### Example 3 — tempo at a custom HR range

20-min warmup, 30 min at 150–165 bpm, 10-min cooldown:

```python
schedule_running_workout(
    date="2026-05-30",
    name="Tempo @ HR 150–165",
    steps=[
        {"type": "warmup",   "end_condition": "time", "end_value": 1200},
        {"type": "interval", "end_condition": "time", "end_value": 1800,
         "target_type": "heart_rate", "target_low": 150, "target_high": 165},
        {"type": "cooldown", "end_condition": "time", "end_value": 600},
    ],
)
```

#### Example 4 — pace zone

5-min warmup, 10 min in pace zone 4, 5-min cooldown:

```python
schedule_running_workout(
    date="2026-05-31",
    name="Zone 4 pace",
    steps=[
        {"type": "warmup",   "end_condition": "time", "end_value": 300},
        {"type": "interval", "end_condition": "time", "end_value": 600,
         "target_type": "pace_zone", "zone": 4},
        {"type": "cooldown", "end_condition": "time", "end_value": 300},
    ],
)
```

## Claude Desktop config

Add an entry to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/garmin-mcp", "run", "garmin-mcp", "serve"]
    }
  }
}
```

Alternatively, install the script globally with `uv tool install .` and use the
`garmin-mcp` entry point directly.

## Docker

The image runs HTTP transport by default, listens on `0.0.0.0:8000`, drops to a non-root
user, and reads tokens from a mounted `/data/tokens` volume. Credentials are never baked
into the image.

```bash
docker build -t garmin-mcp .
```

**One-shot token bootstrap** — run `auth login` interactively against a named volume
(or a host bind mount), entering credentials and the MFA code at the prompt:

```bash
docker volume create garmin-tokens
docker run --rm -it \
  --entrypoint garmin-mcp \
  -v garmin-tokens:/data/tokens \
  -e GARMIN_TOKENS_PATH=/data/tokens \
  garmin-mcp auth login
```

**Run the server** with the tokens volume mounted and a bearer token enforced:

```bash
docker run --rm -p 8000:8000 \
  -v garmin-tokens:/data/tokens \
  -e MCP_BEARER_TOKEN="$(openssl rand -hex 32)" \
  garmin-mcp
```

(If you'd rather bootstrap on the host: run `garmin-mcp auth login` locally and bind-mount
`~/.config/garmin-mcp/tokens` to `/data/tokens` instead of using a named volume.)

## Deployment (Fly.io)

The repo ships a `fly.toml` and Dockerfile tuned for a single-tenant Fly.io
deployment: one always-on `shared-cpu-1x` machine with 256 MB RAM, a 1 GB
volume for tokens, and an HTTP healthcheck against `/healthz`.

**Prerequisites**

- A Fly.io account and `flyctl` installed (`brew install flyctl` or follow
  the [official installer](https://fly.io/docs/flyctl/install/)).
- Tokens already bootstrapped locally (`uv run garmin-mcp auth login`).
  Tokens land in `~/.config/garmin-mcp/tokens` by default.

**One-time setup**

```bash
# Pick an app name; Fly will reserve <name>.fly.dev for you.
fly launch --no-deploy --name <app-name> --region waw --copy-config

# Generate and store the bearer token Fly will inject as MCP_BEARER_TOKEN.
fly secrets set MCP_BEARER_TOKEN=$(openssl rand -hex 32)
```

> **Note**: `fly launch --copy-config` rewrites `app =` in `fly.toml` to your
> chosen app name. **Don't commit that change** — either revert it
> (`git checkout fly.toml`) or use `fly launch --reuse-app` if you've already
> created the app via the Fly dashboard.

`fly.toml` declares the volume (`garmin_tokens`, mounted at `/data/tokens`)
with `initial_size = "1gb"`, so the volume is created automatically on the
first deploy. If you'd rather create it explicitly:

```bash
fly volumes create garmin_tokens --size 1 --region waw
```

**Bootstrap tokens onto the volume**

After the first deploy, Fly will have created an empty `/data/tokens` on
the volume. Copy the local tokens up via SSH SFTP:

```bash
fly ssh sftp shell
> put -r /home/you/.config/garmin-mcp/tokens /data
> exit
```

(`put -r tokens /data` copies the directory in, leaving `/data/tokens` —
matching `GARMIN_TOKENS_PATH`.)

**Deploy**

```bash
fly deploy
```

**Verify**

```bash
# Anonymous — should return {"status":"ok"}.
curl https://<app-name>.fly.dev/healthz

# Authenticated MCP endpoint — should not 401.
curl -H "Authorization: Bearer $MCP_BEARER_TOKEN" https://<app-name>.fly.dev/mcp
```

**Rotation**

- *Garmin tokens*: re-run `garmin-mcp auth login` locally, then re-upload
  via `fly ssh sftp shell` (overwrites the volume copy).
- *Bearer token*: `fly secrets set MCP_BEARER_TOKEN=<new>`; Fly restarts
  the machine. Update any clients that hold the old token.

### Continuous deployment

`.github/workflows/deploy.yml` redeploys the Fly app (`garmin-mcp-nrlo2w`)
on every push to `main`. It installs `flyctl` on the runner and runs
`flyctl deploy -a garmin-mcp-nrlo2w --remote-only`; the `-a` flag is passed
explicitly because `fly.toml` ships the placeholder `app = "garmin-mcp"`.

**Required secret.** Add a `FLY_API_TOKEN` to the repo's GitHub Actions
secrets. Prefer a scoped, deploy-only token (cannot run `flyctl secrets`,
`flyctl destroy`, etc.):

```bash
flyctl tokens create deploy -a garmin-mcp-nrlo2w --expiry 8760h   # 1 year
gh secret set FLY_API_TOKEN -R kgabryje/garmin-mcp                # paste the token
```

**Skipping a deploy.** Either push to a non-`main` branch, or include
`[skip deploy]` anywhere in the commit message (useful for docs-only
changes that don't affect the runtime image).

**Manual re-trigger.** The workflow accepts `workflow_dispatch`, so a
deploy can be kicked off from the Actions UI's "Run workflow" button on
the *Deploy to Fly* workflow without pushing a no-op commit.

**Concurrency.** If a second commit lands on `main` while an earlier
deploy is still in flight, the older deploy is cancelled and the new one
takes over — guarantees the latest `main` is what's actually deployed.

## Multi-tenant deployment

The HTTP transport supports multiple Garmin accounts on a single deployment.
Each tenant has their own bearer token (so workspace members can no longer
read each other's data) and their own per-user tokens directory under
`<GARMIN_TOKENS_PATH>/<user_id>/`. Provisioning is admin-mediated: the
operator runs the SSO flow once per user and hands the user a fresh bearer.

### Registry file format

A JSON file at `GARMIN_REGISTRY_PATH` (default `/data/tokens/registry.json`,
inside the persistent tokens volume) maps
`sha256(bearer) → user_id`:

```json
{
  "users": [
    {"user_id": "alice", "bearer_sha256": "<64-char hex digest>"},
    {"user_id": "bob",   "bearer_sha256": "<64-char hex digest>"}
  ]
}
```

The file is hot-reloaded on mtime change — adding or removing a user does
not require a server restart. When the registry is empty or missing the
server falls back to legacy single-bearer mode (see Migration below).

### Provisioning

```bash
# On the server (or any host with the volume mounted):
garmin-mcp admin provision --user-id alice
#   prompts for email, password, and MFA (or reads GARMIN_EMAIL/GARMIN_PASSWORD/GARMIN_MFA)
#   writes tokens to <root>/alice/
#   atomically appends {alice, sha256(bearer)} to the registry
#   prints the bearer to stdout exactly ONCE — copy it now, it can't be recovered
```

```bash
garmin-mcp admin list                     # show user_ids + sha256 prefixes
garmin-mcp admin revoke --user-id alice   # remove from registry
garmin-mcp admin revoke --user-id alice --purge-tokens   # also rm -rf the tokens dir
```

The bearer is **never persisted in plaintext** — only its sha256 lives in
the registry. Lose it and you must re-provision.

### Audit log

Every tool call emits one line to stderr:

```
2026-05-09 12:34:56 INFO garmin_mcp.audit: ts=2026-05-09T12:34:56 user=alice tool=list_recent_activities
```

Tool arguments are deliberately not logged.

### Migration from v1 (single-tenant)

Phases 1–3 of the migration ship the registry-aware middleware while
keeping legacy single-bearer mode active. Owner traffic is unaffected
until they explicitly switch over.

1. **Deploy the new code with no registry file yet.** The server detects
   the empty registry, falls back to comparing the request bearer against
   `MCP_BEARER_TOKEN` (the v1 secret), and tags the request as the
   `_legacy` user. Tokens at `/data/tokens/garmin_tokens.json` continue
   to be served — no changes needed in this window.

2. **Relocate the v1 tokens to the per-user layout** (one-time, via SSH):

   ```bash
   fly ssh console
   mkdir -p /data/tokens/_legacy
   mv /data/tokens/garmin_tokens.json /data/tokens/_legacy/
   ```

   The server keeps serving the owner from `_legacy/` while the registry
   stays empty.

3. **Provision the owner as a real tenant.** This step retires legacy mode:

   ```bash
   garmin-mcp admin provision --user-id owner
   #   logs in fresh into /data/tokens/owner/
   #   writes /data/tokens/registry.json with the owner's entry
   #   prints the new bearer
   ```

   As soon as `/data/tokens/registry.json` is non-empty the legacy fallback
   auto-disables: any client still using `MCP_BEARER_TOKEN` is rejected.
   Update the owner's MCP-server config with the new bearer.

4. **Add additional tenants.** For each new user:

   ```bash
   garmin-mcp admin provision --user-id <them>
   ```

   Hand them their bearer privately. Their data lives at
   `/data/tokens/<them>/`; cache keys and tokens are isolated.

After step 3 you can `unset` `MCP_BEARER_TOKEN` from the Fly secrets;
it is no longer required.

## Limitations / known issues

- **Stdout discipline relies on the upstream library.** Stdio MCP requires a clean
  stdout for JSON-RPC framing. The server pushes its own logging to stderr, but if
  `garth` ever calls stdlib `print()` during a mid-session token refresh it could
  corrupt the framing. Not observed in practice; eager auth verification at startup
  mitigates the common case.

## Development

```bash
uv sync                 # install deps
uv run pytest           # run unit tests
uv run ruff check .     # lint
uv run mypy src/        # type-check
GARMIN_LIVE_TEST=1 uv run pytest tests/integration  # live smoke test
uv run mcp dev src/garmin_mcp/server.py             # FastMCP Inspector
```

## License

[MIT](LICENSE).
