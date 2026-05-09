# Multi-tenant architecture for garmin-mcp

Status: **proposal** — owner sign-off required before implementation. See §6.

## 1. Problem statement

v1 ships one bearer token and one Garmin token store on a single Fly machine. Two concrete problems:

1. **Cross-tenant leak inside Agor.** The bearer is set in workspace-level MCP-server config, so any workspace member can call the server and read the owner's Garmin data — sleep, HRV, location-tagged activities. There is no per-caller identity at the protocol layer.
2. **No room for additional users.** ≥2 people now want to point this server at *their own* Garmin accounts. The current `client_factory()` returns one process-wide `_LockedGarmin` wrapping one set of tokens; there is no concept of a caller, only "the user".

Both problems are the same problem: **the server has no notion of who is calling it**.

## 2. Architectural options

The MCP spec puts auth strictly at the transport layer ([modelcontextprotocol.io spec][mcp-auth]) — there is no in-protocol `user_id` field, only `Authorization: Bearer <token>` (or full OAuth 2.1 with Protected Resource Metadata + Dynamic Client Registration). Identity has to be derived from the bearer/access token. With that constraint:

### (A) Per-user bearer → per-user volume dir

Each tenant gets an opaque bearer (`gmcp_<32B base64>`). A `BEARER_REGISTRY` env var (or `/data/registry.json`) maps `sha256(bearer) → user_id`. Middleware resolves the caller; `client_factory(user_id)` loads a per-user `_LockedGarmin` from `/data/tokens/<user_id>/`. Bootstrap is admin-mediated: owner runs `garmin-mcp admin provision --user-id alice` over `flyctl ssh`, then hands `alice` her bearer.

### (B) MCP-as-OAuth-IdP (full OAuth 2.1 per the MCP spec)

Server implements RFC 9728 / RFC 8414 metadata endpoints, `/authorize`, `/token`, RFC 7591 dynamic client registration, and PKCE. First-time login is a browser flow that collects Garmin email/password/MFA, runs the python-garminconnect SSO under the hood, persists Garmin tokens, and mints a short-lived access token. Same shape as Notion MCP (`auth_type: oauth, oauth_mode: per_user`). Caveat: Garmin has no public OAuth, so we still wrap reverse-engineered SSO — the OAuth layer is around *us*, not federated.

### (C) Pass-through credentials at request time

Server stateless w.r.t. user data. Caller supplies a `X-Garmin-Tokens` header containing their full OAuth1+OAuth2 token JSON on every request. Server builds a fresh `Garmin` per request, services it, drops it. Nothing user-identifying on the volume.

### (D) Encrypted-at-rest with per-user keys

Same `/data/tokens/<user_id>/` layout as (A), but tokens are AES-GCM encrypted. The key is never on the server — bearer becomes `<user_id>.<base64-key>`, middleware splits, decrypts in-memory, builds the client. Stealing the volume alone yields only ciphertext.

### (E) Per-user volume + Agor session-JWT verification *(added)*

Agor sessions carry an `mcp_token` JWT whose claims include `sub` (session) and `uid` (Agor user) — visible on `agor_sessions_get_current`. If we validate this JWT against an Agor JWKS, the Agor `uid` becomes the tenant key — no admin-managed bearer registry, automatic deprovisioning when Agor disables the user. Only works inside Agor; non-Agor clients (Claude Desktop, Cursor) need a fallback.

## 3. Trade-off matrix

| Dimension | A: per-user bearer | B: full OAuth 2.1 | C: pass-through | D: encrypted-at-rest | E: Agor JWT |
|---|---|---|---|---|---|
| **Bootstrap UX** | Admin-mediated; one `flyctl ssh` per user | Self-serve browser flow (after first-time Garmin SSO inside it) | Caller manages tokens client-side; ugly to paste ~4KB JSON | Admin runs login + emits one combined string per user | Self-serve inside Agor; non-Agor users need fallback |
| **Compromise blast radius** (server volume stolen) | All users' Garmin tokens leaked | All users' Garmin tokens leaked | **Zero** — nothing persisted | **Zero plaintext** — needs a live bearer to decrypt anything | All users' Garmin tokens leaked |
| **Ops cost / user** | ~5 min provision, manual rotate | ~0 after deploy; users self-rotate | 0 server-side; all on caller | ~5 min provision; key-rotation = re-login | ~0; piggy-backs Agor lifecycle |
| **Code complexity** | ~150 LOC (registry + middleware + factory plumbing) | ~800–1200 LOC (OAuth 2.1, DCR, PKCE, metadata endpoints, browser-flow Garmin SSO) | ~80 LOC, but burdens every caller | ~250 LOC (A + envelope encryption) | ~200 LOC (JWKS verify + factory plumbing); rises if fallback to A is added |
| **Migration cost** | Owner's existing bearer becomes user `owner`; tokens move from `/data/tokens/garmin_tokens.json` → `/data/tokens/owner/garmin_tokens.json`. Trivial. | High — requires a browser flow that does not exist yet, and re-onboarding the owner | Low — owner now uploads tokens with each call; client config rewrite | Medium — owner needs to pick a key, re-encrypt | Low if owner is in Agor; needs fallback otherwise |
| **Suitability ≥2 users / hypothetical 100** | Excellent for ≤20; provisioning toil grows linearly past that | Excellent at any scale; built to scale | Fine at any scale, but unusable from clients that can't easily inject ~4KB headers | Same as A on bootstrap; better security ceiling | Excellent inside Agor; needs (A) bolted on to leave Agor |

## 4. Recommendation

**Option A — per-user bearer with per-user volume directories.**

Least code, simplest ops, solves both stated problems. The owner has 2–5 concrete users in mind, not 100. Full OAuth 2.1 (B) is the spec-recommended answer and what Notion does, but it's a 5–10× implementation lift and requires a browser-based first-time Garmin SSO that doesn't exist today — buys nothing the owner currently needs. Pass-through (C) shifts the security responsibility to every caller and breaks the "set up once" UX. Encryption-at-rest (D) layers cleanly onto (A) — same on-disk shape — so defer it. Agor-JWT (E) is attractive for Agor-only callers but couples this server to one client.

### Implementation phases

| Phase | Scope | Rough effort | Depends on |
|---|---|---|---|
| **1 — bearer registry & middleware** | `BEARER_REGISTRY` env var (newline-separated `user_id:sha256_hex`); middleware looks up `sha256(bearer)`, stores `user_id` in request scope. Legacy single-bearer mode preserved when registry is empty. | ~0.5 day | none |
| **2 — per-user token directories** | `Settings.garmin_tokens_path` → `garmin_tokens_root`. New `_PerUserClientCache` lazy-loads one `_LockedGarmin` per user under a per-user lock. Cache key in `cache.py` extended to `(user_id, name, args, kwargs)` so users don't share cached responses. | ~1 day | Phase 1 |
| **3 — tool plumbing** | 17 tools currently close over `client_factory`; switch to reading `user_id` from a `contextvars.ContextVar` set by middleware (propagates across `anyio.to_thread.run_sync`). Tool bodies stay one-liners. | ~0.5 day | Phase 2 |
| **4 — provisioning UX** | `garmin-mcp admin provision --user-id alice --email …`: runs SSO into `/data/tokens/alice/`, mints a bearer, prints `alice:<sha256>` (for registry) and the bearer (for the user). `admin revoke <user_id>` does the inverse. | ~1 day | Phase 1 |
| **5 — docs & rollout** | README, migration doc, `fly.toml` env. | ~0.5 day | 1–4 |

Total: ~3–4 dev days. Phases 1, 2, 3 must be sequential. Phase 4 can be done in parallel with 2/3.

```python
# Sketch of the new middleware (illustrative — not real code):
def _bearer_middleware(app, registry: dict[str, str]):
    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        token = _parse_bearer_header(_get_header(scope, "authorization"))
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        user_id = registry.get(digest)
        if user_id is None:
            return await _send_401(send)
        scope.setdefault("state", {})["user_id"] = user_id
        _CURRENT_USER.set(user_id)  # ContextVar consumed by client_factory
        await app(scope, receive, send)
    return asgi
```

## 5. Migration plan

Strategy: **legacy single-bearer mode stays the default until `BEARER_REGISTRY` is populated.**

1. Deploy phases 1–3 with `BEARER_REGISTRY` unset. Bearer middleware behaves as today; `user_id` defaults to `"_legacy"`. One `flyctl ssh` + `mv` relocates `/data/tokens/*` into `/data/tokens/_legacy/`. Owner's traffic unchanged throughout.
2. Owner runs `garmin-mcp admin provision --user-id owner …`, gets a registry line and a fresh bearer; updates Agor MCP-server config. Subsequent users: repeat for each.
3. Once the registry is non-empty the server rejects any bearer not in it — legacy fallback auto-disables. `_legacy/` can be deleted.

The owner's existing bearer + tokens keep working as the first user throughout steps 1–2; step 2 is a one-time switchover.

## 6. Open questions for the owner

1. **Provisioning: admin-mediated or self-serve?** Recommendation assumes admin-mediated. Self-serve would require option B (~10× lift). **Stick with admin-mediated for v2?**
2. **Encryption-at-rest: now or later?** ~100 LOC; users must store a key. **Defer?**
3. **Registry storage: env var or `/data/registry.json`?** Env var = simpler but Fly redeploy per add; file = hot-reloadable. **Which?**
4. **Non-Agor clients (Claude Desktop, Cursor) in scope?** If no, option E becomes attractive. **In scope for v2?**
5. **Audit logging — log `(ts, user_id, tool_name)` to stderr in phase 1?** Cheap and forensically valuable. **Y/N?**
6. **Per-user MFA prompt path needed**, or is provisioning being interactive (one `GARMIN_MFA` per `provision` invocation) sufficient?

[mcp-auth]: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
