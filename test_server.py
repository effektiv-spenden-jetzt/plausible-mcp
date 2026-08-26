"""Run with: python test_server.py"""

import asyncio
import os

os.environ.setdefault("ALLOWED_DOMAINS", "example.com")
os.environ.setdefault("ALLOWED_EMAILS", "guest@example.net")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("PLAUSIBLE_API_KEY", "test-key")

import server  # noqa: E402
from server import bump, build_query, is_allowed, is_verified, to_rows  # noqa: E402


def verified(email):
    return {"email": email, "email_verified": True}


def test_allowlist():
    assert is_allowed(verified("alice@example.com"))
    assert is_allowed(verified("ALICE@Example.com")), "case insensitive"
    assert is_allowed(verified("guest@example.net")), "named extra outside domain"

    assert not is_allowed(verified("stranger@gmail.com"))
    assert not is_allowed(verified("attacker@evil-example.com")), "prefix"
    assert not is_allowed(verified("attacker@example.com.evil.com")), "suffix"
    assert not is_allowed({"email": "alice@example.com"}), "unverified"
    assert not is_allowed({"email": "x@example.com", "email_verified": False})
    assert not is_allowed({}), "no claims at all"


def test_verified_flag_forms():
    assert is_verified(True) and is_verified("true") and is_verified("True")
    assert not is_verified(False), "boolean false"
    assert not is_verified("false"), "tokeninfo sends strings, and 'false' is truthy"
    assert not is_verified(None) and not is_verified("")

    assert is_allowed({"email": "a@example.com", "email_verified": "true"})
    assert not is_allowed({"email": "a@example.com", "email_verified": "false"})


def test_build_query():
    lean = build_query("a.com", ["visitors"], "7d", None, None, None, 100, True)
    assert "filters" not in lean and "order_by" not in lean, "omit empty optionals"
    assert lean["dimensions"] == []
    assert lean["pagination"] == {"limit": 100, "offset": 0}

    full = build_query(
        "a.com", ["visitors"], ["2026-01-01", "2026-01-31"], ["visit:channel"],
        [["is", "visit:channel", ["Direct"]]], [["visitors", "desc"]], 5, False,
    )
    assert full["filters"] == [["is", "visit:channel", ["Direct"]]]
    assert full["date_range"] == ["2026-01-01", "2026-01-31"]
    assert full["include"]["imports"] is False


def test_to_rows():
    payload = {"results": [{"dimensions": ["Direct"], "metrics": [669, 12.5]}]}
    assert to_rows(payload, ["visitors", "bounce_rate"], ["visit:channel"]) == [
        {"visit:channel": "Direct", "visitors": 669, "bounce_rate": 12.5}
    ]
    totals = {"results": [{"dimensions": [], "metrics": [669]}]}
    assert to_rows(totals, ["visitors"], []) == [{"visitors": 669}]
    assert to_rows({}, ["visitors"], []) == []


def test_bump():
    first = bump(None, "query", 100.0)
    assert first == {
        "calls": 1, "first_seen": 100.0, "last_seen": 100.0, "tools": {"query": 1}
    }

    second = bump(first, "query", 250.0)
    assert second["calls"] == 2
    assert second["first_seen"] == 100.0, "first_seen must not move"
    assert second["last_seen"] == 250.0
    assert second["tools"] == {"query": 2}

    third = bump(second, "list_sites", 300.0)
    assert third["tools"] == {"query": 2, "list_sites": 1}, "counts are per tool"


def test_bump_does_not_mutate_input_tools():
    entry = {"calls": 1, "first_seen": 1.0, "last_seen": 1.0, "tools": {"query": 1}}
    original = entry["tools"]
    bump(entry, "query", 2.0)
    assert original == {"query": 1}, "caller's nested dict stays untouched"


def test_chart_overlays_sites_and_caps_at_max_series():
    async def fake_plausible(path, method="POST", json=None):
        assert json["dimensions"] == ["time:day"]
        return {"results": [{"dimensions": ["2026-08-01"], "metrics": [10]}]}

    original = server.plausible
    server.plausible = fake_plausible
    try:
        result = asyncio.run(server.chart(
            site_ids=[f"site{i}.com" for i in range(10)], metric="visitors",
            date_range="7d", interval="day",
        ))
    finally:
        server.plausible = original

    assert result["metric"] == "visitors" and result["interval"] == "day"
    assert len(result["series"]) == server.MAX_CHART_SERIES, "caps to the palette's slot count"
    assert result["series"]["site0.com"] == [{"date": "2026-08-01", "value": 10}]


def test_chart_defaults_to_all_sites():
    async def fake_plausible(path, method="POST", json=None):
        if "sites" in path:
            return {"sites": [{"domain": "a.com"}, {"domain": "b.com"}]}
        return {"results": []}

    original = server.plausible
    server.plausible = fake_plausible
    try:
        result = asyncio.run(server.chart())
    finally:
        server.plausible = original

    assert set(result["series"]) == {"a.com", "b.com"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
