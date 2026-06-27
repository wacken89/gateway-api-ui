# Changelog

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
