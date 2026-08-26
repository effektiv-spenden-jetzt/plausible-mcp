"""Plausible Analytics MCP server, gated behind Google sign-in.

Claude registers itself with this server over OAuth 2.1 dynamic client
registration, which Google does not support. So this server acts as its own
authorization server and delegates the human login upstream to Google.
"""

import asyncio
import base64
import os
from datetime import datetime, timezone
from pathlib import Path

import diskcache
import httpx
from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

ROOT = Path(__file__).parent
CHART_RESOURCE_URI = "ui://chart/mcp-app.html"
MAX_CHART_SERIES = 8  # matches the dataviz palette's validated categorical slot count
CHART_LIMIT = 10000  # Plausible's max page size; hourly over 12mo is 8760 buckets


def env_set(name: str) -> set[str]:
    return {v.strip().lower() for v in os.getenv(name, "").split(",") if v.strip()}


ALLOWED_EMAILS = env_set("ALLOWED_EMAILS")
ALLOWED_DOMAINS = env_set("ALLOWED_DOMAINS")
PLAUSIBLE_URL = os.getenv("PLAUSIBLE_URL", "https://plausible.io").rstrip("/")
USAGE_PATH = os.getenv("USAGE_PATH")

usage = diskcache.Cache(USAGE_PATH) if USAGE_PATH else None


def bump(entry: dict | None, tool: str, now: float) -> dict:
    entry = entry or {"calls": 0, "first_seen": now, "tools": {}}
    entry["calls"] += 1
    entry["last_seen"] = now
    entry["tools"] = dict(entry["tools"])
    entry["tools"][tool] = entry["tools"].get(tool, 0) + 1
    return entry


def record(email: str, tool: str, now: float, allowed: bool = True) -> None:
    if usage is None:
        return
    key = f"{'user' if allowed else 'denied'}::{email or 'unknown'}"
    with usage.transact():
        usage[key] = bump(usage.get(key), tool, now)


def as_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def is_verified(value: object) -> bool:
    """Google's tokeninfo reports email_verified as the string "true", while its
    userinfo endpoint reports a real boolean. Truthiness alone would accept "false"."""
    return value is True or str(value).strip().lower() == "true"


def is_allowed(claims: dict) -> bool:
    email = str(claims.get("email") or "").lower()
    if not email or not is_verified(claims.get("email_verified")):
        return False
    return email in ALLOWED_EMAILS or email.rpartition("@")[2] in ALLOWED_DOMAINS


def build_query(
    site_id: str,
    metrics: list[str],
    date_range: str | list[str],
    dimensions: list[str] | None,
    filters: list | None,
    order_by: list | None,
    limit: int,
    include_imports: bool,
) -> dict:
    body: dict = {
        "site_id": site_id,
        "metrics": metrics,
        "date_range": date_range,
        "dimensions": dimensions or [],
        "pagination": {"limit": limit, "offset": 0},
        "include": {"imports": include_imports, "total_rows": True},
    }
    if filters:
        body["filters"] = filters
    if order_by:
        body["order_by"] = order_by
    return body


def to_rows(payload: dict, metrics: list[str], dimensions: list[str]) -> list[dict]:
    """Plausible returns positional arrays; give the model named keys instead."""
    return [
        dict(zip(dimensions, row.get("dimensions", [])))
        | dict(zip(metrics, row.get("metrics", [])))
        for row in payload.get("results", [])
    ]


class Allowlist(Middleware):
    async def on_call_tool(self, context, call_next):
        token = get_access_token()
        claims = getattr(token, "claims", None) or {}
        email = str(claims.get("email") or "").lower()
        tool = getattr(context.message, "name", "unknown")
        now = context.timestamp.timestamp()

        if not is_allowed(claims):
            record(email, tool, now, allowed=False)
            raise ToolError(
                f"{claims.get('email') or 'This account'} is not authorised to use "
                "this server. Ask an admin to add you to ALLOWED_EMAILS."
            )

        record(email, tool, now)
        return await call_next(context)


def client_storage():
    """Persist OAuth client registrations so a redeploy does not sign everyone out."""
    directory = os.getenv("CLIENT_STORAGE_PATH")
    if not directory:
        return None
    from key_value.aio.stores.disk import DiskStore

    return DiskStore(directory=directory)


def build_server() -> FastMCP:
    if not (ALLOWED_EMAILS or ALLOWED_DOMAINS):
        raise SystemExit(
            "Refusing to start: set ALLOWED_EMAILS or ALLOWED_DOMAINS, otherwise "
            "any Google account can read your analytics."
        )

    server = FastMCP(
        name="Plausible",
        auth=GoogleProvider(
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            base_url=os.environ["BASE_URL"],
            required_scopes=["openid", "email", "profile"],
            jwt_signing_key=os.getenv("JWT_SIGNING_KEY"),
            client_storage=client_storage(),
        ),
    )
    server.add_middleware(Allowlist())
    return server


mcp = build_server()


def build_chart_html() -> str:
    """Inline the vendored MCP Apps client bundle into the chart UI resource as a
    base64 data blob, so the resource is self-contained HTML with no build step
    and no runtime network dependency."""
    bundle_b64 = base64.b64encode((ROOT / "vendor" / "ext-apps-app.js").read_bytes()).decode()
    html = (ROOT / "chart_app.html").read_text()
    return html.replace("__EXT_APPS_BUNDLE_B64__", bundle_b64)


CHART_HTML = build_chart_html()


async def plausible(path: str, method: str = "POST", json: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            f"{PLAUSIBLE_URL}{path}",
            json=json,
            headers={"Authorization": f"Bearer {os.environ['PLAUSIBLE_API_KEY']}"},
        )
    if response.is_error:
        raise ToolError(f"Plausible returned {response.status_code}: {response.text}")
    return response.json()


@mcp.tool
async def query(
    site_id: str,
    metrics: list[str],
    date_range: str | list[str] = "7d",
    dimensions: list[str] | None = None,
    filters: list | None = None,
    order_by: list | None = None,
    limit: int = 100,
    include_imports: bool = True,
) -> dict:
    """Query Plausible Analytics through the Stats API v2. One call answers
    timeseries, breakdown and goal-conversion questions: the shape of the answer
    depends on which dimensions you group by.

    site_id: the site's domain, for example "example.com". Required.
      Call list_sites if you do not know which sites exist.

    metrics: visitors, visits, pageviews, views_per_visit, bounce_rate,
      visit_duration, events, scroll_depth, percentage, time_on_page,
      conversion_rate, group_conversion_rate, average_revenue, total_revenue.
      conversion_rate needs an event:goal dimension or an event:goal filter.

    date_range: "day", "24h", "7d", "28d", "30d", "91d", "month", "6mo", "12mo",
      "year", "all", or a pair of inclusive ISO dates such as
      ["2026-01-01", "2026-01-31"]. Dates must not be in the future.

    dimensions: omit for a single total row. Group by time with time:hour,
      time:day, time:week or time:month. Group by event with event:goal,
      event:page, event:hostname or event:props:<name>. Group by visit with
      visit:source, visit:referrer, visit:channel, visit:utm_source,
      visit:utm_medium, visit:utm_campaign, visit:utm_content, visit:utm_term,
      visit:device, visit:browser, visit:browser_version, visit:os,
      visit:os_version, visit:entry_page, visit:exit_page, visit:country_name,
      visit:region_name or visit:city_name. Use the _name variants when showing
      geography to a person: visit:country, visit:region and visit:city return
      ISO and Geoname codes instead.

    filters: a list of [operator, dimension, [values]] entries, where operator is
      is, is_not, contains, contains_not, matches or matches_not. Combine them
      with ["and", [f1, f2]], ["or", [f1, f2]] or ["not", f]. For example
      [["is", "visit:channel", ["Organic Search"]]].

    order_by: a list such as [["visitors", "desc"]].

    To compare two periods, call this tool once per period.
    """
    dimensions = dimensions or []
    body = build_query(
        site_id, metrics, date_range, dimensions, filters, order_by, limit,
        include_imports,
    )
    payload = await plausible("/api/v2/query", json=body)
    return {
        "rows": to_rows(payload, metrics, dimensions),
        "total_rows": payload.get("meta", {}).get("total_rows"),
        "query": payload.get("query"),
    }


async def site_domains() -> list[str]:
    payload = await plausible("/api/v1/sites?limit=100", method="GET")
    return [site["domain"] for site in payload.get("sites", [])]


@mcp.tool
async def list_sites() -> list[str]:
    """List the Plausible sites this server can query, as domains to pass as site_id."""
    return await site_domains()


@mcp.tool(app=AppConfig(resource_uri=CHART_RESOURCE_URI))
async def chart(
    site_ids: list[str] | None = None,
    metric: str = "visitors",
    date_range: str | list[str] = "30d",
    interval: str = "day",
) -> dict:
    """Render an interactive chart comparing one metric across one or more sites
    over time, overlaid on shared axes. The chart lets the viewer toggle sites,
    switch metric, date range and interval, and hover for exact values, without
    involving the model again.

    site_ids: domains to overlay, as returned by list_sites. Omit to chart every
      site this server can query (capped at 8 — the categorical palette caps out
      there too).

    metric: any single metric accepted by the `query` tool, for example visitors,
      visits, pageviews, views_per_visit or bounce_rate.

    date_range: same values as `query`'s date_range.

    interval: hour, day, week or month — how the time series is bucketed.
    """
    ids = (site_ids or await site_domains())[:MAX_CHART_SERIES]
    dims = [f"time:{interval}"]

    async def points(site_id: str) -> list[dict]:
        body = build_query(site_id, [metric], date_range, dims, None, None, CHART_LIMIT, True)
        payload = await plausible("/api/v2/query", json=body)
        return [
            {"date": row.get(dims[0]), "value": row.get(metric)}
            for row in to_rows(payload, [metric], dims)
        ]

    series = dict(zip(ids, await asyncio.gather(*(points(site_id) for site_id in ids))))
    return {"metric": metric, "interval": interval, "date_range": date_range, "series": series}


@mcp.resource(CHART_RESOURCE_URI)
def chart_ui() -> str:
    return CHART_HTML


@mcp.tool
async def usage_stats() -> dict:
    """Report who has used this server, how often, and when. Also reports accounts
    that were refused. Anyone allowlisted can see this, including other people's rows.
    """
    if usage is None:
        return {"error": "Usage tracking is off because USAGE_PATH is not set."}

    users: list[dict] = []
    denied: list[dict] = []
    for key in list(usage):
        entry = usage.get(key)
        if not isinstance(entry, dict):
            continue
        kind, _, email = str(key).partition("::")
        row = {
            "email": email,
            "calls": entry["calls"],
            "first_seen": as_iso(entry["first_seen"]),
            "last_seen": as_iso(entry["last_seen"]),
            "tools": entry["tools"],
        }
        (users if kind == "user" else denied).append(row)

    users.sort(key=lambda row: row["calls"], reverse=True)
    denied.sort(key=lambda row: row["calls"], reverse=True)
    return {"distinct_users": len(users), "users": users, "refused": denied}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
