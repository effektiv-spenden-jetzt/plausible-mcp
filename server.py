"""Plausible Analytics MCP server, gated behind Google sign-in.

Claude registers itself with this server over OAuth 2.1 dynamic client
registration, which Google does not support. So this server acts as its own
authorization server and delegates the human login upstream to Google.
"""

import os

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse


def env_set(name: str) -> set[str]:
    return {v.strip().lower() for v in os.getenv(name, "").split(",") if v.strip()}


ALLOWED_EMAILS = env_set("ALLOWED_EMAILS")
ALLOWED_DOMAINS = env_set("ALLOWED_DOMAINS")
PLAUSIBLE_URL = os.getenv("PLAUSIBLE_URL", "https://plausible.io").rstrip("/")


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
    """Google says who you are. This says whether you may read our analytics."""

    async def on_call_tool(self, context, call_next):
        token = get_access_token()
        claims = getattr(token, "claims", None) or {}
        if not is_allowed(claims):
            raise ToolError(
                f"{claims.get('email') or 'This account'} is not authorised to use "
                "this server. Ask an admin to add you to ALLOWED_EMAILS."
            )
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


@mcp.tool
async def list_sites() -> list[str]:
    """List the Plausible sites this server can query, as domains to pass as site_id."""
    payload = await plausible("/api/v1/sites?limit=100", method="GET")
    return [site["domain"] for site in payload.get("sites", [])]


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
