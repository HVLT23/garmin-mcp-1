"""Click CLI: `garmin-mcp auth login`, `auth status`, `serve`."""

from __future__ import annotations

import sys

import click

from garmin_mcp.auth import (
    AuthError,
    load_client,
    login_interactive,
    make_mfa_prompt,
    verify,
)
from garmin_mcp.config import load_settings


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


if __name__ == "__main__":
    main()
