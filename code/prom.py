"""Optional Prometheus integration.

When PROMETHEUS_URL is set, the dashboard enriches routes with live traffic
metrics (RPS, p95 latency, error rate) and a small sparkline. Queries are fully
templated via env so they can be adapted to any metrics pipeline; the defaults
target Envoy Gateway's per-cluster Envoy stats.

Cluster (backend) metrics carry an `envoy_cluster_name` like
`httproute/<namespace>/<name>/rule/<n>`. We run a handful of grouped queries
(not one-per-route), then map the series back to routes by parsing that label.

Stdlib-only HTTP (urllib) — no extra dependency. Degrades gracefully: any error
just yields empty metrics, never breaks a page.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse

import urllib3

try:
    import certifi
    _CA = certifi.where()
except Exception:  # pragma: no cover
    _CA = None


def _finite(value) -> float:
    """Coerce a Prometheus value to a JSON-safe finite float (NaN/Inf -> 0)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0

# Grouped PromQL — one query returns every route's value, keyed by cluster name.
# Override via env to match your metrics. `{range}` is substituted with the window.
# `{clusters}` is a regex substituted with the requested page's clusters (or `.+`
# for all), so Prometheus only computes the routes currently on screen — bounded
# load/payload at 1000+ routes. `{range}` is the rate window.
_Q_RPS = os.environ.get(
    "PROMETHEUS_QUERY_RPS",
    'sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_total'
    '{envoy_cluster_name=~"{clusters}"}[{range}]))',
)
_Q_P95 = os.environ.get(
    "PROMETHEUS_QUERY_P95",
    'histogram_quantile(0.95, sum by (envoy_cluster_name, le) '
    '(rate(envoy_cluster_upstream_rq_time_bucket{envoy_cluster_name=~"{clusters}"}[{range}])))',
)
_Q_ERR = os.environ.get(
    "PROMETHEUS_QUERY_ERROR_RATE",
    'sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_xx'
    '{envoy_cluster_name=~"{clusters}",envoy_response_code_class="5"}[{range}])) '
    '/ clamp_min(sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_total'
    '{envoy_cluster_name=~"{clusters}"}[{range}])), 1)',
)
# Route-level time series (single route drawer): aggregated across the route's
# rules (no per-cluster grouping) so each returns one line.
_TS_RPS = os.environ.get(
    "PROMETHEUS_TS_RPS",
    'sum(rate(envoy_cluster_upstream_rq_total{envoy_cluster_name=~"{clusters}"}[{range}]))',
)
_TS_P95 = os.environ.get(
    "PROMETHEUS_TS_P95",
    'histogram_quantile(0.95, sum by (le) '
    '(rate(envoy_cluster_upstream_rq_time_bucket{envoy_cluster_name=~"{clusters}"}[{range}])))',
)
_TS_ERR = os.environ.get(
    "PROMETHEUS_TS_ERROR_RATE",
    '(sum(rate(envoy_cluster_upstream_rq_xx{envoy_cluster_name=~"{clusters}",'
    'envoy_response_code_class="5"}[{range}])) or vector(0)) / clamp_min(sum(rate('
    'envoy_cluster_upstream_rq_total{envoy_cluster_name=~"{clusters}"}[{range}])), 1)',
)

# How to pull (namespace, name) out of the series label.
_CLUSTER_RE = re.compile(
    os.environ.get("PROMETHEUS_CLUSTER_REGEX",
                   r"^httproute/(?P<ns>[^/]+)/(?P<name>[^/]+)"))
_LABEL = os.environ.get("PROMETHEUS_GROUP_LABEL", "envoy_cluster_name")
_RANGE = os.environ.get("PROMETHEUS_RATE_WINDOW", "5m")
_TIMEOUT = float(os.environ.get("PROMETHEUS_TIMEOUT_SECONDS", "6"))

# Pooled HTTP client with keep-alive so repeated metric queries reuse connections
# (no per-request TCP/TLS setup, no ephemeral-port exhaustion under load). TLS is
# verified against the certifi CA bundle.
_POOL = urllib3.PoolManager(
    maxsize=10, retries=False, timeout=urllib3.Timeout(connect=3.0, read=_TIMEOUT),
    cert_reqs="CERT_REQUIRED" if _CA else "CERT_NONE", ca_certs=_CA,
)


def enabled() -> bool:
    return bool(os.environ.get("PROMETHEUS_URL"))


def _base() -> str:
    return os.environ.get("PROMETHEUS_URL", "").rstrip("/")


def _request(path: str, params: dict) -> dict | None:
    """GET a Prometheus API path; return the response body dict or None on error."""
    url = f"{_base()}{path}?{urllib.parse.urlencode(params)}"
    try:
        resp = _POOL.request("GET", url)
        data = json.loads(resp.data)
    except Exception:
        return None
    return data


def _get(path: str, params: dict) -> dict | None:
    data = _request(path, params)
    if not data or data.get("status") != "success":
        return None
    return data.get("data")


def _key_from_labels(metric: dict) -> str | None:
    m = _CLUSTER_RE.match(metric.get(_LABEL, ""))
    if not m:
        return None
    return f"{m.group('ns')}/{m.group('name')}"


_RE2_META = set(r"\.+*?()[]{}^$|")


def _re2_escape(s: str) -> str:
    """Escape only true RE2 metacharacters.

    NB: re.escape() escapes '-' to '\\-', which RE2 (used by Prometheus) rejects
    as 'unknown escape sequence'. Kubernetes ns/name are [a-z0-9-] + '/', none of
    which are RE2 metacharacters, so this typically returns the string unchanged.
    """
    return "".join("\\" + c if c in _RE2_META else c for c in s)


def _clusters_regex(keys: list[str] | None) -> str:
    """RE2 matching the Envoy cluster names of the given '<ns>/<name>' routes."""
    if not keys:
        return ".+"
    alt = "|".join(_re2_escape(k) for k in keys)
    return f".*/({alt})(/.*)?"


def _render(query: str, clusters: str = ".+") -> str:
    # PromQL uses {} for label selectors, so substitute tokens by hand rather than
    # str.format (which would choke on those braces).
    return query.replace("{clusters}", clusters).replace("{range}", _RANGE)


def _instant(query: str, clusters: str) -> dict[str, float]:
    """Return {"<ns>/<name>": value} for a grouped instant query."""
    out: dict[str, float] = {}
    data = _get("/api/v1/query", {"query": _render(query, clusters)})
    if not data:
        return out
    for series in data.get("result", []):
        key = _key_from_labels(series.get("metric", {}))
        if not key:
            continue
        try:
            val = _finite(series["value"][1])
        except (KeyError, IndexError):
            continue
        # multiple clusters (rules) per route -> sum for rps/err, max for p95
        out[key] = out.get(key, 0.0) + val
    return out


def route_metrics(keys: list[str] | None = None,
                  window_minutes: int = 30, step_seconds: int = 60) -> dict:
    """Per-route metrics + RPS sparkline, keyed by "<namespace>/<name>".

    When `keys` is given, queries are scoped to just those routes (the visible
    page) so Prometheus only computes what's on screen. Returns {} when
    Prometheus is unconfigured or unreachable.
    """
    if not enabled():
        return {}

    clusters = _clusters_regex(keys)
    rps = _instant(_Q_RPS, clusters)
    err = _instant(_Q_ERR, clusters)
    p95 = _instant(_Q_P95, clusters)

    spark: dict[str, list[float]] = {}
    import time
    end = int(time.time())
    start = end - window_minutes * 60
    rng = _get("/api/v1/query_range", {
        "query": _render(_Q_RPS, clusters),
        "start": start, "end": end, "step": step_seconds,
    })
    if rng:
        for series in rng.get("result", []):
            key = _key_from_labels(series.get("metric", {}))
            if not key:
                continue
            pts = [_finite(v) for _, v in series.get("values", [])]
            # sum across rules into a single per-route sparkline
            if key in spark and len(spark[key]) == len(pts):
                spark[key] = [a + b for a, b in zip(spark[key], pts)]
            else:
                spark[key] = pts

    out: dict[str, dict] = {}
    for key in set(rps) | set(err) | set(p95) | set(spark):
        out[key] = {
            "rps": round(rps.get(key, 0.0), 2),
            "errorRate": round(err.get(key, 0.0), 4),
            "p95Ms": round(p95.get(key, 0.0), 1),
            "spark": [round(x, 2) for x in spark.get(key, [])][-60:],
        }
    return out


def route_timeseries(key: str, window_minutes: int = 30, step_seconds: int = 30) -> dict:
    """Time series (rps / p95 / error-rate) for one route, for the drawer charts.

    Aggregated across the route's rules, so each metric is a single line of
    [unix_ts, value] points. Returns {"enabled": False} when unconfigured.
    """
    if not enabled():
        return {"enabled": False}
    import time
    clusters = _clusters_regex([key])
    end = int(time.time())
    start = end - window_minutes * 60

    def series(query: str) -> list[list[float]]:
        d = _get("/api/v1/query_range", {
            "query": _render(query, clusters),
            "start": start, "end": end, "step": step_seconds,
        })
        if not d or not d.get("result"):
            return []
        # single aggregated series expected
        return [[int(t), _finite(v)] for t, v in d["result"][0].get("values", [])]

    return {
        "enabled": True, "window": window_minutes, "step": step_seconds,
        "rps": series(_TS_RPS),
        "p95Ms": series(_TS_P95),
        "errorRate": series(_TS_ERR),
    }


def diagnose() -> dict:
    """Self-check to explain why metrics may be empty: reachability (auth/TLS),
    whether the request metric exists, and a few real cluster label values so you
    can tune PROMETHEUS_QUERY_* / PROMETHEUS_CLUSTER_REGEX to your pipeline.
    """
    if not enabled():
        return {"enabled": False}
    base = _base()
    out: dict = {"enabled": True, "url": base, "clusterLabel": _LABEL,
                 "rateWindow": _RANGE}

    def run(q: str):
        url = f"{base}/api/v1/query?{urllib.parse.urlencode({'query': q})}"
        try:
            resp = _POOL.request("GET", url)
            body = json.loads(resp.data)
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"
        if resp.status >= 400:
            return None, f"HTTP {resp.status}: {body.get('error', '')}".strip()
        if body.get("status") != "success":
            return None, body.get("error", "non-success response")
        return body.get("data", {}), None

    d, err = run("vector(1)")
    out["reachable"] = d is not None
    if err:
        out["error"] = err
        return out

    # Does the base request metric exist, and what do its cluster labels look like?
    base_metric = "envoy_cluster_upstream_rq_total"
    d2, _ = run(f"count({base_metric})")
    out["requestMetric"] = base_metric
    out["requestMetricExists"] = bool(d2 and d2.get("result"))
    d3, _ = run(f"group by ({_LABEL}) ({base_metric})")
    samples = []
    if d3:
        for s in d3.get("result", [])[:10]:
            v = s.get("metric", {}).get(_LABEL)
            if v:
                samples.append(v)
    out["sampleClusters"] = samples
    out["clusterRegex"] = _CLUSTER_RE.pattern
    return out
