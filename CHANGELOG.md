# Changelog

## v1.1.0

The dashboard grows an optional **write mode** and **Prometheus route metrics**, scales to
1000+ routes, and gets a round of security hardening. Everything new is opt-in and off by
default — a plain `helm upgrade` with no value changes behaves exactly like v1.0.0.

### Added
- **Write mode** (`write.enabled` / `WRITE_ENABLED=true`) — create, edit and delete Gateway API
  objects from the UI via a guided form or raw YAML (`POST /api/apply`, `DELETE /api/object`),
  gated by RBAC and the existing auth. A **New** button and per-object **Edit** / **Delete**
  appear in the drawer only when enabled.
- **Multi-rule route form** — handles HTTPRoutes with multiple rules, each with its own path,
  backends, **URLRewrite** (replace-prefix), **set request headers** and **timeouts**. Also
  covers GRPCRoute, TLSRoute and TCPRoute, with searchable gateway / listener / service pickers.
  Objects the form can't model still open as YAML.
- **Prometheus route metrics** (`prometheus.url` / `PROMETHEUS_URL`) — RPS / p95 latency /
  error-rate + sparkline on route cards, and a **Metrics tab** in the drawer with full
  time-series charts (selectable 15m–6h window). Queries are templated to fit any metrics
  pipeline and scoped to only the routes on screen, so neither the browser nor Prometheus is
  hit with everything at once. `/api/metrics/check` reports reachability for troubleshooting.
- **Scales to 1000+ routes** — the Routes view is server-paginated (slim payloads, infinite
  scroll) with server-side search; full per-rule detail loads lazily in the drawer.
- **`NetworkPolicy`** (`networkPolicy.enabled`, off by default) — restricts direct Service access
  to the same namespace plus explicit peers you list (your gateway's Envoy proxy, Prometheus),
  so identity headers can't be forged by bypassing the gateway.

### Changed
- ClusterRole/ClusterRoleBinding are now named `<release>` instead of `<release>-readonly` (the
  same object now carries write verbs when `write.enabled=true`). **Upgrade note:** this leaves
  the old `-readonly`-suffixed ClusterRole/Binding orphaned in the cluster — Helm no longer
  manages them; delete them manually once you've confirmed the new ones are in place.

### Fixed (hardening)
- Route-metrics query params (`namespace`, `name`, `keys`) are now strictly validated —
  previously crafted values could break out of the PromQL label selector.
- Write endpoints (`/api/apply`, `DELETE /api/object`) now reject cross-origin requests
  (Origin/Referer vs Host check) as defense-in-depth alongside the existing JSON-only,
  no-CORS posture.
- The apiserver cache no longer stampedes on TTL expiry: concurrent refreshes of the same key
  now collapse into a single upstream request (singleflight).
- Prometheus queries reuse a pooled HTTP client (keep-alive, `certifi`-verified TLS) instead of
  opening a new connection per request.

## v1.0.0 — Initial release

First public release of **Gateway API UI** — a read-only web dashboard for the Kubernetes
**Gateway API** (`gateway.networking.k8s.io`). A spiritual successor to the old Traefik dashboard
for Envoy-based gateways (Cilium, Envoy Gateway, NGINX, Istio, …), including the Envoy AI Gateway.

### Highlights

- **Overview** — counts, health summary, and a *problems* list that surfaces the exact
  `Accepted` / `Programmed` / `ResolvedRefs` condition reason behind a broken route.
- **Gateways / Routes / Policies** — listeners (host/port/protocol/TLS), HTTP/GRPC/TLS/TCP routes
  (matches, filters, weighted backends), and Envoy/AI policies with resolved `targetRef`s.
- **Topology** — GatewayClass → Gateway → Route → Backend graph with hover highlighting, live
  endpoint counts (catches "route OK but 0 endpoints"), and click-through.
- **Detail drawer** — Summary (conditions), Related (clickable neighbours with health), raw YAML,
  plus copy-YAML / copy-`kubectl`.
- **⌘K command palette** and keyboard/a11y-first UX (focus trap, `aria-*`, reduced-motion), light/dark.

### Run modes

One image, two modes: **local** via `~/.kube/config`, or **in-cluster** via a ServiceAccount with a
minimal read-only ClusterRole (`get`/`list`/`watch` only). Tolerates missing CRDs (TLS/TCP routes,
Envoy AI Gateway, policies) by hiding those sections.

### Authentication & authorization

Auth is handled at the edge (Envoy Gateway + Keycloak OIDC). The app reads the forwarded identity
headers and enforces an optional Keycloak **group allowlist** (`AUTH_ENABLED` / `AUTH_ALLOWED_GROUPS`),
with a user header, logout, and *Access denied* screen. Group claims forwarded as base64-encoded
JSON are decoded automatically.

### Observability

Prometheus `/metrics` exposes UI HTTP metrics **and** live Gateway API state
(`gatewayapi_gateways{health}`, `gatewayapi_routes{type,health}`, `gatewayapi_policies{kind,health}`,
…) so you can alert on unhealthy routes. A `ServiceMonitor` ships in the Helm chart.

### Install

Docker image (multi-arch `linux/amd64` + `linux/arm64`):

```bash
docker pull wacken/gateway-api-ui:1.0.0
```

Helm:

```bash
helm repo add gateway-api-ui https://wacken89.github.io/gateway-api-ui
helm install gateway-api-ui gateway-api-ui/gateway-api-ui \
  --namespace gateway-api-ui --create-namespace
```

### Notes

- Read-only by design; runs non-root with a read-only root filesystem and dropped capabilities.
- Build-free frontend (vanilla Alpine.js + handcrafted CSS); no Node/Tailwind step.
- Licensed under Apache-2.0.
