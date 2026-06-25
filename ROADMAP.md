# Gateway API UI — Roadmap

A read-only web dashboard for the Kubernetes **Gateway API** (`gateway.networking.k8s.io`),
the way the old Traefik dashboard let us see entrypoints / routers / services at a glance.
Our gateways are now Envoy-based (Cilium Gateway API for north-south traffic, Envoy AI
Gateway for LLM traffic), so this tool visualizes the Gateway API object graph:

```
GatewayClass ──▶ Gateway (+ Listeners) ──▶ HTTP/GRPC/TLS/TCP Route ──▶ Backend (Service)
```

## Goals

- Give SRE / developers a **single pane of glass** over all Gateway API objects across namespaces.
- **Traefik-dashboard parity**: see what is exposed, on which host/port/path, and where it routes.
- **Status at a glance**: surface `Accepted` / `Programmed` / `ResolvedRefs` conditions and the
  reasons behind a broken route (the #1 debugging question: "why is my route 404/503?").
- **Two run modes from one binary**:
  - local — uses `~/.kube/config` (current context) for ad-hoc inspection;
  - in-cluster — uses the pod's **ServiceAccount** + read-only RBAC.
- **Read-only & safe**: no mutating verbs, runs non-root, minimal RBAC (`get`/`list`/`watch`).

## Non-goals (for now)

- Editing / applying / deleting resources (read-only by design).
- Replacing `kubectl` / Hubble / Grafana for raw metrics & flow logs (we link out instead).
- Multi-cluster federation in a single view (one kubeconfig context / one cluster per instance).

## Architecture

```
                 ┌──────────────────────────────────────────────┐
  Kubernetes ──▶ │  kube-apiserver                               │
   API           │   gateway.networking.k8s.io/v1  Gateway,      │
                 │     HTTPRoute, GRPCRoute, GatewayClass        │
                 │   .../v1alpha2  TLSRoute, TCPRoute            │
                 │   aigateway.envoyproxy.io  AIGatewayRoute …   │
                 └───────────────────┬──────────────────────────┘
                                     │ list / watch (read-only)
                 ┌───────────────────┴──────────────────────────┐
                 │  gateway-api-ui  (FastAPI)                     │  ◀── browser (SPA)
                 │   • kubeconfig OR in-cluster ServiceAccount    │
                 │   • normalizes objects → graph (nodes/edges)   │
                 │   • same-origin JSON API at /api/*             │
                 └───────────────────────────────────────────────┘
```

- **Backend** — Python **FastAPI** + official `kubernetes` client. Loads in-cluster config
  when `KUBERNETES_SERVICE_HOST` is present, otherwise falls back to kubeconfig
  (`KUBECONFIG` / `~/.kube/config`, context selectable via `KUBE_CONTEXT`). Tolerates missing
  CRDs (e.g. no Envoy AI Gateway installed) by degrading gracefully. Short TTL cache to avoid
  hammering the apiserver on auto-refresh.
- **Frontend** — static SPA: **Alpine.js + Lucide icons + Tailwind** (precompiled, committed,
  no Node in the image). Light/dark theme, namespace
  selector, fuzzy search, auto-refresh, detail drawer, topology graph.
- **Packaging** — single non-root container image; plain manifests in `deploy/` (ServiceAccount
  + read-only ClusterRole + Deployment + Service) and a Helm chart.

## Object model (what we render)

| Gateway API kind        | Group/version                         | Traefik analog      |
|-------------------------|---------------------------------------|---------------------|
| `GatewayClass`          | `gateway.networking.k8s.io/v1`        | provider            |
| `Gateway` + `Listener`  | `gateway.networking.k8s.io/v1`        | entrypoint          |
| `HTTPRoute`             | `gateway.networking.k8s.io/v1`        | router + middleware |
| `GRPCRoute`             | `gateway.networking.k8s.io/v1`        | router              |
| `TLSRoute` / `TCPRoute` | `gateway.networking.k8s.io/v1alpha2`  | TCP/TLS router      |
| `ReferenceGrant`        | `gateway.networking.k8s.io/v1beta1`   | cross-ns trust      |
| backend `Service`       | `core/v1`                             | service             |
| `AIGatewayRoute` (opt)  | `aigateway.envoyproxy.io/v1alpha1`    | LLM router          |

## Milestones

### M0 — Foundations ✅ (this commit)
- Repo scaffold, Dockerfile (non-root), Makefile, requirements.
- Dual config loader (kubeconfig + in-cluster) with context/health endpoint.
- Read-only RBAC manifests + Deployment/Service.

### M1 — Core read-only dashboard ✅ (this commit)
- **Overview**: cluster/context badge, object counts, health summary, problem list.
- **Gateways**: list + listeners (host/port/protocol/TLS), addresses, attached-routes count,
  `Programmed`/`Accepted` status with reasons.
- **Routes** (HTTP/GRPC/TLS/TCP): hostnames, matches (path/method/headers), filters,
  backendRefs with weights, `ResolvedRefs` status.
- **Detail drawer** with raw YAML view per object.
- **Topology**: GatewayClass → Gateway/Listener → Route → Backend flow graph with
  hover highlighting and click-through.
- UX: namespace selector, global search, light/dark theme, auto-refresh (configurable),
  empty/error/loading states, deep-linkable views.

### M2 — Debugging depth
- Resolve `backendRefs` to live `Service`/`EndpointSlice` and show ready endpoint counts
  (catch "route OK but 0 endpoints" cases).
- Surface conflicting/duplicate hostnames & listener conflicts.
- Cross-namespace `ReferenceGrant` validation (does a route actually have permission?).
- Per-object event timeline (`kubectl get events` for the object).
- "Why is this broken?" inline explanations derived from condition reasons.

### M3 — Live & observability
- `watch`-based live updates over SSE/WebSocket (replace polling).
- Request-path tester: given host+path+method, compute which listener/route/backend matches.
- Deep links to Hubble / Grafana / Kiali for the selected route's flows & metrics.
- Envoy AI Gateway tab: AIGatewayRoute → model backends, token-cost rules, weights.

### M4 — Hardening & adoption
- OIDC/SSO in front (oauth2-proxy / Keycloak) for the in-cluster deployment.
- Optional per-namespace RBAC scoping (show only what the SA can see).
- Multi-context switcher for local mode.
- Prometheus metrics for the UI itself + Helm chart published to an OCI registry (GHCR).

## Security posture

- Strictly read-only verbs (`get`, `list`, `watch`); no `create`/`update`/`delete` anywhere.
- Runs as non-root (uid 10001), read-only root FS, drops all caps.
- In-cluster: dedicated ServiceAccount, ClusterRole limited to Gateway API + read-only core.
- No secrets are rendered (TLS `Secret` refs are shown by name only, never contents).
- Front it with SSO before exposing outside the cluster.
