"""Local tests for garmin-connector v1. No AWS, GitHub, or Garmin account needed.

Run:  pip install -r src/requirements.txt pytest && pytest -q tests
"""

import os
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import app as appmod  # noqa: E402

GOOD = {
    "garmin_api_key": "test-garmin-key",
    "github_client_id": "Iv1.test",
    "github_client_secret": "test-secret",
    "master_key": "m" * 48,
}
BASE = "https://example.execute-api.us-east-1.amazonaws.com"


class FakeSecrets(appmod.Secrets):
    def __init__(self, value=None, error=None):
        self._v, self._e = value, error

    def get(self):
        if self._e:
            raise self._e
        return self._v


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("ALLOWED_GITHUB_LOGIN", "example-user")
    monkeypatch.setenv("LOCAL_TIMEZONE", "America/New_York")


def memory_store():
    from key_value.aio.stores.memory import MemoryStore

    return MemoryStore()


def test_fails_closed_with_placeholders():
    secret = dict(GOOD, garmin_api_key="REPLACE_ME")
    with TestClient(appmod.build_app(FakeSecrets(secret))) as c:
        assert c.get("/healthz").json()["status"] == "not_configured"
        r = c.post("/mcp", json={})
        assert r.status_code == 503 and "garmin_api_key" not in r.text  # no detail leaked


def test_fails_closed_when_secret_unreadable():
    with TestClient(appmod.build_app(FakeSecrets(error=RuntimeError("boom")))) as c:
        assert c.post("/mcp", json={}).status_code == 503


def test_configured_app_requires_auth_and_publishes_oauth_metadata():
    with TestClient(appmod.build_app(FakeSecrets(GOOD), storage=memory_store())) as c:
        assert c.get("/healthz").json() == {"status": "ok", "version": appmod.VERSION}

        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")

        meta = c.get("/.well-known/oauth-authorization-server").json()
        assert meta["issuer"].rstrip("/") == BASE
        assert meta["registration_endpoint"].startswith(BASE)
        assert "S256" in meta["code_challenge_methods_supported"]


def test_dynamic_client_registration_accepts_claude_and_rejects_strangers():
    with TestClient(appmod.build_app(FakeSecrets(GOOD), storage=memory_store())) as c:
        ok = c.post(
            "/register",
            json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "client_name": "Claude",
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert ok.status_code == 201, ok.text
        bad = c.post(
            "/register",
            json={
                "redirect_uris": ["https://evil.example.com/cb"],
                "client_name": "x",
                "token_endpoint_auth_method": "none",
            },
        )
        assert bad.status_code == 400


def test_tools_registered_minus_excluded():
    from fastmcp import FastMCP
    import asyncio

    mcp = FastMCP("t")
    names = appmod.register_garmin_tools(mcp, FakeSecrets(GOOD))
    assert "garmin_get_day" in names and "garmin_search_activities" in names
    assert not set(names) & appmod.EXCLUDED_TOOLS
    listed = {t.name for t in asyncio.run(mcp.list_tools())}
    assert listed == set(names)


def test_garmin_key_comes_from_secrets_manager():
    from fastmcp import FastMCP
    from garmin_personal_mcp import garmin_client

    appmod.register_garmin_tools(FastMCP("t"), FakeSecrets(GOOD))
    assert garmin_client.resolve_api_key(None) == "test-garmin-key"


@pytest.mark.parametrize(
    "login,uid,allowed_id,ok",
    [
        ("example-user", "123", None, True),
        ("Example-User", "123", None, True),
        ("someoneelse", "123", None, False),
        ("example-user", "123", "123", True),
        ("example-user", "999", "123", False),  # renamed-login takeover blocked by ID pin
    ],
)
def test_allowlist(monkeypatch, login, uid, allowed_id, ok):
    import asyncio
    from fastmcp.server.auth import AccessToken
    from fastmcp.server.auth.providers.github import GitHubTokenVerifier

    async def fake_super(self, token):
        return AccessToken(token=token, client_id=uid, scopes=["user"],
                           claims={"login": login, "sub": uid})

    monkeypatch.setattr(GitHubTokenVerifier, "verify_token", fake_super)
    v = appmod.make_allowlist_verifier("example-user", allowed_id)()
    assert (asyncio.run(v.verify_token("t")) is not None) is ok


def test_derived_keys_differ_by_purpose():
    a = appmod.derive_key("x" * 48, "fastmcp-jwt-signing")
    b = appmod.derive_key("x" * 48, "oauth-storage-encryption")
    assert a != b and len(a) == len(b) == 32


def test_local_timezone_applied():
    import time

    appmod._set_local_timezone()
    assert time.tzname[0] in {"EST", "EDT"}


def test_tool_call_end_to_end_with_mocked_garmin(monkeypatch):
    """A real MCP client calls a tool; only the Garmin HTTP call is faked."""
    import asyncio
    from fastmcp import Client, FastMCP
    from garmin_personal_mcp import garmin_client

    seen = {}

    def fake_call(op, *, api_key=None):
        seen["key"] = garmin_client.resolve_api_key(api_key)

        class FakeClient:
            def __getattr__(self, name):
                return lambda *a, **k: [{"activityName": "Long run", "distanceInMeters": 24140}]

        return op(FakeClient())

    monkeypatch.setattr(garmin_client, "call", fake_call)
    mcp = FastMCP("t")
    appmod.register_garmin_tools(mcp, FakeSecrets(GOOD))

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("garmin_search_activities", {"start": "2026-10-01", "end": "2026-10-24"})

    result = asyncio.run(go())
    assert seen["key"] == "test-garmin-key"
    assert "Long run" in str(result.content)
