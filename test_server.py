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


DEFAULT_RESOLVED = ["2026-08-01T00:00:00+00:00", "2026-08-03T23:59:59+00:00"]


def chart_api(*, buckets=(), labels=None, total=0, sites=(), resolved=None, seen=None):
    """Fake `plausible` for chart(), which asks two different questions per site: a
    bucketed timeseries and a separate aggregate. Answer them differently so a test can
    tell which number the chart actually used."""

    async def call(path, method="POST", json=None):
        if seen is not None and json is not None:
            seen.append(json)  # the site listing is a GET with no body
        if "sites" in path:
            return {"sites": [{"domain": domain} for domain in sites]}
        payload = {"query": {"date_range": resolved or DEFAULT_RESOLVED}}
        if json["dimensions"]:
            payload["results"] = [
                {"dimensions": [date], "metrics": [value]} for date, value in buckets
            ]
            if labels is not None:
                payload["meta"] = {"time_labels": labels}
        else:
            payload["results"] = [{"dimensions": [], "metrics": [total]}]
        return payload

    return call


def run_chart(fake, **kwargs):
    original = server.plausible
    server.plausible = fake
    try:
        return asyncio.run(server.chart(**kwargs))
    finally:
        server.plausible = original


def test_densify_fills_the_buckets_plausible_omits():
    payload = {
        "results": [{"dimensions": ["2026-08-02"], "metrics": [7]}],
        "meta": {"time_labels": ["2026-08-01", "2026-08-02", "2026-08-03"]},
    }
    assert server.densify(payload, "visitors", "time:day") == [
        {"date": "2026-08-01", "value": 0},
        {"date": "2026-08-02", "value": 7},
        {"date": "2026-08-03", "value": 0},
    ], "a count metric reads zero when nobody came"

    rates = server.densify(payload, "bounce_rate", "time:day")
    assert [point["value"] for point in rates] == [None, 7, None], (
        "a rate over zero traffic is undefined, not 0%"
    )

    unlabelled = {"results": [{"dimensions": ["2026-08-02"], "metrics": [7]}]}
    assert server.densify(unlabelled, "visitors", "time:day") == [
        {"date": "2026-08-02", "value": 7}
    ], "without time_labels, fall back to whatever came back"


def test_densify_joins_on_the_row_dimension_value():
    """If meta.time_labels and the row keys ever stop matching, every lookup misses and
    the chart renders blank with no error. Pin the join."""
    payload = {
        "results": [{"dimensions": ["2026-08-02 00:00:00"], "metrics": [5]}],
        "meta": {"time_labels": ["2026-08-01 00:00:00", "2026-08-02 00:00:00"]},
    }
    assert [p["value"] for p in server.densify(payload, "visitors", "time:hour")] == [0, 5]


def test_previous_range():
    assert server.previous_range(
        ["2024-09-04T00:00:00+00:00", "2024-09-10T23:59:59+00:00"]
    ) == ["2024-08-28", "2024-09-03"], "seven days, immediately before"

    assert server.previous_range(
        ["2024-03-01T00:00:00+00:00", "2024-03-31T23:59:59+00:00"]
    ) == ["2024-01-30", "2024-02-29"], "crosses a month boundary into a leap day"

    assert server.previous_range(
        ["2024-09-10T00:00:00+00:00", "2024-09-10T23:59:59+00:00"]
    ) == ["2024-09-09", "2024-09-09"], "a single day compares to the day before"

    assert server.previous_range(
        ["2024-09-09T14:00:00+00:00", "2024-09-10T13:59:59+00:00"]
    ) is None, "a rolling window has no whole-day previous period"

    assert server.previous_range([]) is None


def test_chart_asks_for_time_labels_without_leaking_into_query():
    seen = []
    run_chart(chart_api(sites=["a.com"], seen=seen), site_ids=["a.com"])
    timeseries = [body for body in seen if body["dimensions"]]
    aggregate = [body for body in seen if not body["dimensions"]]
    assert timeseries and timeseries[0]["include"]["time_labels"] is True
    assert aggregate and "time_labels" not in aggregate[0]["include"]

    plain = build_query("a.com", ["visitors"], "7d", ["time:day"], None, None, 100, True)
    assert "time_labels" not in plain["include"], "the query tool is unchanged"


def test_chart_totals_come_from_the_aggregate_not_the_sum():
    """visitors is a UNIQUE count, so summing the daily buckets double-counts anyone who
    came back. The fixture's buckets sum to 30; the real answer is 18."""
    result = run_chart(
        chart_api(
            buckets=[("2026-08-01", 10), ("2026-08-02", 10), ("2026-08-03", 10)],
            labels=["2026-08-01", "2026-08-02", "2026-08-03"],
            total=18,
            sites=["a.com"],
        ),
        site_ids=["a.com"],
    )
    assert result["totals"]["a.com"] == 18


def test_chart_echoes_filters_and_reports_sites_before_truncation():
    filters = [["is", "visit:channel", ["Organic Search"]]]
    seen = []
    result = run_chart(
        chart_api(sites=[f"site{i}.com" for i in range(23)], seen=seen),
        site_ids=[f"site{i}.com" for i in range(23)],
        filters=filters,
    )
    assert len(result["series"]) == server.MAX_CHART_SERIES, "caps to the palette's slots"
    assert result["total_sites"] == 23, "counted before the cap, so the UI can say so"
    assert result["filters"] == filters, "echoed back or refetch silently drops them"
    assert all(body["filters"] == filters for body in seen), "applied to every site"


def test_chart_refuses_to_compare_an_open_ended_range():
    seen = []
    result = run_chart(chart_api(sites=["a.com"], seen=seen), site_ids=["a.com"], date_range="all", compare=True)
    assert result["compare"] is False, "nothing precedes all time"
    assert result["previous"] is None

    baseline = []
    run_chart(chart_api(sites=["a.com"], seen=baseline), site_ids=["a.com"], date_range="all")
    assert len(seen) == len(baseline), "and it costs no extra requests"


def test_chart_compares_against_the_preceding_window():
    result = run_chart(chart_api(sites=["a.com"], total=5), site_ids=["a.com"], date_range="7d", compare=True)
    assert result["compare"] is True
    assert result["previous"]["date_range"] == ["2026-07-29", "2026-07-31"]
    assert result["previous"]["totals"]["a.com"] == 5


def test_chart_survives_a_failed_comparison():
    """The comparison is a garnish: if only the previous period fails, the chart should
    still render the current one."""
    good = chart_api(sites=["a.com"], total=7)

    async def previous_period_broken(path, method="POST", json=None):
        if json and json.get("date_range") == ["2026-07-29", "2026-07-31"]:
            raise server.ToolError("Plausible returned 500")
        return await good(path, method, json)

    result = run_chart(previous_period_broken, site_ids=["a.com"], date_range="7d", compare=True)
    assert result["compare"] is False and result["previous"] is None
    assert result["totals"]["a.com"] == 7


def test_chart_keeps_the_requested_range_not_the_resolved_one():
    """The resolved range is always a list; echoing it back would turn every preset into
    a custom range in the picker."""
    result = run_chart(chart_api(sites=["a.com"]), site_ids=["a.com"], date_range="30d")
    assert result["date_range"] == "30d"
    assert result["resolved_range"] == DEFAULT_RESOLVED


def test_chart_drops_a_failing_site_but_surfaces_a_total_failure():
    async def one_bad_site(path, method="POST", json=None):
        if "sites" in path:
            return {"sites": [{"domain": "good.com"}, {"domain": "bad.com"}]}
        if json["site_id"] == "bad.com":
            raise server.ToolError("Plausible returned 404")
        return {"results": [], "query": {"date_range": DEFAULT_RESOLVED}}

    result = run_chart(one_bad_site, site_ids=["good.com", "bad.com"])
    assert set(result["series"]) == {"good.com"}, "one bad domain costs only that domain"
    assert result["sites"] == ["good.com", "bad.com"], (
        "the UI refetches with this, so a one-off failure must not shrink the chart"
    )

    async def everything_broken(path, method="POST", json=None):
        if "sites" in path:
            return {"sites": [{"domain": "a.com"}]}
        raise server.ToolError("Plausible returned 500")

    try:
        run_chart(everything_broken, site_ids=["a.com"])
    except server.ToolError:
        pass
    else:
        raise AssertionError("a total failure must surface, not render an empty chart")


def raises(fn, *, contains):
    try:
        fn()
    except server.ToolError as e:
        assert contains in str(e), f"expected {contains!r} in {str(e)!r}"
        return str(e)
    raise AssertionError(f"expected a ToolError mentioning {contains!r}")


def test_chart_rejects_bad_inputs_by_naming_the_good_ones():
    """Plausible answers "daily" with `Invalid dimension "time:daily"`, which tells the
    model what it got wrong but never what would have been right. Say the valid values
    so it can correct itself instead of failing the whole call."""
    api = chart_api(sites=["a.com"])
    message = raises(lambda: run_chart(api, interval="daily"), contains="interval must be one of")
    assert "day" in message and "'daily'" in message

    raises(lambda: run_chart(api, metric="sessions"), contains="metric must be one of")
    raises(lambda: run_chart(api, scale="logarithmic"), contains="scale must be one of")
    raises(lambda: run_chart(api, date_range="last_30_days"), contains="date_range must be one of")
    raises(lambda: run_chart(api, date_range=["2026-01-01"]), contains="exactly two ISO dates")


def test_chart_names_the_sites_it_has_when_given_one_it_does_not():
    """A wrong site_id gets a 401 from Plausible that reads like the API key is broken."""
    message = raises(
        lambda: run_chart(chart_api(sites=["a.com", "b.com"]), site_ids=["nope.com"]),
        contains="No such site",
    )
    assert "a.com" in message and "b.com" in message


def test_chart_accepts_a_site_given_as_a_url():
    result = run_chart(
        chart_api(sites=["a.com"]), site_ids=["https://A.com/"]
    )
    assert list(result["series"]) == ["a.com"], "models pass the URL they have seen"


def test_chart_reports_a_site_that_returned_nothing():
    async def one_bad_site(path, method="POST", json=None):
        if "sites" in path:
            return {"sites": [{"domain": "good.com"}, {"domain": "bad.com"}]}
        if json["site_id"] == "bad.com":
            raise server.ToolError("Plausible returned 401")
        return {"results": [], "query": {"date_range": DEFAULT_RESOLVED}}

    result = run_chart(one_bad_site)
    assert list(result["series"]) == ["good.com"]
    assert result["unavailable"] == ["bad.com"], "a partial chart must say it is partial"
    assert result["sites"] == ["good.com", "bad.com"], "so a retry can bring it back"


def test_chart_defaults_to_all_sites():
    result = run_chart(chart_api(sites=["a.com", "b.com"]))
    assert set(result["series"]) == {"a.com", "b.com"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
