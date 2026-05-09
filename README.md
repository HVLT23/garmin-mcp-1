# garmin-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes the
user's Garmin Connect data — activities, wellness signals (sleep, HRV, body battery, stress,
steps), and training metrics (training status, readiness, VO2 max, training load) — so an
AI assistant can analyse training sessions in the context of daily-life signals.

> **Personal-use disclaimer.** Garmin Connect has no public API for this data. This server is
> built on top of [`garminconnect`](https://github.com/cyberjunky/python-garminconnect), which
> reverse-engineers the same private endpoints the official Garmin Connect web app uses. It is
> intended for single-user personal automation against your own account, may break when Garmin
> changes endpoints, and is provided as-is.

## Prerequisites

- Python 3.11+
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

When `MCP_BEARER_TOKEN` is set, the HTTP transport rejects requests without a matching
`Authorization: Bearer <token>` header.

## Configuration

All settings can be supplied via environment variables (or a `.env` file in the working
directory — see `.env.example`).

| Variable                | Default                              | Purpose                                                                 |
| ----------------------- | ------------------------------------ | ----------------------------------------------------------------------- |
| `GARMIN_EMAIL`          | —                                    | Garmin account email (only needed during `auth login`).                 |
| `GARMIN_PASSWORD`       | —                                    | Garmin account password (only needed during `auth login`).              |
| `GARMIN_MFA`            | —                                    | If set, used non-interactively as the MFA code during `auth login`.     |
| `GARMIN_TOKENS_PATH`    | `~/.config/garmin-mcp/tokens`        | Where OAuth tokens are persisted.                                       |
| `MCP_TRANSPORT`         | `stdio`                              | `stdio` or `http`.                                                      |
| `MCP_HOST`              | `0.0.0.0`                            | HTTP bind host.                                                         |
| `MCP_PORT`              | `8000`                               | HTTP bind port.                                                         |
| `MCP_BEARER_TOKEN`      | —                                    | If set with HTTP transport, requires this bearer token on requests.     |
| `GARMIN_MCP_NO_CACHE`   | —                                    | Set to `1` to disable in-process TTL caching.                           |

`XDG_CONFIG_HOME` is honoured when `GARMIN_TOKENS_PATH` is unset.

## Tools

All tools are **read-only**. Date arguments are ISO `YYYY-MM-DD`; date-defaulted tools fall
back to today.

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

```bash
docker build -t garmin-mcp .
# Bootstrap tokens locally, then mount them into the container:
docker run --rm -p 8000:8000 \
  -v ~/.config/garmin-mcp/tokens:/data/tokens \
  -e MCP_BEARER_TOKEN="$(openssl rand -hex 32)" \
  garmin-mcp
```

The image runs HTTP transport by default. Tokens must be bootstrapped on the host (or in a
disposable container with `auth login`) and mounted into `/data/tokens`. Credentials are
never baked into the image.

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
