"""Authorization layer.

Authentication happens at the edge: Envoy Gateway runs the OIDC flow against
Keycloak (a SecurityPolicy), so by the time a request reaches this app the user is
already authenticated. Envoy forwards the identity as request headers (the
SecurityPolicy maps JWT claims -> headers).

This module only does AUTHORIZATION: it reads those headers and enforces a group
allowlist, so people outside the allowed Keycloak groups can't see anything.

Everything is gated by AUTH_ENABLED. When disabled the app is fully open
(local / kubeconfig use, or clusters where the gateway already restricts access).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re

_B64_RE = re.compile(r"^[A-Za-z0-9_\-+/]+={0,2}$")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _csv(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").replace(";", ",").split(",") if x.strip()]


def _str_list(value) -> list[str] | None:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return None


def parse_groups(raw: str | None) -> list[str]:
    """Parse the forwarded groups header into a clean list of group names.

    Envoy Gateway's JWT `claimToHeaders` forwards a *list* claim as base64-encoded
    JSON (e.g. base64('["admins","devs"]')) and a *string* claim verbatim. Handle
    both, plus a bare JSON array and the simple delimited-string case.
    """
    if not raw:
        return []
    s = raw.strip()

    # 1. bare JSON array
    if s.startswith("["):
        try:
            out = _str_list(json.loads(s))
            if out is not None:
                return out
        except ValueError:
            pass

    # 2. base64-encoded JSON array (Envoy Gateway list-claim encoding)
    if _B64_RE.match(s):
        compact = s.rstrip("=")
        padded = compact + "=" * (-len(compact) % 4)
        for decoder in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                txt = decoder(padded).decode("utf-8").strip()
            except (binascii.Error, ValueError):
                continue
            if txt.startswith("["):
                try:
                    out = _str_list(json.loads(txt))
                    if out is not None:
                        return out
                except ValueError:
                    continue

    # 3. delimited string ("a,b" / "/a;/b") or a single group name
    return _csv(s)


def _norm(group: str) -> str:
    # Keycloak group claims may be "/gateway-admins" or "gateway-admins".
    return group.strip().lstrip("/").lower()


class AuthConfig:
    def __init__(self) -> None:
        self.enabled = _bool(os.environ.get("AUTH_ENABLED"), False)
        # Header names the gateway uses to forward identity (claim -> header).
        self.name_header = os.environ.get("AUTH_NAME_HEADER", "X-Auth-Request-User")
        self.username_header = os.environ.get(
            "AUTH_USERNAME_HEADER", "X-Auth-Request-Preferred-Username")
        self.email_header = os.environ.get("AUTH_EMAIL_HEADER", "X-Auth-Request-Email")
        self.groups_header = os.environ.get("AUTH_GROUPS_HEADER", "X-Auth-Request-Groups")
        self.allowed_groups = _csv(os.environ.get("AUTH_ALLOWED_GROUPS"))
        # Path Envoy intercepts to clear the OIDC session (SecurityPolicy logoutPath).
        self.logout_path = os.environ.get("AUTH_LOGOUT_PATH", "/logout")


CONFIG = AuthConfig()


def is_allowed(groups: list[str]) -> bool:
    """True if the user may use the app."""
    if not CONFIG.enabled:
        return True
    if not CONFIG.allowed_groups:
        # Enabled but no allowlist => any authenticated user is allowed.
        return True
    allowed = {_norm(a) for a in CONFIG.allowed_groups}
    have = {_norm(g) for g in groups}
    return bool(allowed & have)


def identity(headers) -> dict:
    """Build the identity view model from forwarded request headers."""
    cfg = CONFIG
    name = headers.get(cfg.name_header) or headers.get(cfg.username_header)
    username = headers.get(cfg.username_header)
    email = headers.get(cfg.email_header)
    groups = parse_groups(headers.get(cfg.groups_header))
    authenticated = bool(name or username or email or groups)
    return {
        "authEnabled": cfg.enabled,
        # With auth off, treat everyone as an allowed anonymous user.
        "authenticated": authenticated if cfg.enabled else True,
        "allowed": is_allowed(groups) if cfg.enabled else True,
        "name": name or username or email,
        "username": username,
        "email": email,
        "groups": groups,
        "allowedGroups": cfg.allowed_groups,
        "logoutUrl": cfg.logout_path,
    }
