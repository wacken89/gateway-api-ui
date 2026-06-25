"""Gateway API UI — FastAPI backend.

Serves a read-only JSON API over the Kubernetes Gateway API and the static SPA.
Runs from a kubeconfig context locally, or from the pod ServiceAccount in-cluster.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import auth
import metrics
import model
from k8s import KIND_REGISTRY, POLICY_KINDS, KubeClient
from model import ROUTE_KINDS

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("gateway-api-ui")

STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "5"))

app = FastAPI(title="Gateway API UI", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.middleware("http")(metrics.metrics_middleware)


# Authorization guard: authentication is done by Envoy/Keycloak at the edge; here
# we only enforce the group allowlist on data endpoints. /api/me is always allowed
# (so the SPA can render an "access denied" screen), as are probes/metrics/static.
@app.middleware("http")
async def authz_guard(request: Request, call_next):
    path = request.url.path
    if auth.CONFIG.enabled and path.startswith("/api/") and path != "/api/me":
        ident = auth.identity(request.headers)
        if not ident["authenticated"]:
            return JSONResponse(status_code=401, content={"detail": "not authenticated"})
        if not ident["allowed"]:
            return JSONResponse(status_code=403,
                                content={"detail": "not authorized", "groups": ident["groups"]})
    return await call_next(request)

_kube: KubeClient | None = None
_kube_error: str | None = None


def kube() -> KubeClient:
    global _kube, _kube_error
    if _kube is None:
        try:
            _kube = KubeClient(cache_ttl=CACHE_TTL)
            _kube_error = None
        except Exception as exc:  # noqa: BLE001 — surface any config failure to the UI
            _kube_error = str(exc)
            log.exception("failed to initialize kube client")
            raise HTTPException(status_code=503, detail=f"kube config error: {exc}") from exc
    return _kube


# Expose live Gateway API state as Prometheus gauges (read at scrape time).
metrics.register(kube)


@app.get("/metrics")
def prometheus_metrics():
    data, content_type = metrics.render()
    return Response(content=data, media_type=content_type)


# ---------------------------------------------------------------------------
# meta / health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/me")
def me(request: Request):
    """Current user identity + whether they're allowed (drives the UI header/logout)."""
    return auth.identity(request.headers)


@app.get("/api/context")
def context():
    try:
        k = kube()
    except HTTPException as exc:
        return JSONResponse(status_code=503,
                            content={"connected": False, "error": exc.detail})
    return {
        "connected": True,
        "inCluster": k.in_cluster,
        "context": k.context_name,
        "serverVersion": k.server_version(),
        "readOnly": True,
    }


# ---------------------------------------------------------------------------
# namespaces
# ---------------------------------------------------------------------------

@app.get("/api/namespaces")
def namespaces():
    """Only namespaces that actually hold Gateway API objects.

    We deliberately do NOT list every cluster namespace: on Rancher-managed
    clusters that is a huge, noisy list. The picker should show what's relevant.
    """
    k = kube()
    derived: set[str] = set()

    def collect(items):
        for o in items:
            ns = o.get("metadata", {}).get("namespace")
            if ns:
                derived.add(ns)

    for plural in ("gateways", "httproutes", "grpcroutes", "tlsroutes",
                   "tcproutes", "udproutes"):
        collect(k.list_gateway(plural))
    for pk in POLICY_KINDS:
        collect(k.list_kind(pk))
    collect(k.list_ai("aigatewayroutes"))
    return {"namespaces": sorted(derived)}


# ---------------------------------------------------------------------------
# resource listings
# ---------------------------------------------------------------------------

def _ns(namespace: str | None) -> str | None:
    return None if not namespace or namespace == "all" else namespace


@app.get("/api/gatewayclasses")
def gatewayclasses():
    k = kube()
    return {"items": [model.gatewayclass_view(o) for o in k.list_gateway("gatewayclasses")]}


@app.get("/api/gateways")
def gateways(namespace: str | None = Query(default=None)):
    k = kube()
    items = [model.gateway_view(o) for o in k.list_gateway("gateways", _ns(namespace))]
    return {"items": items}


def _all_routes(k: KubeClient, namespace: str | None,
                rtype: str | None = None) -> list[dict]:
    out = []
    types = [rtype] if rtype and rtype in ROUTE_KINDS else list(ROUTE_KINDS)
    for t in types:
        for o in k.list_gateway(ROUTE_KINDS[t], namespace):
            out.append(model.route_view(o, t))
    return out


@app.get("/api/routes")
def routes(namespace: str | None = Query(default=None),
           type: str | None = Query(default=None)):
    k = kube()
    return {"items": _all_routes(k, _ns(namespace), type)}


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

@app.get("/api/overview")
def overview(namespace: str | None = Query(default=None)):
    k = kube()
    ns = _ns(namespace)
    classes = [model.gatewayclass_view(o) for o in k.list_gateway("gatewayclasses")]
    gws = [model.gateway_view(o) for o in k.list_gateway("gateways", ns)]
    rts = _all_routes(k, ns)

    def tally(items):
        t = {"ok": 0, "warn": 0, "error": 0, "unknown": 0}
        for i in items:
            t[i.get("health", "unknown")] += 1
        return t

    problems = []
    for item in gws + rts:
        if item.get("health") in ("warn", "error"):
            bad = [c for c in item.get("conditions", []) if c.get("status") == "False"]
            problems.append({
                "kind": item["kind"], "name": item["name"],
                "namespace": item.get("namespace"), "health": item["health"],
                "reasons": [f"{c.get('type')}: {c.get('reason')}" for c in bad][:3],
            })

    route_by_type: dict[str, int] = {}
    for r in rts:
        route_by_type[r["routeType"]] = route_by_type.get(r["routeType"], 0) + 1

    return {
        "counts": {
            "gatewayClasses": len(classes),
            "gateways": len(gws),
            "routes": len(rts),
            "listeners": sum(len(g["listeners"]) for g in gws),
            "attachedRoutes": sum(g["attachedRoutes"] for g in gws),
        },
        "routesByType": route_by_type,
        "health": {"gateways": tally(gws), "routes": tally(rts)},
        "problems": problems[:50],
    }


# ---------------------------------------------------------------------------
# topology graph
# ---------------------------------------------------------------------------

@app.get("/api/graph")
def graph(namespace: str | None = Query(default=None)):
    k = kube()
    ns = _ns(namespace)
    classes = [model.gatewayclass_view(o) for o in k.list_gateway("gatewayclasses")]
    gws = [model.gateway_view(o) for o in k.list_gateway("gateways", ns)]
    rts = _all_routes(k, ns)
    services = k.list_services(ns)
    endpoints = k.endpoint_counts(ns)
    return model.build_graph(classes, gws, rts, services, endpoints)


# ---------------------------------------------------------------------------
# detail (raw object + yaml)
# ---------------------------------------------------------------------------

@app.get("/api/object")
def object_detail(kind: str, name: str, namespace: str | None = Query(default=None)):
    k = kube()
    if kind not in KIND_REGISTRY:
        raise HTTPException(status_code=400, detail=f"unsupported kind {kind}")
    for o in k.list_kind(kind, _ns(namespace)):
        m = o.get("metadata", {})
        if m.get("name") == name and (namespace in (None, "all")
                                      or m.get("namespace") == namespace):
            # strip noisy managedFields before showing raw YAML
            o.get("metadata", {}).pop("managedFields", None)
            return {"raw": o, "yaml": yaml.safe_dump(o, sort_keys=False, allow_unicode=True)}
    raise HTTPException(status_code=404, detail=f"{kind}/{name} not found")


# ---------------------------------------------------------------------------
# policies (Envoy Gateway + AI Gateway, optional CRDs)
# ---------------------------------------------------------------------------

@app.get("/api/policies")
def policies(namespace: str | None = Query(default=None)):
    k = kube()
    ns = _ns(namespace)
    items, available = [], {}
    for kind in POLICY_KINDS:
        objs = k.list_kind(kind, ns)
        available[kind] = k.policy_available(kind)
        items.extend(model.policy_view(o, kind) for o in objs)
    return {"items": items, "available": available,
            "anyAvailable": any(available.values())}


# ---------------------------------------------------------------------------
# related resources (clickable graph of neighbours + their status)
# ---------------------------------------------------------------------------

@app.get("/api/related")
def related(kind: str, name: str, namespace: str | None = Query(default=None)):
    k = kube()
    ns = namespace if namespace not in (None, "all") else None
    return {"items": _related(k, kind, name, namespace if namespace != "all" else None)}


def _related(k: KubeClient, kind: str, name: str, namespace: str | None) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(rkind: str, nm: str | None, rns: str | None, health: str, relation: str):
        if not nm:
            return
        key = (rkind, rns, nm)
        if key in seen:
            return
        seen.add(key)
        out.append({"kind": rkind, "name": nm, "namespace": rns,
                    "health": health, "relation": relation})

    gateways = [model.gateway_view(o) for o in k.list_gateway("gateways")]
    gw_by = {(g["namespace"], g["name"]): g for g in gateways}
    routes = []
    for t in ROUTE_KINDS:
        routes.extend(model.route_view(o, t) for o in k.list_gateway(ROUTE_KINDS[t]))
    pols = []
    for pk in POLICY_KINDS:
        pols.extend(model.policy_view(o, pk) for o in k.list_kind(pk))

    def policies_targeting(target_kind: str, target_name: str, target_ns: str | None):
        for p in pols:
            for t in p["targetRefs"]:
                if t["kind"] == target_kind and t["name"] == target_name and \
                        (target_ns is None or t["namespace"] == target_ns):
                    add(p["kind"], p["name"], p["namespace"], p["health"], "policy")

    if kind == "Gateway":
        g = gw_by.get((namespace, name))
        if g and g.get("gatewayClassName"):
            cls = next((model.gatewayclass_view(o) for o in k.list_gateway("gatewayclasses")
                        if o.get("metadata", {}).get("name") == g["gatewayClassName"]), None)
            add("GatewayClass", g["gatewayClassName"], None,
                cls["health"] if cls else "unknown", "class")
        for r in routes:
            for p in r["parentRefs"]:
                if p["name"] == name and p["namespace"] == namespace:
                    add(r["kind"], r["name"], r["namespace"], r["health"], "route")
        policies_targeting("Gateway", name, namespace)

    elif kind in {"HTTPRoute", "GRPCRoute", "TLSRoute", "TCPRoute"}:
        r = next((x for x in routes if x["kind"] == kind and x["name"] == name
                  and x["namespace"] == namespace), None)
        if r:
            for p in r["parentRefs"]:
                g = gw_by.get((p["namespace"], p["name"]))
                add("Gateway", p["name"], p["namespace"],
                    g["health"] if g else "unknown", "parent")
            services = k.list_services()
            endpoints = k.endpoint_counts()
            for rule in r["rules"]:
                for b in rule["backendRefs"]:
                    bkey = f"{b['namespace']}/{b['name']}"
                    ready = endpoints.get(bkey)
                    if services.get(bkey) is None:
                        bh = "error"
                    elif ready == 0:
                        bh = "error"
                    elif ready is None:
                        bh = "warn"
                    else:
                        bh = "ok"
                    add(b.get("kind", "Service"), b["name"], b["namespace"], bh, "backend")
        policies_targeting(kind, name, namespace)

    elif kind == "GatewayClass":
        for g in gateways:
            if g.get("gatewayClassName") == name:
                add("Gateway", g["name"], g["namespace"], g["health"], "gateway")

    elif kind in POLICY_KINDS:
        p = next((x for x in pols if x["kind"] == kind and x["name"] == name
                  and x["namespace"] == namespace), None)
        if p:
            for t in p["targetRefs"]:
                tns = t["namespace"]
                health = "unknown"
                if t["kind"] == "Gateway":
                    g = gw_by.get((tns, t["name"]))
                    health = g["health"] if g else "unknown"
                else:
                    rr = next((x for x in routes if x["kind"] == t["kind"]
                               and x["name"] == t["name"] and x["namespace"] == tns), None)
                    health = rr["health"] if rr else "unknown"
                add(t["kind"], t["name"], tns, health,
                    "target" + (f" · {t['sectionName']}" if t.get("sectionName") else ""))

    return out


# ---------------------------------------------------------------------------
# AI gateway (optional CRD)
# ---------------------------------------------------------------------------

@app.get("/api/ai/routes")
def ai_routes(namespace: str | None = Query(default=None)):
    k = kube()
    items = [model.ai_route_view(o) for o in k.list_ai("aigatewayroutes", _ns(namespace))]
    return {"items": items, "available": bool(items) or
            k._resolved_version.get("aigatewayroutes") is not None}


@app.get("/api/providers")
def providers():
    """Distinct gateway controllers in the cluster, with class/gateway counts.

    There can be several (e.g. Cilium + Envoy), so this is a list — a single
    sidebar logo would be misleading.
    """
    k = kube()
    classes = [model.gatewayclass_view(o) for o in k.list_gateway("gatewayclasses")]
    gws = [model.gateway_view(o) for o in k.list_gateway("gateways")]
    class_provider = {c["name"]: model.provider_of(c.get("controller")) for c in classes}

    agg: dict[str, dict] = {}
    for c in classes:
        p = model.provider_of(c.get("controller"))
        e = agg.setdefault(p["key"], {**p, "classes": 0, "gateways": 0,
                                      "controllers": set(), "health": "ok"})
        e["classes"] += 1
        e["controllers"].add(c.get("controller"))
        if c["health"] in ("warn", "error"):
            e["health"] = "warn" if e["health"] == "ok" else e["health"]
    for g in gws:
        p = class_provider.get(g.get("gatewayClassName"))
        if not p:
            continue
        e = agg.get(p["key"])
        if e is None:
            e = agg.setdefault(p["key"], {**p, "classes": 0, "gateways": 0,
                                          "controllers": set(), "health": "ok"})
        e["gateways"] += 1
        if g["health"] == "error":
            e["health"] = "error"
        elif g["health"] == "warn" and e["health"] == "ok":
            e["health"] = "warn"

    out = []
    for e in agg.values():
        e["controllers"] = sorted(x for x in e["controllers"] if x)
        out.append(e)
    out.sort(key=lambda x: (-x["gateways"], x["name"]))
    return {"items": out}


@app.get("/api/index")
def index_all(namespace: str | None = Query(default=None)):
    """Flat, lightweight index of every object — powers the ⌘K command palette."""
    k = kube()
    ns = _ns(namespace)
    out = []
    for o in k.list_gateway("gatewayclasses"):
        v = model.gatewayclass_view(o)
        out.append({"kind": "GatewayClass", "name": v["name"], "namespace": None, "health": v["health"]})
    for o in k.list_gateway("gateways", ns):
        v = model.gateway_view(o)
        out.append({"kind": "Gateway", "name": v["name"], "namespace": v["namespace"], "health": v["health"]})
    for t in ROUTE_KINDS:
        for o in k.list_gateway(ROUTE_KINDS[t], ns):
            v = model.route_view(o, t)
            out.append({"kind": v["kind"], "name": v["name"], "namespace": v["namespace"], "health": v["health"]})
    for pk in POLICY_KINDS:
        for o in k.list_kind(pk, ns):
            v = model.policy_view(o, pk)
            out.append({"kind": pk, "name": v["name"], "namespace": v["namespace"], "health": v["health"]})
    return {"items": out}


@app.post("/api/refresh")
def refresh():
    kube().invalidate()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# static SPA (mounted last so /api/* wins)
# ---------------------------------------------------------------------------

@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
