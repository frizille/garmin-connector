"""garmin-connector v1 -- private Garmin MCP connector for Claude (web, phone, desktop).

Runs on AWS Lambda behind API Gateway, via the Lambda Web Adapter.

  * Garmin data:  Garmin's official Personal API, through the read-only tools of
                  garmin-personal-mcp (pinned in requirements.txt).
  * Sign-in:      GitHub OAuth, allowlisted to one GitHub account.
  * Secrets:      one Secrets Manager secret (Garmin key, GitHub OAuth app, master key).
  * OAuth state:  DynamoDB, encrypted with a key derived from the master key.

Fails closed: if the secret is missing, unreadable, or still holds placeholder
values, only /healthz answers and every other path returns 503.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from typing import Any

import boto3
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

VERSION = "garmin-connector v1"

log = logging.getLogger("garmin-connector")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

PLACEHOLDER = "REPLACE_ME"

# Tools from garmin-personal-mcp that make no sense here: the download tool
# writes a file to the server's own disk (Lambda's /tmp, which you can't reach),
# and the menstrual-cycle tools don't apply to this account.
EXCLUDED_TOOLS = {
    "garmin_download_activity",
    "garmin_get_menstrual_cycle",
    "garmin_get_menstrual_day",
}

SECRET_TTL_SECONDS = int(os.environ.get("SECRET_TTL_SECONDS", "600"))


# --------------------------------------------------------------------------- #
# Local time zone: garmin-personal-mcp uses date.today() for "today", and
# Lambda runs in UTC. Without this, "today" flips to tomorrow at 8 pm Eastern.
# --------------------------------------------------------------------------- #
def _set_local_timezone() -> None:
    tz = os.environ.get("LOCAL_TIMEZONE")
    if tz:
        os.environ["TZ"] = tz
        time.tzset()


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
class Secrets:
    """Reads the app secret from Secrets Manager and re-reads it every
    SECRET_TTL_SECONDS, so a rotated Garmin key takes effect without a redeploy."""

    def __init__(self, secret_arn: str, client: Any | None = None) -> None:
        self._arn = secret_arn
        self._client = client or boto3.client("secretsmanager")
        self._lock = threading.Lock()
        self._value: dict[str, str] = {}
        self._fetched_at = 0.0

    def get(self) -> dict[str, str]:
        # Wall-clock time: Lambda may freeze idle containers, and time.monotonic()
        # can stop advancing while frozen.
        with self._lock:
            if not self._value or time.time() - self._fetched_at > SECRET_TTL_SECONDS:
                raw = self._client.get_secret_value(SecretId=self._arn)["SecretString"]
                self._value = json.loads(raw)
                self._fetched_at = time.time()
            return self._value

    def invalidate(self) -> None:
        """Force a re-read on next use (called when Garmin rejects the key)."""
        with self._lock:
            self._fetched_at = 0.0


def missing_fields(secret: dict[str, str]) -> list[str]:
    required = ["garmin_api_key", "github_client_id", "github_client_secret", "master_key"]
    return [f for f in required if not secret.get(f) or secret.get(f) == PLACEHOLDER]


def derive_key(master_key: str, purpose: str) -> bytes:
    """32 bytes for one purpose (JWT signing, storage encryption) from the master key."""
    return hmac.new(master_key.encode(), purpose.encode(), hashlib.sha256).digest()


# --------------------------------------------------------------------------- #
# GitHub allowlist
# --------------------------------------------------------------------------- #
def make_allowlist_verifier(allowed_login: str, allowed_id: str | None):
    from fastmcp.server.auth.providers.github import GitHubTokenVerifier

    allowed_login = allowed_login.strip().lower()
    allowed_id = (allowed_id or "").strip() or None

    class AllowlistGitHubVerifier(GitHubTokenVerifier):
        """Accepts a GitHub token only if it belongs to the one allowed account.
        Checked on every MCP request, not just at sign-in."""

        async def verify_token(self, token: str):  # type: ignore[override]
            result = await super().verify_token(token)
            if result is None:
                return None
            login = str(result.claims.get("login") or "").lower()
            user_id = str(result.claims.get("sub") or "")
            if login != allowed_login or (allowed_id and user_id != allowed_id):
                log.warning("rejected GitHub account login=%s id=%s", login, user_id)
                return None
            return result

    return AllowlistGitHubVerifier


# --------------------------------------------------------------------------- #
# Garmin tools
# --------------------------------------------------------------------------- #
def register_garmin_tools(mcp, secrets: Secrets) -> list[str]:
    """Copies garmin-personal-mcp's read-only tools onto our server, and points its
    key lookup at Secrets Manager rather than a file in the home directory."""
    from garmin_personal_mcp import garmin_client
    from garmin_personal_mcp import server as upstream

    def key_from_secrets_manager(api_key: str | None = None) -> str:
        return api_key or secrets.get()["garmin_api_key"]

    garmin_client.resolve_api_key = key_from_secrets_manager  # used by bearer_token()

    # If Garmin rejects the key, re-read the secret next time so a freshly pasted
    # key works right away instead of after the cache TTL.
    original_call = garmin_client.call
    if not getattr(original_call, "_invalidates_secret", False):

        def call_and_invalidate_on_bad_key(op, *, api_key=None):
            try:
                return original_call(op, api_key=api_key)
            except garmin_client.ApiKeyError:
                secrets.invalidate()
                raise

        call_and_invalidate_on_bad_key._invalidates_secret = True
        garmin_client.call = call_and_invalidate_on_bad_key

    registered = []
    for tool in upstream.mcp._tool_manager.list_tools():
        if tool.name in EXCLUDED_TOOLS:
            continue
        mcp.tool(
            tool.fn,
            name=tool.name,
            title=tool.title,
            description=tool.description,
            annotations=tool.annotations,
        )
        registered.append(tool.name)
    return registered


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def not_configured_app(reason: str) -> Starlette:
    async def healthz(_: Request):
        return JSONResponse({"status": "not_configured", "version": VERSION})

    # The reason goes to the logs only; callers learn nothing about the secret.
    log.warning("serving not-configured app: %s", reason)

    async def refuse(_: Request):
        return PlainTextResponse(f"{VERSION} is not configured yet.", status_code=503)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/{path:path}", refuse, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
        ]
    )


def build_app(secrets: Secrets | None = None, storage=None):
    _set_local_timezone()

    base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    secrets = secrets or Secrets(os.environ["SECRET_ARN"])

    try:
        secret = secrets.get()
    except Exception as exc:  # fail closed
        log.error("could not read secret: %s", type(exc).__name__)
        return not_configured_app("the secret could not be read")

    missing = missing_fields(secret)
    if missing:
        log.warning("secret still has placeholders: %s", ", ".join(missing))
        return not_configured_app("fill in " + ", ".join(missing) + " in the secret")

    from fastmcp import FastMCP
    from fastmcp.server.auth.providers.github import GitHubProvider
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    if storage is None:
        from key_value.aio.stores.dynamodb import DynamoDBStore

        storage = DynamoDBStore(table_name=os.environ["OAUTH_TABLE"], auto_create=False)

    master = secret["master_key"]
    fernet_key = base64.urlsafe_b64encode(derive_key(master, "oauth-storage-encryption"))
    # Undecryptable rows (e.g. after a master-key change) read as missing, so
    # Claude simply re-registers instead of every request failing with a 500.
    encrypted_storage = FernetEncryptionWrapper(
        key_value=storage, fernet=Fernet(fernet_key), raise_on_decryption_error=False
    )

    auth = GitHubProvider(
        client_id=secret["github_client_id"],
        client_secret=secret["github_client_secret"],
        base_url=base_url,
        # Read-only profile access; the provider's default ("user") is read/write.
        required_scopes=["read:user"],
        jwt_signing_key=derive_key(master, "fastmcp-jwt-signing"),
        client_storage=encrypted_storage,
        # claude.ai's callback; other clients (desktop, Claude Code) use localhost.
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
            "http://localhost:*",
            "http://127.0.0.1:*",
        ],
    )
    verifier_cls = make_allowlist_verifier(
        os.environ.get("ALLOWED_GITHUB_LOGIN", ""), os.environ.get("ALLOWED_GITHUB_ID")
    )
    # Swap in the allowlisted verifier. A 5-minute cache saves two GitHub API calls
    # per request; the login/ID check still runs on every hit.
    auth._token_validator = verifier_cls(
        required_scopes=auth.required_scopes, cache_ttl_seconds=300
    )

    mcp = FastMCP(
        "Garmin",
        instructions=(
            "Read-only access to the owner's Garmin Connect data through Garmin's "
            "official Personal API: activities with splits, daily summaries, sleep, "
            "HRV, heart rate, stress, Body Battery, weight, and HR zones. Dates are "
            "YYYY-MM-DD. Distances are meters, durations seconds, weights grams."
        ),
        auth=auth,
    )
    tools = register_garmin_tools(mcp, secrets)
    log.info("%s ready with %d tools", VERSION, len(tools))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request):
        return JSONResponse({"status": "ok", "version": VERSION})

    return mcp.http_app(path="/mcp", transport="http", stateless_http=True, json_response=True)


app = build_app() if os.environ.get("PUBLIC_BASE_URL") else None
