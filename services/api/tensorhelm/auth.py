"""Who is calling, and what may they do.

In production ("proxy" mode) users sign in through the OpenShift oauth-proxy in front of the web pod. nginx in
the same pod forwards X-Forwarded-User to the API together with a shared secret, so a request that did not
pass through the proxy is rejected. Roles come from OpenShift groups.
"""

import hmac
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from tensorhelm.config import Settings, split_csv

ROLES = ("viewer", "engineer", "admin")


@dataclass(frozen=True)
class User:
    name: str
    groups: frozenset[str]
    role: str

    def has(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)


def role_for(groups: set[str], settings: Settings) -> str:
    if groups & settings.admin_group_set:
        return "admin"
    if groups & settings.engineer_group_set:
        return "engineer"
    return "viewer"


class GroupResolver:
    """Caches the OpenShift Group list and answers "which groups is this user in"."""

    def __init__(self, kube, ttl_s: int):
        self.kube = kube
        self.ttl_s = ttl_s
        self._expires = 0.0
        self._by_user: dict[str, set[str]] = {}

    async def groups_for(self, user: str) -> set[str]:
        if self.kube is None:
            return set()
        if time.monotonic() >= self._expires:
            by_user: dict[str, set[str]] = {}
            for group in await self.kube.list_groups():
                name = (group.get("metadata") or {}).get("name")
                for member in group.get("users") or []:
                    by_user.setdefault(member, set()).add(name)
            self._by_user = by_user
            self._expires = time.monotonic() + self.ttl_s
        return self._by_user.get(user, set())


async def current_user(request: Request) -> User:
    state = request.app.state
    settings: Settings = state.settings
    if settings.auth_mode == "dev":
        groups = split_csv(settings.dev_groups)
        return User(settings.dev_user, frozenset(groups), role_for(groups, settings))
    if settings.auth_mode != "proxy":
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "TH_AUTH_MODE must be 'proxy' or 'dev'")

    secret = request.headers.get("x-tensorhelm-proxy-secret", "")
    if not settings.proxy_shared_secret or not hmac.compare_digest(secret, settings.proxy_shared_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in through the Tensorhelm portal")
    name = request.headers.get("x-forwarded-user")
    if not name:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No signed-in user")
    groups = await state.groups.groups_for(name)
    return User(name, frozenset(groups), role_for(groups, settings))


def require(role: str):
    async def dependency(user: User = Depends(current_user)) -> User:
        if not user.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"This action needs the {role} role")
        return user

    return dependency
