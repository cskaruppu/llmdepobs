from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tensorhelm import __version__
from tensorhelm.auth import User, current_user, require
from tensorhelm.deps import cluster_views, get_session
from tensorhelm.models import AuditEvent, Deployment

router = APIRouter(prefix="/api/v1")


@router.get("/health/live")
async def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "database": "ok",
        "kubernetes": "ok" if request.app.state.kube is not None else "disabled",
        "version": __version__,
    }


@router.get("/me")
async def me(request: Request, user: User = Depends(current_user)) -> dict:
    settings = request.app.state.settings
    return {
        "name": user.name,
        "role": user.role,
        "groups": sorted(user.groups),
        "hub": settings.hub_cluster_name,
        "delivery_mode": "gitops" if settings.gitops_enabled else "direct",
        "version": __version__,
    }


def _alerts(clusters: list[dict], deployments: list[Deployment]) -> list[dict]:
    alerts = []
    for c in clusters:
        if c["status"] == "offline":
            alerts.append(
                {
                    "severity": "critical",
                    "title": f"Agent offline on {c['name']}",
                    "detail": f"Last report {c['agent']['seconds_ago']} s ago",
                }
            )
        elif c["status"] == "pending":
            alerts.append(
                {
                    "severity": "warning",
                    "title": f"{c['name']} is waiting for approval",
                    "detail": "A new agent registered; approve it on the Clusters page",
                }
            )
        elif c["status"] == "unreachable":
            alerts.append(
                {
                    "severity": "warning",
                    "title": f"{c['name']} is not available in RHACM",
                    "detail": "Check the klusterlet on that cluster",
                }
            )
        for node in c["nodes"]:
            if not node["ready"]:
                alerts.append(
                    {
                        "severity": "critical",
                        "title": f"GPU node {node['name']} not ready",
                        "detail": f"{c['name']} · {node['gpu_allocatable']}× {node['gpu_product']}",
                    }
                )
            elif not node["schedulable"]:
                alerts.append(
                    {"severity": "warning", "title": f"GPU node {node['name']} cordoned", "detail": c["name"]}
                )
        for ep in c["endpoints"]:
            if not ep["ready"]:
                alerts.append(
                    {
                        "severity": "warning",
                        "title": f"Endpoint {ep['name']} not ready",
                        "detail": f"{c['name']} · {ep['namespace']}",
                    }
                )
    for d in deployments:
        if d.status == "failed":
            alerts.append({"severity": "critical", "title": f"Deployment {d.name} failed", "detail": d.message[:200]})
    return alerts


@router.get("/overview")
async def overview(
    request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)
) -> dict:
    clusters = await cluster_views(request, session)
    deployments = (await session.execute(select(Deployment))).scalars().all()
    endpoints = [{**ep, "cluster": c["name"]} for c in clusters for ep in c["endpoints"]]
    endpoints.sort(key=lambda e: (e["ready"], e["name"]))
    return {
        "kpis": {
            "clusters": len(clusters),
            "clusters_healthy": sum(c["status"] in ("active", "managed") for c in clusters),
            "gpus_total": sum(c["gpu_total"] for c in clusters),
            "gpus_used": sum(c["gpu_used"] for c in clusters),
            "gpu_nodes_not_ready": sum(not n["ready"] or not n["schedulable"] for c in clusters for n in c["nodes"]),
            "endpoints": len(endpoints),
            "endpoints_ready": sum(e["ready"] for e in endpoints),
            "deployments": len(deployments),
        },
        "clusters": clusters,
        "endpoints": endpoints,
        "alerts": _alerts(clusters, deployments),
    }


@router.get("/gpus")
async def gpus(request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)) -> list:
    clusters = await cluster_views(request, session)
    return [{**n, "cluster": c["name"], "cluster_status": c["status"]} for c in clusters for n in c["nodes"]]


@router.get("/audit")
async def audit(
    limit: int = 200, session: AsyncSession = Depends(get_session), _: User = Depends(require("viewer"))
) -> list:
    rows = (await session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(min(limit, 1000)))).scalars()
    return [
        {
            "id": r.id,
            "at": r.at.isoformat(),
            "actor": r.actor,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
        }
        for r in rows
    ]
