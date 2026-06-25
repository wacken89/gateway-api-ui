"""Prometheus metrics for Gateway API UI.

Two kinds of metrics:

* HTTP metrics for the UI's own API (request count + latency), via middleware.
* Domain metrics — the actual Gateway API state (gateways / routes / policies by
  health) — emitted by a custom collector that reads the *cached* kube client at
  scrape time. This lets Prometheus alert on "a route went unhealthy" without the
  UI being open.

The collector degrades gracefully: if the apiserver is unreachable it just emits
`gatewayapi_scrape_ok 0` and no domain series, never raising during a scrape.
"""

from __future__ import annotations

import time
from typing import Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, REGISTRY

import model
from k8s import POLICY_KINDS

HTTP_REQUESTS = Counter(
    "gatewayapi_ui_http_requests_total",
    "HTTP requests handled by the UI backend.",
    ["method", "path", "status"],
)
HTTP_LATENCY = Histogram(
    "gatewayapi_ui_http_request_duration_seconds",
    "HTTP request latency for the UI backend.",
    ["method", "path"],
)

_HEALTHS = ("ok", "warn", "error", "unknown")


def normalize_path(path: str) -> str:
    """Collapse static-asset paths to keep label cardinality bounded."""
    if path == "/healthz" or path == "/metrics":
        return path
    if path.startswith("/api/"):
        return path
    return "/static"


async def metrics_middleware(request, call_next):
    method = request.method
    path = normalize_path(request.url.path)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        HTTP_LATENCY.labels(method, path).observe(time.perf_counter() - start)
        HTTP_REQUESTS.labels(method, path, str(status)).inc()


class GatewayApiCollector:
    """Custom collector that turns live Gateway API state into gauges."""

    def __init__(self, kube_provider: Callable):
        # kube_provider() returns the (cached) KubeClient, or raises if unconfigured.
        self._kube = kube_provider

    def collect(self):
        ok = GaugeMetricFamily(
            "gatewayapi_scrape_ok", "1 if the apiserver was reachable on this scrape.")
        try:
            k = self._kube()
        except Exception:
            ok.add_metric([], 0.0)
            yield ok
            return
        ok.add_metric([], 1.0)
        yield ok

        try:
            classes = [model.gatewayclass_view(o) for o in k.list_gateway("gatewayclasses")]
            gateways = [model.gateway_view(o) for o in k.list_gateway("gateways")]
            routes = []
            for t in model.ROUTE_KINDS:
                routes.extend(model.route_view(o, t) for o in k.list_gateway(model.ROUTE_KINDS[t]))
            policies = []
            for pk in POLICY_KINDS:
                policies.extend(model.policy_view(o, pk) for o in k.list_kind(pk))
        except Exception:
            return  # transient apiserver issue — skip domain series this scrape

        g_classes = GaugeMetricFamily(
            "gatewayapi_gatewayclasses", "GatewayClasses by health.", labels=["health"])
        g_gw = GaugeMetricFamily(
            "gatewayapi_gateways", "Gateways by health.", labels=["health"])
        g_routes = GaugeMetricFamily(
            "gatewayapi_routes", "Routes by type and health.", labels=["type", "health"])
        g_pol = GaugeMetricFamily(
            "gatewayapi_policies", "Policies by kind and health.", labels=["kind", "health"])
        g_listeners = GaugeMetricFamily(
            "gatewayapi_listeners_total", "Total Gateway listeners.")
        g_attached = GaugeMetricFamily(
            "gatewayapi_attached_routes_total", "Total routes attached across all listeners.")

        for h in _HEALTHS:
            g_classes.add_metric([h], sum(1 for c in classes if c["health"] == h))
            g_gw.add_metric([h], sum(1 for g in gateways if g["health"] == h))

        for t in model.ROUTE_KINDS:
            for h in _HEALTHS:
                g_routes.add_metric([t, h], sum(
                    1 for r in routes if r["routeType"] == t and r["health"] == h))

        for pk in POLICY_KINDS:
            for h in _HEALTHS:
                g_pol.add_metric([pk, h], sum(
                    1 for p in policies if p["kind"] == pk and p["health"] == h))

        g_listeners.add_metric([], sum(len(g["listeners"]) for g in gateways))
        g_attached.add_metric([], sum(g["attachedRoutes"] for g in gateways))

        yield from (g_classes, g_gw, g_routes, g_pol, g_listeners, g_attached)


_registered = False


def register(kube_provider: Callable) -> None:
    global _registered
    if not _registered:
        REGISTRY.register(GatewayApiCollector(kube_provider))
        _registered = True


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
