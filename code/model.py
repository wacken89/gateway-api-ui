"""Normalize raw Gateway API objects into compact view models + a topology graph.

The frontend stays dumb: it renders whatever shape we produce here. All the
Gateway API spec knowledge (parentRefs, listener attachment, backendRefs,
condition reasons) lives in this module.
"""

from __future__ import annotations

from typing import Any

ROUTE_KINDS = {
    "http": "httproutes",
    "grpc": "grpcroutes",
    "tls": "tlsroutes",
    "tcp": "tcproutes",
}


def _meta(obj: dict) -> dict:
    m = obj.get("metadata", {})
    return {
        "name": m.get("name"),
        "namespace": m.get("namespace"),
        "uid": m.get("uid"),
        "created": m.get("creationTimestamp"),
        "labels": m.get("labels", {}),
    }


def _conditions(obj: dict) -> list[dict]:
    conds = (obj.get("status") or {}).get("conditions") or []
    return [
        {
            "type": c.get("type"),
            "status": c.get("status"),
            "reason": c.get("reason"),
            "message": c.get("message"),
        }
        for c in conds
    ]


def _health_from_conditions(conds: list[dict]) -> str:
    """Roll a set of conditions into ok | warn | error | unknown."""
    if not conds:
        return "unknown"
    bad = [c for c in conds if c.get("status") == "False"]
    if any(c.get("type") in ("Accepted", "Programmed", "ResolvedRefs") for c in bad):
        return "error"
    if bad:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# GatewayClass
# ---------------------------------------------------------------------------

def gatewayclass_view(obj: dict) -> dict:
    spec = obj.get("spec", {})
    conds = _conditions(obj)
    return {
        "kind": "GatewayClass",
        **_meta(obj),
        "controller": spec.get("controllerName"),
        "description": spec.get("description"),
        "conditions": conds,
        "health": _health_from_conditions(conds),
    }


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

def gateway_view(obj: dict) -> dict:
    spec = obj.get("spec", {})
    status = obj.get("status") or {}
    listeners = []
    # status.listeners carries attachedRoutes + per-listener conditions
    status_listeners = {l.get("name"): l for l in (status.get("listeners") or [])}
    for l in spec.get("listeners", []):
        sl = status_listeners.get(l.get("name"), {})
        lconds = [
            {"type": c.get("type"), "status": c.get("status"),
             "reason": c.get("reason"), "message": c.get("message")}
            for c in (sl.get("conditions") or [])
        ]
        tls = l.get("tls") or {}
        listeners.append({
            "name": l.get("name"),
            "hostname": l.get("hostname"),
            "port": l.get("port"),
            "protocol": l.get("protocol"),
            "tlsMode": tls.get("mode"),
            "tlsRefs": [r.get("name") for r in (tls.get("certificateRefs") or [])],
            "allowedRoutes": (l.get("allowedRoutes") or {}).get("namespaces", {}),
            "attachedRoutes": sl.get("attachedRoutes", 0),
            "conditions": lconds,
            "health": _health_from_conditions(lconds),
        })

    addresses = [a.get("value") for a in (status.get("addresses") or spec.get("addresses") or [])]
    conds = _conditions(obj)
    return {
        "kind": "Gateway",
        **_meta(obj),
        "gatewayClassName": spec.get("gatewayClassName"),
        "addresses": addresses,
        "listeners": listeners,
        "conditions": conds,
        "health": _health_from_conditions(conds + [c for l in listeners for c in l["conditions"]]),
        "attachedRoutes": sum(l["attachedRoutes"] for l in listeners),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _backend_refs(rule: dict, route_ns: str) -> list[dict]:
    out = []
    for b in (rule.get("backendRefs") or []):
        out.append({
            "name": b.get("name"),
            "namespace": b.get("namespace") or route_ns,
            "port": b.get("port"),
            "weight": b.get("weight", 1),
            "kind": b.get("kind", "Service"),
            "group": b.get("group", ""),
        })
    return out


def _http_matches(rule: dict) -> list[dict]:
    out = []
    for m in (rule.get("matches") or []):
        path = m.get("path") or {}
        out.append({
            "path": f"{path.get('type', 'PathPrefix')}:{path.get('value', '/')}",
            "method": m.get("method"),
            "headers": [f"{h.get('name')}={h.get('value')}" for h in (m.get("headers") or [])],
            "queryParams": [f"{q.get('name')}={q.get('value')}" for q in (m.get("queryParams") or [])],
        })
    return out or [{"path": "PathPrefix:/", "method": None, "headers": [], "queryParams": []}]


def _http_filters(rule: dict) -> list[str]:
    out = []
    for f in (rule.get("filters") or []):
        out.append(f.get("type", "Filter"))
    return out


def _parent_refs(spec: dict, route_ns: str) -> list[dict]:
    out = []
    for p in (spec.get("parentRefs") or []):
        out.append({
            "name": p.get("name"),
            "namespace": p.get("namespace") or route_ns,
            "sectionName": p.get("sectionName"),
            "port": p.get("port"),
            "kind": p.get("kind", "Gateway"),
        })
    return out


def route_view(obj: dict, rtype: str) -> dict:
    spec = obj.get("spec", {})
    meta = _meta(obj)
    ns = meta["namespace"]
    rules = []
    for r in (spec.get("rules") or []):
        rule: dict[str, Any] = {"backendRefs": _backend_refs(r, ns)}
        if rtype in ("http", "grpc"):
            rule["matches"] = _http_matches(r)
            rule["filters"] = _http_filters(r)
        rules.append(rule)

    # Route status conditions are nested per-parent under status.parents[].conditions
    status_parents = (obj.get("status") or {}).get("parents") or []
    parent_conditions = []
    for p in status_parents:
        for c in (p.get("conditions") or []):
            parent_conditions.append({
                "type": c.get("type"), "status": c.get("status"),
                "reason": c.get("reason"), "message": c.get("message"),
                "controller": p.get("controllerName"),
            })

    return {
        "kind": {"http": "HTTPRoute", "grpc": "GRPCRoute",
                 "tls": "TLSRoute", "tcp": "TCPRoute"}[rtype],
        "routeType": rtype,
        **meta,
        "hostnames": spec.get("hostnames", []),
        "parentRefs": _parent_refs(spec, ns),
        "rules": rules,
        "conditions": parent_conditions,
        "health": _health_from_conditions(parent_conditions),
        "backendCount": sum(len(r["backendRefs"]) for r in rules),
    }


_ROUTE_KIND = {"http": "HTTPRoute", "grpc": "GRPCRoute", "tls": "TLSRoute", "tcp": "TCPRoute"}


def route_summary(obj: dict, rtype: str) -> dict:
    """Slim route view for the (paginated) list.

    Carries a *compact* per-rule breakdown (path -> backends + filter types) so
    the card shows which URL routes to which service, but omits the heavy parts
    (header/query matches, backend weights, full filter configs). Full detail is
    loaded on demand via /api/object when a route is opened in the drawer.
    """
    spec = obj.get("spec", {})
    meta = _meta(obj)
    meta.pop("labels", None)
    ns = meta["namespace"]
    rules = []
    all_backends = []
    for r in (spec.get("rules") or []):
        matches = r.get("matches") or []
        m0 = matches[0] if matches else {}
        p = m0.get("path") or {}
        path = f"{p.get('type', 'PathPrefix')}:{p.get('value', '/')}" if p else None
        rbackends = [{"name": b.get("name"), "port": b.get("port")}
                     for b in (r.get("backendRefs") or [])]
        all_backends.extend(rbackends)
        rules.append({
            "path": path,
            "method": m0.get("method"),
            "backends": rbackends,
            "filters": [f.get("type") for f in (r.get("filters") or [])],
        })
    status_parents = (obj.get("status") or {}).get("parents") or []
    pconds = [c for p in status_parents for c in (p.get("conditions") or [])]
    return {
        "kind": _ROUTE_KIND[rtype],
        "routeType": rtype,
        **meta,
        "hostnames": spec.get("hostnames", []),
        "parentRefs": _parent_refs(spec, ns),
        "rules": rules,
        "ruleCount": len(rules),
        "backendCount": len(all_backends),
        "backends": all_backends[:6],
        "health": _health_from_conditions(pconds),
    }


# ---------------------------------------------------------------------------
# Controller / provider identification (Cilium, Envoy, NGINX, …)
# ---------------------------------------------------------------------------

# substring(controllerName) -> (key, display name, brand colour). First match wins,
# so order matters where names overlap. `key` doubles as the logo filename:
# drop static/vendor/logos/<key>.svg to show a real logo instead of the monogram.
_PROVIDER_TABLE = [
    ("cilium", "Cilium", "#16a34a"),
    ("envoyproxy", "Envoy Gateway", "#ac6199"),
    ("envoy", "Envoy", "#ac6199"),
    ("nginx", "NGINX", "#009639"),
    ("istio", "Istio", "#466bb0"),
    ("traefik", "Traefik", "#24a1c1"),
    ("kong", "Kong", "#1b4d9b"),
    ("haproxy", "HAProxy", "#106da4"),
    ("contour", "Contour", "#6f42c1"),
    ("gke", "GKE Gateway", "#4285f4"),
    ("aws", "AWS Gateway", "#ff9900"),
]


def provider_of(controller: str | None) -> dict:
    c = (controller or "").lower()
    for key, name, color in _PROVIDER_TABLE:
        if key in c:
            return {"key": key, "name": name, "color": color}
    short = controller.split("/")[-1] if controller else "unknown"
    return {"key": "generic", "name": short, "color": "#64748b"}


# ---------------------------------------------------------------------------
# Policies (Envoy Gateway: Security/ClientTraffic/BackendTraffic; AI: BackendSecurity)
# ---------------------------------------------------------------------------

def _target_refs(spec: dict, ns: str) -> list[dict]:
    raw = spec.get("targetRefs")
    if raw is None and spec.get("targetRef"):
        raw = [spec["targetRef"]]
    out = []
    for t in (raw or []):
        out.append({
            "group": t.get("group", ""),
            "kind": t.get("kind"),
            "name": t.get("name"),
            "namespace": t.get("namespace") or ns,
            "sectionName": t.get("sectionName"),
        })
    return out


def _policy_conditions(obj: dict) -> list[dict]:
    """Policies report status either flat (status.conditions) or per-ancestor."""
    status = obj.get("status") or {}
    out = []
    for c in (status.get("conditions") or []):
        out.append({"type": c.get("type"), "status": c.get("status"),
                    "reason": c.get("reason"), "message": c.get("message")})
    for a in (status.get("ancestors") or []):
        for c in (a.get("conditions") or []):
            out.append({"type": c.get("type"), "status": c.get("status"),
                        "reason": c.get("reason"), "message": c.get("message")})
    return out


def policy_view(obj: dict, kind: str) -> dict:
    spec = obj.get("spec", {})
    meta = _meta(obj)
    conds = _policy_conditions(obj)
    return {
        "kind": kind,
        "isPolicy": True,
        **meta,
        "targetRefs": _target_refs(spec, meta["namespace"]),
        "conditions": conds,
        "health": _health_from_conditions(conds),
    }


# ---------------------------------------------------------------------------
# Topology graph
# ---------------------------------------------------------------------------

def build_graph(gatewayclasses: list[dict], gateways: list[dict],
                routes: list[dict], services: dict[str, dict],
                endpoints: dict[str, int]) -> dict:
    """Produce {nodes, edges} for the topology view.

    Columns: gatewayclass -> gateway -> route -> backend.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(node: dict) -> None:
        if node["id"] not in seen:
            seen.add(node["id"])
            nodes.append(node)

    gw_index = {f"{g['namespace']}/{g['name']}": g for g in gateways}

    for gc in gatewayclasses:
        add_node({"id": f"gatewayclass/{gc['name']}", "type": "gatewayclass",
                  "label": gc["name"], "sub": gc.get("controller", ""),
                  "health": gc["health"], "ref": {"kind": "GatewayClass", "name": gc["name"]}})

    for g in gateways:
        gid = f"gateway/{g['namespace']}/{g['name']}"
        listeners = ", ".join(
            f"{l['protocol']}:{l['port']}" for l in g["listeners"]) or "—"
        add_node({"id": gid, "type": "gateway", "label": g["name"],
                  "sub": f"{g['namespace']} · {listeners}", "health": g["health"],
                  "ref": {"kind": "Gateway", "name": g["name"], "namespace": g["namespace"]}})
        if g.get("gatewayClassName"):
            cid = f"gatewayclass/{g['gatewayClassName']}"
            if cid not in seen:
                add_node({"id": cid, "type": "gatewayclass", "label": g["gatewayClassName"],
                          "sub": "", "health": "unknown",
                          "ref": {"kind": "GatewayClass", "name": g["gatewayClassName"]}})
            edges.append({"source": cid, "target": gid})

    for rt in routes:
        rid = f"route/{rt['namespace']}/{rt['name']}"
        hosts = ", ".join(rt.get("hostnames") or []) or "*"
        add_node({"id": rid, "type": "route", "label": rt["name"],
                  "sub": f"{rt['kind']} · {hosts}", "health": rt["health"],
                  "ref": {"kind": rt["kind"], "name": rt["name"],
                          "namespace": rt["namespace"], "routeType": rt["routeType"]}})
        # attach to gateways via parentRefs
        for p in rt["parentRefs"]:
            target = f"gateway/{p['namespace']}/{p['name']}"
            if target in seen:
                edges.append({"source": target, "target": rid,
                              "label": p.get("sectionName") or ""})
        # backends
        for rule in rt["rules"]:
            for b in rule["backendRefs"]:
                key = f"{b['namespace']}/{b['name']}"
                bid = f"backend/{key}:{b.get('port')}"
                svc = services.get(key)
                ready = endpoints.get(key)
                if ready is None:
                    bhealth = "unknown" if svc is None else "warn"
                elif ready == 0:
                    bhealth = "error"
                else:
                    bhealth = "ok"
                add_node({"id": bid, "type": "backend", "label": b["name"],
                          "sub": f"{b['namespace']}:{b.get('port')} · "
                                 f"{'?' if ready is None else ready} eps",
                          "health": bhealth,
                          "missing": svc is None,
                          "ref": {"kind": b.get("kind", "Service"), "name": b["name"],
                                  "namespace": b["namespace"]}})
                edges.append({"source": rid, "target": bid,
                              "label": f"w{b.get('weight', 1)}"})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# AI Gateway (optional)
# ---------------------------------------------------------------------------

def ai_route_view(obj: dict) -> dict:
    spec = obj.get("spec", {})
    meta = _meta(obj)
    rules = []
    for r in (spec.get("rules") or []):
        matches = []
        for m in (r.get("matches") or []):
            matches.append([f"{h.get('name')}={h.get('value')}" for h in (m.get("headers") or [])])
        backends = [{"name": b.get("name"), "weight": b.get("weight", 1)}
                    for b in (r.get("backendRefs") or [])]
        rules.append({"matches": matches, "backendRefs": backends})
    costs = [{"key": c.get("metadataKey"), "type": c.get("type")}
             for c in (spec.get("llmRequestCosts") or [])]
    return {
        "kind": "AIGatewayRoute",
        **meta,
        "parentRefs": _parent_refs(spec, meta["namespace"]),
        "llmRequestCosts": costs,
        "rules": rules,
    }
