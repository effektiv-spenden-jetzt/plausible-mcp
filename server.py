"""Plausible Analytics MCP server, gated behind Google sign-in.

Claude registers itself with this server over OAuth 2.1 dynamic client
registration, which Google does not support. So this server acts as its own
authorization server and delegates the human login upstream to Google.
"""

import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import diskcache
import httpx
from fastmcp import FastMCP
from fastmcp.apps import AppConfig, ResourcePermissions
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

# Metrics that are a count of things and so read as 0 when a bucket has no traffic.
# Everything else is a rate or an average, which is undefined over zero traffic rather
# than 0 — drawing it as 0% would invent a datapoint.
COUNT_METRICS = {"visitors", "visits", "pageviews", "events", "total_revenue"}

# Checked before the request goes out. Plausible rejects a wrong value with its own
# schema error ("Invalid dimension \"time:daily\""), which tells a model what it got
# wrong but never what would have been right, so it cannot correct itself.
METRICS = (
    "visitors", "visits", "pageviews", "views_per_visit", "bounce_rate",
    "visit_duration", "events", "scroll_depth", "percentage", "time_on_page",
    "conversion_rate", "group_conversion_rate", "average_revenue", "total_revenue",
)
INTERVALS = ("hour", "day", "week", "month")
SCALES = ("linear", "log", "indexed")
DATE_RANGES = (
    "day", "24h", "7d", "28d", "30d", "91d", "month", "6mo", "12mo", "year", "all",
)


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


def densify(payload: dict, metric: str, dimension: str) -> list[dict]:
    """Join a timeseries onto every bucket in the range, not just the ones with traffic.

    Plausible omits empty buckets ("If no data falls into a given time bucket, no values
    are returned"), so a quiet day silently disappears from the axis and the line joins
    straight across it. include.time_labels asks for the full bucket list; this joins the
    rows onto it. Done per site because "all" resolves to each site's own first datapoint,
    so there is no single axis to share.
    """
    values = {
        row.get(dimension): row.get(metric)
        for row in to_rows(payload, [metric], [dimension])
    }
    labels = payload.get("meta", {}).get("time_labels")
    if not labels:
        return [{"date": date, "value": value} for date, value in values.items()]
    fill = 0 if metric in COUNT_METRICS else None
    return [{"date": label, "value": values.get(label, fill)} for label in labels]


def one_of(value: str, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise ToolError(f"{name} must be one of {', '.join(allowed)}. Got {value!r}.")
    return value


def normalise_site(value: str) -> str:
    """Models reach for the URL they have seen rather than the bare domain Plausible
    wants, and Plausible answers a wrong site_id with a 401 that reads like the API key
    is broken."""
    site = value.strip().lower()
    for prefix in ("https://", "http://"):
        site = site.removeprefix(prefix)
    return site.rstrip("/")


def check_date_range(date_range: str | list[str]) -> str | list[str]:
    if isinstance(date_range, str):
        return one_of(date_range, DATE_RANGES, "date_range")
    if len(date_range) != 2:
        raise ToolError(
            "date_range as a list must be exactly two ISO dates, "
            f"for example [\"2026-01-01\", \"2026-01-31\"]. Got {date_range!r}."
        )
    return date_range


def previous_range(resolved: list[str]) -> list[str] | None:
    """The equal-length window immediately before `resolved`, as plain ISO dates.

    Deliberately date-only arithmetic. The resolved range carries the site's own UTC
    offset and ends at 23:59:59, so subtracting timedeltas from the datetimes drifts an
    hour across a DST boundary and trips over the off-by-one second. Returns None for a
    rolling window that does not start at midnight (24h and friends), where a
    day-granular previous period would be the wrong length.
    """
    if not resolved or len(resolved) < 2:
        return None
    start, end = (datetime.fromisoformat(value) for value in resolved[:2])
    if (start.hour, start.minute, start.second) != (0, 0, 0):
        return None
    span = end.date() - start.date()
    previous_end = start.date() - timedelta(days=1)
    return [(previous_end - span).isoformat(), previous_end.isoformat()]


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
    filters: list | None = None,
    compare: bool = False,
    scale: str = "linear",
) -> dict:
    """Render an interactive chart comparing one metric across one or more sites
    over time, overlaid on shared axes. The chart lets the viewer toggle sites,
    switch metric, date range, interval and y-axis scale, and hover for exact
    values, without involving the model again.

    site_ids: domains to overlay, as returned by list_sites. Omit to chart every
      site this server can query (capped at 8 — the categorical palette caps out
      there too).

    metric: exactly one of visitors, visits, pageviews, views_per_visit, bounce_rate,
      visit_duration, events, scroll_depth, percentage, time_on_page, conversion_rate,
      group_conversion_rate, average_revenue, total_revenue.

    date_range: one of "day", "24h", "7d", "28d", "30d", "91d", "month", "6mo",
      "12mo", "year", "all", or a pair of inclusive ISO dates such as
      ["2026-01-01", "2026-01-31"].

    interval: exactly one of hour, day, week, month — how the series is bucketed.
      Not "daily" or "weekly".

    filters: same shape as `query`'s filters, applied to every site.

    compare: leave false unless the person actually asked to compare periods, or asked
      whether something is up or down. It is not a free extra: it doubles the queries
      and draws a second dashed line per site, which crowds the chart when nobody asked
      for it. When true, fetches the equal-length period immediately before so each
      site's change can be shown. Ignored for date_range "all" and for rolling windows
      like "24h", which have no comparable preceding period.

    scale: the y-axis the chart opens on — "linear", "log", or "indexed" (every site
      rebased to 100 at its first value, which is how you compare sites whose traffic
      differs by orders of magnitude). Purely a display choice; the data is the same.
    """
    one_of(metric, METRICS, "metric")
    one_of(interval, INTERVALS, "interval")
    one_of(scale, SCALES, "scale")
    date_range = check_date_range(date_range)

    known = await site_domains()
    if site_ids:
        requested = [normalise_site(site) for site in site_ids]
        unknown = [site for site in requested if site not in known]
        if unknown:
            raise ToolError(
                f"No such site: {', '.join(unknown)}. "
                f"This server can query: {', '.join(known)}."
            )
    else:
        requested = known

    ids = requested[:MAX_CHART_SERIES]
    dims = [f"time:{interval}"]

    async def fetch(site_id: str, period: str | list[str]) -> dict:
        body = build_query(site_id, [metric], period, dims, filters, None, CHART_LIMIT, True)
        body["include"]["time_labels"] = True
        # The aggregate is a separate question, not the sum of the buckets: visitors is
        # a UNIQUE count, so adding up the days double-counts anyone who came back.
        aggregate = build_query(site_id, [metric], period, [], filters, None, 1, True)
        series, totals = await asyncio.gather(
            plausible("/api/v2/query", json=body),
            plausible("/api/v2/query", json=aggregate),
        )
        rows = to_rows(totals, [metric], [])
        return {
            "points": densify(series, metric, dims[0]),
            "total": rows[0].get(metric) if rows else None,
            "resolved": series.get("query", {}).get("date_range"),
        }

    async def gather_period(period: str | list[str], sites: list[str]) -> list[tuple[str, dict]]:
        """One site 404ing should cost that site, not the whole chart."""
        results = await asyncio.gather(
            *(fetch(site_id, period) for site_id in sites), return_exceptions=True
        )
        kept = [
            (site_id, result)
            for site_id, result in zip(sites, results)
            if not isinstance(result, BaseException)
        ]
        if sites and not kept:
            # Every site failed, so this is not one bad domain — say why rather than
            # rendering an empty chart and letting the real error disappear.
            raise results[0]
        return kept

    current = await gather_period(date_range, ids)
    resolved = next((row["resolved"] for _, row in current if row.get("resolved")), None)

    previous = None
    if compare and date_range != "all":
        window = previous_range(resolved) if resolved else None
        if window:
            try:
                before = await gather_period(window, [site_id for site_id, _ in current])
            except ToolError:
                # The comparison is a garnish. Losing it should not take the chart with it.
                before = None
            if before:
                previous = {
                    "date_range": window,
                    "series": {site_id: row["points"] for site_id, row in before},
                    "totals": {site_id: row["total"] for site_id, row in before},
                }

    return {
        "metric": metric,
        "interval": interval,
        # The range as asked for, verbatim. The resolved pair is always a list, and
        # feeding that back would turn every preset into a "custom range" in the UI.
        "date_range": date_range,
        "resolved_range": resolved,
        "scale": scale,
        "filters": filters,
        "compare": previous is not None,
        "series": {site_id: row["points"] for site_id, row in current},
        "totals": {site_id: row["total"] for site_id, row in current},
        # What was asked for, including sites that failed this time round. The UI refetches
        # with this, so one bad response does not shrink the chart for the rest of the session.
        "sites": ids,
        "total_sites": len(requested),
        # A site that answered for neither query is missing from the chart. Name it,
        # rather than quietly drawing fewer lines than were asked for.
        "unavailable": [site for site in ids if site not in dict(current)],
        "previous": previous,
    }


@mcp.resource(
    CHART_RESOURCE_URI,
    app=AppConfig(permissions=ResourcePermissions(clipboard_write={})),
)
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
