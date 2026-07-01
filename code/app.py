"""Gateway API UI — FastAPI backend.

Serves a read-only JSON API over the Kubernetes Gateway API and the static SPA.
Runs from a kubeconfig context locally, or from the pod ServiceAccount in-cluster.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import auth
import metrics
import model
import prom
from k8s import KIND_REGISTRY, POLICY_KINDS, KubeClient
from model import ROUTE_KINDS

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("gateway-api-ui")

STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "5"))
# Write mode is opt-in and contradicts the read-only default — keep it off unless
# explicitly enabled (and backed by a ClusterRole that grants the write verbs).
WRITE_ENABLED = os.environ.get("WRITE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
# Optional explicit allowlist of Origins for write requests; empty => same-origin only.
TRUSTED_ORIGINS = {o.strip() for o in os.environ.get("TRUSTED_ORIGINS", "").split(",") if o.strip()}

# Kubernetes ns/name are lowercase DNS names; a metrics key is "<ns>/<name>" and a
# keys list is comma-separated. Reject anything else so user input can never break
# out of the Prometheus label selector (PromQL injection).
_K8S_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")
_KEYS_RE = re.compile(r"^[a-z0-9][a-z0-9.\-/,]*$")


def _valid_name(value: str | None) -> str | None:
    if value in (None, "", "all"):
        return value
    if not _K8S_NAME_RE.match(value):
        raise HTTPException(status_code=400, detail="invalid name/namespace")
    return value


def _valid_keys(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) > 8000 or not _KEYS_RE.match(value):
        raise HTTPException(status_code=400, detail="invalid keys parameter")
    return value


def _check_csrf(request: Request) -> None:
    """Reject cross-site mutating requests (belt-and-suspenders on top of the
    JSON-only body + no-CORS design). Compares Origin/Referer host to the request
    Host; behind Envoy the Host is preserved so same-origin requests pass."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return  # non-browser client (curl/kubectl-style) — nothing to forge
    host = urlsplit(src).netloc
    if host in TRUSTED_ORIGINS:
        return
    if host and host != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="cross-origin write blocked")

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
        "readOnly": not WRITE_ENABLED,
        "writeEnabled": WRITE_ENABLED,
        "metricsEnabled": prom.enabled(),
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


@app.get("/api/namespaces/all")
def namespaces_all():
    """Every namespace (for the create form's namespace picker)."""
    return {"namespaces": kube().list_namespaces()}


@app.get("/api/services")
def services(namespace: str = Query(...)):
    """Services in a namespace, with their ports — powers the backend picker."""
    k = kube()
    out = []
    for key, svc in k.list_services(namespace).items():
        ns, name = key.split("/", 1)
        if ns != namespace:
            continue
        ports = [p["port"] for p in (svc.get("ports") or []) if p.get("port")]
        out.append({"name": name, "ports": ports})
    return {"items": sorted(out, key=lambda x: x["name"])}


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


def _route_matches(r: dict, ql: str) -> bool:
    hay = " ".join([
        r.get("name") or "", r.get("namespace") or "",
        " ".join(r.get("hostnames") or []),
        " ".join(p.get("name") or "" for p in r.get("parentRefs") or []),
        " ".join(b.get("name") or "" for b in r.get("backends") or []),
    ]).lower()
    return ql in hay


@app.get("/api/routes")
def routes(namespace: str | None = Query(default=None),
           type: str | None = Query(default=None),
           q: str | None = Query(default=None),
           limit: int = Query(default=100, ge=1, le=1000),
           offset: int = Query(default=0, ge=0)):
    """Paginated, slim, server-side-searchable route list.

    The full LIST is still served from the cached kube client, but only a small
    page of slim summaries crosses the wire — keeps the browser/DOM bounded at
    1000+ routes. Full per-rule detail is loaded lazily via /api/object.
    """
    k = kube()
    ns = _ns(namespace)
    types = [type] if type and type in ROUTE_KINDS else list(ROUTE_KINDS)
    items = [model.route_summary(o, t)
             for t in types for o in k.list_gateway(ROUTE_KINDS[t], ns)]
    if q and q.strip():
        ql = q.strip().lower()
        items = [r for r in items if _route_matches(r, ql)]
    items.sort(key=lambda r: ((r.get("namespace") or ""), r.get("name") or ""))
    total = len(items)
    page = items[offset:offset + limit]
    return {"items": page, "total": total, "offset": offset,
            "limit": limit, "hasMore": offset + limit < total}


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
# route traffic metrics (optional, Prometheus)
# ---------------------------------------------------------------------------

@app.get("/api/metrics/check")
def metrics_check():
    """Diagnostics for the Prometheus integration (reachability + sample labels)."""
    return prom.diagnose()


@app.get("/api/metrics/route")
def metrics_route(namespace: str = Query(...), name: str = Query(...),
                  window: int = Query(default=30, ge=5, le=1440)):
    """Time series (rps / p95 / error-rate) for one route — powers the drawer charts."""
    _valid_name(namespace)
    _valid_name(name)
    if not prom.enabled():
        return {"enabled": False}
    # step: keep ~60-120 points across the window
    step = max(15, (window * 60) // 90)
    try:
        return prom.route_timeseries(f"{namespace}/{name}", window, step)
    except Exception as exc:  # noqa: BLE001
        log.warning("prometheus timeseries failed: %s", exc)
        return {"enabled": True, "error": str(exc), "rps": [], "p95Ms": [], "errorRate": []}


@app.get("/api/metrics/routes")
def metrics_routes(namespace: str | None = Query(default=None),
                   keys: str | None = Query(default=None)):
    """Per-route RPS / p95 / error-rate + sparkline, keyed by '<ns>/<name>'.

    Pass `keys=ns/name,ns/name,…` (the visible page) to scope the Prometheus
    queries to just those routes. Returns {"enabled": false} when unconfigured.
    """
    _valid_keys(keys)
    if not prom.enabled():
        return {"enabled": False, "items": {}}
    key_list = [x for x in (keys.split(",") if keys else []) if x.strip()]
    try:
        data = prom.route_metrics(key_list or None)
    except Exception as exc:  # noqa: BLE001 — metrics must never break a page
        log.warning("prometheus query failed: %s", exc)
        return {"enabled": True, "items": {}, "error": str(exc)}
    if not key_list and namespace and namespace != "all":
        data = {k: v for k, v in data.items() if k.startswith(f"{namespace}/")}
    return {"enabled": True, "items": data}


# ---------------------------------------------------------------------------
# write operations (opt-in, WRITE_ENABLED) — create / update / delete
# ---------------------------------------------------------------------------

def _require_write(request: Request):
    if not WRITE_ENABLED:
        raise HTTPException(status_code=403, detail="write mode disabled (WRITE_ENABLED=false)")
    _check_csrf(request)


@app.post("/api/apply")
def apply_manifest(request: Request, body: dict):
    """Create or update one or more objects from a YAML manifest (write mode)."""
    _require_write(request)
    k = kube()
    raw = body.get("yaml")
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="empty manifest")
    try:
        docs = [d for d in yaml.safe_load_all(raw) if d]
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"invalid YAML: {exc}") from exc
    if not docs:
        raise HTTPException(status_code=400, detail="no objects in manifest")
    results = []
    for doc in docs:
        try:
            res = k.apply_object(doc)
            results.append({"kind": doc.get("kind"),
                            "name": doc.get("metadata", {}).get("name"),
                            "namespace": doc.get("metadata", {}).get("namespace"),
                            "action": res["action"]})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — surface apiserver errors to the UI
            detail = getattr(exc, "body", None) or str(exc)
            raise HTTPException(status_code=422, detail=f"apply failed: {detail}") from exc
    return {"results": results}


@app.delete("/api/object")
def delete_object(request: Request, kind: str, name: str, namespace: str | None = Query(default=None)):
    _require_write(request)
    k = kube()
    if kind not in KIND_REGISTRY:
        raise HTTPException(status_code=400, detail=f"unsupported kind {kind}")
    try:
        k.delete_object(kind, name, _ns(namespace))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "body", None) or str(exc)
        raise HTTPException(status_code=422, detail=f"delete failed: {detail}") from exc
    return {"deleted": {"kind": kind, "name": name, "namespace": namespace}}


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
