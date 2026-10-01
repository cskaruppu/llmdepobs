"""Cluster records: status rules, RHACM ManagedCluster sync, agent tokens and the API view."""

import hashlib
import secrets
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tensorhelm.models import Cluster, as_utc, utcnow

ACM_AVAILABLE = "ManagedClusterConditionAvailable"
ACM_JOINED = "ManagedClusterJoined"


def new_token() -> str:
    return "tha_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def effective_labels(cluster: Cluster) -> dict[str, str]:
    """RHACM labels win over agent labels: RHACM is where admins set placement labels."""
    return {**(cluster.labels or {}), **((cluster.acm or {}).get("labels") or {})}


def cluster_status(cluster: Cluster, now: datetime, offline_after_s: int) -> str:
    if cluster.source == "hub":
        return "active"
    if cluster.agent_token_hash:
        if not cluster.approved:
            return "pending"
        last_seen = as_utc(cluster.last_seen)
        if last_seen and (now - last_seen).total_seconds() <= offline_after_s:
            return "active"
        return "offline"
    acm = cluster.acm or {}
    if acm.get("available"):
        return "managed"
    return "unreachable"


def cluster_type(cluster: Cluster) -> str:
    labels = effective_labels(cluster)
    if cluster.source == "hub" and not cluster.nodes:
        return "hub"
    if labels.get("tensorhelm.io/burst") == "true":
        return "burst"
    if cluster.nodes:
        return "gpu"
    if (cluster.facts or {}).get("node_count"):
        return "cpu"
    return "unknown"


def gpu_summary(nodes: list[dict]) -> list[dict]:
    counts: Counter[str] = Counter()
    memory: dict[str, int] = {}
    for node in nodes:
        counts[node["gpu_product"]] += node["gpu_allocatable"]
        memory[node["gpu_product"]] = node.get("gpu_memory_mib") or 0
    return [
        {"product": p, "count": c, "memory_gb": round(memory[p] * 1024 * 1024 / 1e9) if memory[p] else None}
        for p, c in counts.most_common()
    ]


def cluster_view(cluster: Cluster, now: datetime, offline_after_s: int) -> dict:
    labels = effective_labels(cluster)
    last_seen = as_utc(cluster.last_seen)
    facts = cluster.facts or {}
    acm = cluster.acm or {}
    return {
        "name": cluster.name,
        "source": cluster.source,
        "status": cluster_status(cluster, now, offline_after_s),
        "type": cluster_type(cluster),
        "approved": cluster.approved,
        "labels": labels,
        "region": labels.get("tensorhelm.io/region") or labels.get("region") or acm.get("region"),
        "facts": facts,
        "gpu_total": sum(n["gpu_allocatable"] for n in cluster.nodes or []),
        "gpu_used": sum(n["gpu_used"] for n in cluster.nodes or []),
        "gpus": gpu_summary(cluster.nodes or []),
        "nodes": cluster.nodes or [],
        "endpoints": cluster.endpoints or [],
        "runtimes": [
            {k: r.get(k) for k in ("name", "display_name", "image", "formats")} for r in cluster.runtimes or []
        ],
        "agent": {
            "installed": bool(cluster.agent_token_hash) or cluster.source == "hub",
            "version": cluster.agent_version,
            "last_seen": last_seen.isoformat() if last_seen else None,
            "seconds_ago": int((now - last_seen).total_seconds()) if last_seen else None,
        },
        "acm": {
            "managed": bool(acm),
            "available": acm.get("available"),
            "joined": acm.get("joined"),
            "openshift_version": acm.get("openshift_version"),
            "api_url": acm.get("api_url"),
        },
    }


def _condition(conditions: list[dict], kind: str) -> bool | None:
    for condition in conditions:
        if condition.get("type") == kind:
            return condition.get("status") == "True"
    return None


def parse_managed_cluster(mc: dict) -> dict:
    meta = mc.get("metadata") or {}
    status = mc.get("status") or {}
    claims = {c.get("name"): c.get("value") for c in status.get("clusterClaims") or []}
    conditions = status.get("conditions") or []
    configs = (mc.get("spec") or {}).get("managedClusterClientConfigs") or []
    return {
        "name": meta.get("name"),
        "labels": meta.get("labels") or {},
        "available": _condition(conditions, ACM_AVAILABLE),
        "joined": _condition(conditions, ACM_JOINED),
        "api_url": configs[0].get("url") if configs else None,
        "openshift_version": claims.get("version.openshift.io"),
        "region": claims.get("region.open-cluster-management.io"),
        "platform": claims.get("platform.open-cluster-management.io"),
    }


def parse_discovered_cluster(dc: dict) -> dict:
    spec = dc.get("spec") or {}
    return {
        "name": spec.get("displayName") or (dc.get("metadata") or {}).get("name"),
        "api_url": spec.get("apiUrl"),
        "openshift_version": spec.get("openshiftVersion"),
        "cloud_provider": spec.get("cloudProvider"),
        "type": spec.get("type"),
        "is_managed": bool(spec.get("isManagedCluster")),
        "console_url": spec.get("console"),
    }


async def ensure_hub(session: AsyncSession, hub_name: str) -> Cluster:
    hub = await session.get(Cluster, hub_name)
    if hub is None:
        hub = Cluster(name=hub_name, source="hub", approved=True)
        session.add(hub)
        await session.flush()
    return hub


async def store_hub_inventory(session: AsyncSession, hub_name: str, inventory: dict) -> None:
    hub = await ensure_hub(session, hub_name)
    hub.facts = inventory["facts"]
    hub.nodes = inventory["nodes"]
    hub.endpoints = inventory["endpoints"]
    hub.runtimes = inventory["runtimes"]
    hub.last_seen = utcnow()
    await session.commit()


async def sync_managed_clusters(session: AsyncSession, managed: list[dict], hub_name: str) -> None:
    """Mirror RHACM ManagedClusters into cluster records. The hub appears in RHACM as local-cluster."""
    parsed = {m["name"]: m for m in (parse_managed_cluster(mc) for mc in managed) if m["name"]}
    existing = {c.name: c for c in (await session.execute(select(Cluster))).scalars()}
    for name, view in parsed.items():
        cluster = existing.get(name)
        if cluster is None:
            session.add(Cluster(name=name, source="acm", approved=False, acm=view))
        else:
            cluster.acm = view
    for name, cluster in existing.items():
        # Drop clusters that RHACM no longer manages, unless an agent or the hub keeps them alive.
        if cluster.source == "acm" and name not in parsed and not cluster.agent_token_hash:
            await session.delete(cluster)
        elif name not in parsed and cluster.acm and name != hub_name:
            cluster.acm = {}
    await session.commit()
