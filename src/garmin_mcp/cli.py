"""Click CLI: `garmin-mcp auth login`, `auth status`, `serve`, `admin ...`."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import sys
from pathlib import Path

import click

from garmin_mcp.auth import (
    AuthError,
    load_client,
    login_interactive,
    make_mfa_prompt,
    verify,
)
from garmin_mcp.config import load_settings
from garmin_mcp.registry import (
    Registry,
    RegistryError,
    load_registry,
    write_registry_atomic,
)


@click.group()
@click.version_option(package_name="garmin-mcp")
def main() -> None:
    """Garmin Connect MCP server."""


@main.group()
def auth() -> None:
    """Manage Garmin Connect authentication tokens."""


@auth.command("login")
@click.option("--email", envvar="GARMIN_EMAIL", default=None,
              help="Garmin Connect email (or set GARMIN_EMAIL).")
@click.option("--password", envvar="GARMIN_PASSWORD", default=None,
              help="Garmin Connect password (or set GARMIN_PASSWORD).")
def auth_login(email: str | None, password: str | None) -> None:
    """Perform a fresh SSO login and persist OAuth tokens."""
    settings = load_settings()
    email = email or click.prompt("Email")
    password = password or click.prompt("Password", hide_input=True)
    try:
        client = login_interactive(
            email=email,
            password=password,
            tokens_path=settings.garmin_tokens_path,
            mfa_prompt=make_mfa_prompt(),
        )
        name = verify(client)
    except AuthError as e:
        click.echo(f"Login failed: {e}\nRemediation: {e.remediation}", err=True)
        sys.exit(1)
    click.echo(f"Logged in as {name}. Tokens saved to {settings.garmin_tokens_path}.")


@auth.command("status")
def auth_status() -> None:
    """Verify that stored tokens still work."""
    settings = load_settings()
    try:
        client = load_client(settings.garmin_tokens_path)
        name = verify(client)
    except AuthError as e:
        click.echo(f"Not authenticated: {e}\nRemediation: {e.remediation}", err=True)
        sys.exit(1)
    click.echo(f"Authenticated as {name}. Tokens at {settings.garmin_tokens_path}.")


@main.command("serve")
@click.option("--transport", type=click.Choice(["stdio", "http"]), default=None,
              help="Transport (or set MCP_TRANSPORT). Default: stdio.")
@click.option("--host", default=None, help="HTTP bind host (or set MCP_HOST).")
@click.option("--port", type=int, default=None, help="HTTP port (or set MCP_PORT).")
def serve_cmd(transport: str | None, host: str | None, port: int | None) -> None:
    """Run the MCP server."""
    from garmin_mcp.server import serve

    settings = load_settings()
    if transport:
        settings.mcp_transport = transport  # type: ignore[assignment]
    if host:
        settings.mcp_host = host
    if port:
        settings.mcp_port = port

    serve(settings)


@main.group()
def admin() -> None:
    """Multi-tenant administration (provision / revoke / list users)."""


def _registry_entries(registry: Registry) -> list[dict[str, str]]:
    return [
        {"user_id": user_id, "bearer_sha256": digest}
        for user_id, digest in registry.entries()
    ]


def _resolve_registry_path(explicit: Path | None, settings) -> Path:
    if explicit is not None:
        return explicit
    return load_registry(settings.garmin_registry_path).path


def _mint_bearer() -> str:
    """Generate a fresh opaque bearer token with a project-namespaced prefix."""
    return f"gmcp_{secrets.token_urlsafe(32)}"


@admin.command("provision")
@click.option("--user-id", required=True, help="Unique tenant identifier (e.g. 'alice').")
@click.option("--email", envvar="GARMIN_EMAIL", default=None,
              help="Garmin Connect email (or set GARMIN_EMAIL).")
@click.option("--password", envvar="GARMIN_PASSWORD", default=None,
              help="Garmin Connect password (or set GARMIN_PASSWORD).")
@click.option("--registry-path", type=click.Path(path_type=Path), default=None,
              help="Override the registry file location (default: GARMIN_REGISTRY_PATH or /data/registry.json).")
def admin_provision(
    user_id: str,
    email: str | None,
    password: str | None,
    registry_path: Path | None,
) -> None:
    """Provision a new tenant: SSO into Garmin, write tokens, mint a bearer.

    Prompts for any unsupplied email/password (and reads MFA from the
    GARMIN_MFA env var or stdin TTY). Writes tokens to
    `<garmin_tokens_root>/<user_id>/`, computes sha256(bearer), atomically
    appends `{user_id, bearer_sha256}` to the registry file, and prints
    the bearer to stdout exactly ONCE — the bearer is never persisted in
    plaintext.
    """
    if not user_id or "/" in user_id or user_id.startswith("."):
        click.echo(f"invalid user-id {user_id!r}", err=True)
        sys.exit(2)
    settings = load_settings()
    reg_path = _resolve_registry_path(registry_path, settings)
    registry = Registry(reg_path)
    if user_id in registry.user_ids():
        click.echo(
            f"user_id {user_id!r} already exists in {reg_path}. "
            "Use `admin revoke` first if you want to re-provision.",
            err=True,
        )
        sys.exit(1)

    email = email or click.prompt("Garmin email", err=True)
    password = password or click.prompt(
        "Garmin password", hide_input=True, err=True
    )

    tokens_dir = settings.tokens_dir_for(user_id)
    try:
        client = login_interactive(
            email=email,
            password=password,
            tokens_path=tokens_dir,
            mfa_prompt=make_mfa_prompt(),
        )
        name = verify(client)
    except AuthError as e:
        click.echo(f"Login failed: {e}\nRemediation: {e.remediation}", err=True)
        sys.exit(1)

    bearer = _mint_bearer()
    digest = hashlib.sha256(bearer.encode("utf-8")).hexdigest()

    entries = _registry_entries(registry)
    entries.append({"user_id": user_id, "bearer_sha256": digest})
    entries.sort(key=lambda e: e["user_id"])
    try:
        write_registry_atomic(reg_path, entries)
    except OSError as e:
        click.echo(f"failed to write registry {reg_path}: {e}", err=True)
        sys.exit(1)

    click.echo(
        f"Provisioned user_id={user_id!r} (Garmin: {name}). "
        f"Tokens at {tokens_dir}. Registry updated at {reg_path}.",
        err=True,
    )
    click.echo(
        f"  registry entry: user_id={user_id} bearer_sha256={digest}",
        err=True,
    )
    click.echo(
        "  WARNING: the bearer below is shown ONCE. Copy it to the user's "
        "client config now — there is no way to recover it later.",
        err=True,
    )
    # The bearer is the one piece of output we deliberately send to stdout
    # so it can be piped (e.g. `... | pbcopy`). All other output goes to
    # stderr to keep this stream clean.
    click.echo(bearer)


@admin.command("revoke")
@click.option("--user-id", required=True, help="Tenant to revoke.")
@click.option("--purge-tokens", is_flag=True,
              help="Also remove the per-user tokens directory.")
@click.option("--registry-path", type=click.Path(path_type=Path), default=None)
def admin_revoke(user_id: str, purge_tokens: bool, registry_path: Path | None) -> None:
    """Remove a tenant's registry entry (and optionally their tokens dir)."""
    settings = load_settings()
    reg_path = _resolve_registry_path(registry_path, settings)
    registry = Registry(reg_path)
    entries = _registry_entries(registry)
    new_entries = [e for e in entries if e["user_id"] != user_id]
    if len(new_entries) == len(entries):
        click.echo(f"user_id {user_id!r} not found in {reg_path}", err=True)
        sys.exit(1)
    try:
        write_registry_atomic(reg_path, new_entries)
    except OSError as e:
        click.echo(f"failed to write registry {reg_path}: {e}", err=True)
        sys.exit(1)
    click.echo(f"Revoked user_id={user_id!r} from {reg_path}.", err=True)

    if purge_tokens:
        import shutil

        tokens_dir = settings.tokens_dir_for(user_id)
        if tokens_dir.exists():
            shutil.rmtree(tokens_dir)
            click.echo(f"  Purged tokens dir {tokens_dir}.", err=True)
        else:
            click.echo(f"  Tokens dir {tokens_dir} did not exist.", err=True)


@admin.command("list")
@click.option("--registry-path", type=click.Path(path_type=Path), default=None)
def admin_list(registry_path: Path | None) -> None:
    """List provisioned tenants. Never prints bearer values."""
    settings = load_settings()
    reg_path = _resolve_registry_path(registry_path, settings)
    registry = Registry(reg_path)
    try:
        entries = registry.entries()
    except RegistryError as e:
        click.echo(f"registry malformed: {e}", err=True)
        sys.exit(1)

    if not entries:
        click.echo(f"(registry empty: {reg_path})", err=True)
        return

    click.echo(f"{'user_id':<20} {'sha256[:8]':<10} {'tokens_dir_mtime':<20}", err=True)
    click.echo("-" * 52, err=True)
    for user_id, digest in entries:
        tokens_dir = settings.tokens_dir_for(user_id)
        if tokens_dir.exists():
            mtime = dt.datetime.fromtimestamp(
                tokens_dir.stat().st_mtime, tz=dt.UTC
            ).strftime("%Y-%m-%dT%H:%M:%S")
        else:
            mtime = "(missing)"
        click.echo(f"{user_id:<20} {digest[:8]:<10} {mtime:<20}")


if __name__ == "__main__":
    main()
