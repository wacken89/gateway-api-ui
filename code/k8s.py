"""Kubernetes access layer.

Loads either an in-cluster ServiceAccount config (when running inside a pod) or a
kubeconfig context (for local use), and exposes thin, cached readers for the
Gateway API CRDs plus the core resources we need to resolve backends.

Everything here is read-only: we only ever call `list_*`. The class tolerates
missing CRDs (e.g. a cluster without the Envoy AI Gateway installed) by returning
empty results instead of raising.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

GROUP = "gateway.networking.k8s.io"
AI_GROUP = "aigateway.envoyproxy.io"
ENVOY_GW_GROUP = "gateway.envoyproxy.io"

# plural -> ordered list of versions to try (newest first). The reader walks the
# list and uses the first version the apiserver actually serves.
GATEWAY_RESOURCES: dict[str, list[str]] = {
    "gatewayclasses": ["v1", "v1beta1"],
    "gateways": ["v1", "v1beta1"],
    "httproutes": ["v1", "v1beta1"],
    "grpcroutes": ["v1", "v1alpha2"],
    "tlsroutes": ["v1alpha2"],
    "tcproutes": ["v1alpha2"],
    "udproutes": ["v1alpha2"],
    "referencegrants": ["v1beta1", "v1alpha2"],
}

AI_RESOURCES: dict[str, list[str]] = {
    "aigatewayroutes": ["v1alpha1"],
    "aiservicebackends": ["v1alpha1"],
}

# Envoy Gateway / AI Gateway policy CRDs (optional — absent on plain Cilium).
# kind -> plural, used for the Policies view and target resolution.
POLICY_KINDS: dict[str, str] = {
    "SecurityPolicy": "securitypolicies",
    "ClientTrafficPolicy": "clienttrafficpolicies",
    "BackendTrafficPolicy": "backendtrafficpolicies",
    "BackendSecurityPolicy": "backendsecuritypolicies",
}

# Central kind registry: kind -> (group, versions newest-first, plural, namespaced).
# Powers list_kind(), the object detail endpoint and related-resource resolution.
KIND_REGISTRY: dict[str, tuple[str, list[str], str, bool]] = {
    "GatewayClass": (GROUP, ["v1", "v1beta1"], "gatewayclasses", False),
    "Gateway": (GROUP, ["v1", "v1beta1"], "gateways", True),
    "HTTPRoute": (GROUP, ["v1", "v1beta1"], "httproutes", True),
    "GRPCRoute": (GROUP, ["v1", "v1alpha2"], "grpcroutes", True),
    "TLSRoute": (GROUP, ["v1alpha2"], "tlsroutes", True),
    "TCPRoute": (GROUP, ["v1alpha2"], "tcproutes", True),
    "AIGatewayRoute": (AI_GROUP, ["v1alpha1"], "aigatewayroutes", True),
    "SecurityPolicy": (ENVOY_GW_GROUP, ["v1alpha1"], "securitypolicies", True),
    "ClientTrafficPolicy": (ENVOY_GW_GROUP, ["v1alpha1"], "clienttrafficpolicies", True),
    "BackendTrafficPolicy": (ENVOY_GW_GROUP, ["v1alpha1"], "backendtrafficpolicies", True),
    "BackendSecurityPolicy": (AI_GROUP, ["v1alpha1"], "backendsecuritypolicies", True),
}

CLUSTER_SCOPED = {"gatewayclasses"}


@dataclass
class _CacheEntry:
    value: Any
    expires: float


class KubeClient:
    """Caching, CRD-tolerant reader for Gateway API objects."""

    def __init__(self, cache_ttl: float = 5.0) -> None:
        self._cache_ttl = cache_ttl
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        # Remember which (plural -> version) actually works so we don't probe twice.
        self._resolved_version: dict[str, str | None] = {}

        self.in_cluster = False
        self.context_name: str | None = None
        self._load_config()

        self.core = client.CoreV1Api()
        self.custom = client.CustomObjectsApi()
        self.version_api = client.VersionApi()

    # ----- config loading -------------------------------------------------

    def _load_config(self) -> None:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
            self.in_cluster = True
            self.context_name = os.environ.get("CLUSTER_NAME", "in-cluster")
            return

        kube_context = os.environ.get("KUBE_CONTEXT") or None
        config.load_kube_config(context=kube_context)
        try:
            _, active = config.list_kube_config_contexts()
            self.context_name = (active or {}).get("name")
        except Exception:
            self.context_name = kube_context

    # ----- low level cache ------------------------------------------------

    def _cached(self, key: str, producer):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit.expires > now:
                return hit.value
        value = producer()
        with self._lock:
            self._cache[key] = _CacheEntry(value, now + self._cache_ttl)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ----- generic custom-object reader -----------------------------------

    def _list_custom(self, group: str, versions: list[str], plural: str,
                     namespace: str | None) -> list[dict]:
        """List a (possibly absent) CRD, trying versions newest-first."""
        cached_version = self._resolved_version.get(plural, "__unset__")
        order = ([cached_version] if cached_version not in (None, "__unset__") else versions)

        for version in order:
            try:
                if plural in CLUSTER_SCOPED:
                    resp = self.custom.list_cluster_custom_object(group, version, plural)
                elif namespace:
                    resp = self.custom.list_namespaced_custom_object(
                        group, version, namespace, plural)
                else:
                    resp = self.custom.list_cluster_custom_object(group, version, plural)
                self._resolved_version[plural] = version
                return resp.get("items", [])
            except ApiException as exc:
                if exc.status in (404, 405):
                    continue  # CRD/version not served — try next
                if exc.status in (403,):
                    # Not allowed to read this kind; treat as empty but don't cache version.
                    return []
                raise
        self._resolved_version[plural] = None
        return []

    def list_gateway(self, plural: str, namespace: str | None = None) -> list[dict]:
        versions = GATEWAY_RESOURCES[plural]
        key = f"gw:{plural}:{namespace or '*'}"
        return self._cached(key, lambda: self._list_custom(GROUP, versions, plural, namespace))

    def list_ai(self, plural: str, namespace: str | None = None) -> list[dict]:
        versions = AI_RESOURCES[plural]
        key = f"ai:{plural}:{namespace or '*'}"
        return self._cached(key, lambda: self._list_custom(AI_GROUP, versions, plural, namespace))

    def list_kind(self, kind: str, namespace: str | None = None) -> list[dict]:
        """List any registered kind (Gateway API / Envoy / policies) by kind name."""
        reg = KIND_REGISTRY.get(kind)
        if not reg:
            return []
        group, versions, plural, namespaced = reg
        ns = namespace if namespaced else None
        key = f"kind:{kind}:{ns or '*'}"
        return self._cached(key, lambda: self._list_custom(group, versions, plural, ns))

    def policy_available(self, kind: str) -> bool:
        plural = POLICY_KINDS.get(kind)
        return plural is not None and self._resolved_version.get(plural) is not None

    # ----- core resources -------------------------------------------------

    def list_namespaces(self) -> list[str]:
        def _do() -> list[str]:
            try:
                items = self.core.list_namespace().items
                return sorted(ns.metadata.name for ns in items)
            except ApiException:
                # In-cluster SA may not be allowed to list namespaces cluster-wide;
                # fall back to whatever shows up on the objects themselves.
                return []
        return self._cached("namespaces", _do)

    def list_services(self, namespace: str | None = None) -> dict[str, dict]:
        """Return {"<ns>/<name>": service_dict} for backend resolution.

        Parsed from raw JSON to avoid the typed client choking on optional fields
        that some objects legitimately omit.
        """
        def _do() -> dict[str, dict]:
            out: dict[str, dict] = {}
            try:
                if namespace:
                    resp = self.core.list_namespaced_service(namespace, _preload_content=False)
                else:
                    resp = self.core.list_service_for_all_namespaces(_preload_content=False)
            except ApiException:
                return out
            for svc in json.loads(resp.data).get("items", []):
                md = svc.get("metadata", {})
                spec = svc.get("spec", {})
                ports = [
                    {"name": p.get("name"), "port": p.get("port"),
                     "protocol": p.get("protocol"), "targetPort": str(p.get("targetPort"))}
                    for p in (spec.get("ports") or [])
                ]
                key = f"{md.get('namespace')}/{md.get('name')}"
                out[key] = {"type": spec.get("type"), "clusterIP": spec.get("clusterIP"),
                            "ports": ports}
            return out
        return self._cached(f"svc:{namespace or '*'}", _do)

    def endpoint_counts(self, namespace: str | None = None) -> dict[str, int]:
        """Return {"<ns>/<name>": ready_address_count} from EndpointSlices.

        Parsed from raw JSON: a slice with no endpoints serializes as `endpoints:
        null`, which the typed v1_endpoint_slice model rejects with a ValueError.
        """
        def _do() -> dict[str, int]:
            counts: dict[str, int] = {}
            disc = client.DiscoveryV1Api()
            try:
                if namespace:
                    resp = disc.list_namespaced_endpoint_slice(namespace, _preload_content=False)
                else:
                    resp = disc.list_endpoint_slice_for_all_namespaces(_preload_content=False)
            except ApiException:
                return counts
            for sl in json.loads(resp.data).get("items", []):
                md = sl.get("metadata", {})
                svc = (md.get("labels") or {}).get("kubernetes.io/service-name")
                if not svc:
                    continue
                key = f"{md.get('namespace')}/{svc}"
                ready = 0
                for ep in (sl.get("endpoints") or []):
                    cond = ep.get("conditions") or {}
                    if cond.get("ready") in (None, True):
                        ready += len(ep.get("addresses") or [])
                counts[key] = counts.get(key, 0) + ready
            return counts
        return self._cached(f"eps:{namespace or '*'}", _do)

    def server_version(self) -> str:
        def _do() -> str:
            try:
                v = self.version_api.get_code()
                return f"{v.major}.{v.minor}".replace("+", "")
            except Exception:
                return "unknown"
        return self._cached("version", _do)
