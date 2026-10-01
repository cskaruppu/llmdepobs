import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tensorhelm import audit
from tensorhelm.auth import User, current_user, require
from tensorhelm.clusters import cluster_view, hash_token, new_token, parse_discovered_cluster
from tensorhelm.deps import cluster_views, get_session
from tensorhelm.models import Cluster, utcnow
from tensorhelm.schemas import AgentRegister, AgentReport

router = APIRouter(prefix="/api/v1")


@router.get("/clusters")
async def list_clusters(
    request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)
) -> list:
    return await cluster_views(request, session)


@router.get("/clusters/discovered")
async def discovered_clusters(request: Request, _: User = Depends(require("admin"))) -> dict:
    """Clusters RHACM discovered (for example from console.redhat.com) but that are not imported yet."""
    kube = request.app.state.kube
    if kube is None:
        return {"available": False, "clusters": []}
    clusters = [parse_discovered_cluster(dc) for dc in await kube.list_discovered_clusters()]
    return {"available": True, "clusters": [c for c in clusters if not c["is_managed"]]}


@router.get("/clusters/connect-info")
async def connect_info(request: Request, _: User = Depends(require("admin"))) -> dict:
    settings = request.app.state.settings
    hub_url = settings.agent_public_url or "<agent route URL>"
    return {
        "agent_url": hub_url,
        "registration_enabled": bool(settings.agent_bootstrap_token),
        "hub": settings.hub_cluster_name,
        "script": (
            f"./scripts/register-cluster.sh --name <cluster-name> --spoke-context <kube-context> --hub-url {hub_url}"
        ),
        "manual": (
            "oc --context <spoke> process -f deploy/openshift/agent-template.yaml "
            f"-p HUB_URL={hub_url} -p CLUSTER_NAME=<cluster-name> "
            '-p BOOTSTRAP_TOKEN="$(oc get secret tensorhelm-agent-bootstrap -n tensorhelm '
            "-o jsonpath='{.data.token}' | base64 -d)\" | oc --context <spoke> apply -f -"
        ),
    }


@router.get("/clusters/{name}")
async def get_cluster(
    name: str, request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)
) -> dict:
    cluster = await session.get(Cluster, name)
    if cluster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No cluster named {name}")
    return cluster_view(cluster, utcnow(), request.app.state.settings.agent_offline_after_s)


@router.post("/clusters/{name}/approve")
async def approve_cluster(
    name: str, session: AsyncSession = Depends(get_session), user: User = Depends(require("admin"))
) -> dict:
    cluster = await session.get(Cluster, name)
    if cluster is None or not cluster.agent_token_hash:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No agent registration for {name}")
    cluster.approved = True
    audit.record(session, user.name, "cluster.approved", name)
    await session.commit()
    return {"name": name, "approved": True}


@router.delete("/clusters/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_cluster(
    name: str, request: Request, session: AsyncSession = Depends(get_session), user: User = Depends(require("admin"))
) -> None:
    cluster = await session.get(Cluster, name)
    if cluster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No cluster named {name}")
    if cluster.source == "hub":
        raise HTTPException(status.HTTP_409_CONFLICT, "The hub cluster cannot be removed")
    if cluster.acm:
        # Keep the RHACM view; only forget the agent so it can register again.
        cluster.agent_token_hash = None
        cluster.approved = False
        cluster.nodes, cluster.endpoints, cluster.runtimes, cluster.labels = [], [], [], {}
    else:
        await session.delete(cluster)
    audit.record(session, user.name, "cluster.removed", name)
    await session.commit()


# ---- agent API: called by spoke agents, authenticated with tokens instead of user sign-in ---------------


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    return authorization.split(" ", 1)[1].strip()


@router.post("/agent/register", status_code=status.HTTP_201_CREATED)
async def agent_register(
    body: AgentRegister,
    request: Request,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = request.app.state.settings
    token = _bearer(authorization)
    if not settings.agent_bootstrap_token or not hmac.compare_digest(token, settings.agent_bootstrap_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bootstrap token")
    if body.cluster_name == settings.hub_cluster_name:
        raise HTTPException(status.HTTP_409_CONFLICT, "That name belongs to the hub cluster")
    cluster = await session.get(Cluster, body.cluster_name)
    if cluster is not None and cluster.agent_token_hash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{body.cluster_name} already has an agent. Remove it on the Clusters page to register again.",
        )
    agent_token = new_token()
    if cluster is None:
        cluster = Cluster(name=body.cluster_name, source="agent")
        session.add(cluster)
    cluster.agent_token_hash = hash_token(agent_token)
    cluster.approved = False
    cluster.agent_version = body.agent_version
    cluster.labels = body.labels
    cluster.facts = body.facts
    cluster.last_seen = utcnow()
    audit.record(
        session,
        f"agent:{body.cluster_name}",
        "cluster.registered",
        body.cluster_name,
        api_url=body.facts.get("api_url"),
        cluster_id=body.facts.get("cluster_id"),
    )
    await session.commit()
    return {"cluster_name": body.cluster_name, "token": agent_token, "approved": False}


@router.post("/agent/report")
async def agent_report(
    body: AgentReport, authorization: str | None = Header(None), session: AsyncSession = Depends(get_session)
) -> dict:
    token = _bearer(authorization)
    cluster = await session.get(Cluster, body.cluster_name)
    if (
        cluster is None
        or not cluster.agent_token_hash
        or not hmac.compare_digest(cluster.agent_token_hash, hash_token(token))
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown cluster or token")
    cluster.agent_version = body.agent_version
    cluster.labels = body.labels
    cluster.facts = body.facts
    cluster.nodes = body.nodes
    cluster.endpoints = body.endpoints
    cluster.runtimes = body.runtimes
    cluster.last_seen = utcnow()
    await session.commit()
    return {"approved": cluster.approved}
