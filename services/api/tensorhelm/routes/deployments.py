import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tensorhelm import audit
from tensorhelm.auth import User, current_user, require
from tensorhelm.deployments import (
    DeploymentError,
    deliver_direct,
    deliver_gitops,
    git_paths,
    placement_request,
    prepare,
)
from tensorhelm.deps import cluster_views, get_session
from tensorhelm.gitops import GitLabClient, GitOpsError
from tensorhelm.models import Cluster, Deployment
from tensorhelm.placement import plan
from tensorhelm.schemas import DeploymentCreate, PlanRequest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def _gitlab(request: Request) -> GitLabClient:
    s = request.app.state.settings
    return GitLabClient(request.app.state.http, s.gitlab_url, s.gitlab_project, s.gitlab_token, s.gitlab_branch)


def _view(d: Deployment, clusters: dict[str, dict]) -> dict:
    endpoint = next(
        (
            e
            for e in clusters.get(d.cluster, {}).get("endpoints", [])
            if e["name"] == d.name and e["namespace"] == d.namespace
        ),
        None,
    )
    return {
        "id": d.id,
        "name": d.name,
        "team": d.team,
        "namespace": d.namespace,
        "cluster": d.cluster,
        "model_id": d.model_id,
        "storage_uri": d.storage_uri,
        "precision": d.precision,
        "gpus_per_replica": d.gpus_per_replica,
        "gpu_product": d.gpu_product,
        "min_replicas": d.min_replicas,
        "max_replicas": d.max_replicas,
        "max_model_len": d.max_model_len,
        "delivery": d.delivery,
        "status": d.status,
        "message": d.message,
        "git_commit": d.git_commit,
        "git_paths": d.git_paths,
        "created_by": d.created_by,
        "created_at": d.created_at.isoformat(),
        "live": endpoint,
    }


@router.post("/deployments/plan")
async def plan_deployment(
    body: PlanRequest, request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)
) -> dict:
    shape = await request.app.state.catalog.shape_for(body.model_id)
    if shape is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Cannot read the size of {body.model_id}. Check the model ID, "
            "or set a Hugging Face token for gated models.",
        )
    clusters = await cluster_views(request, session)
    settings = request.app.state.settings
    candidates = plan(clusters, placement_request(body, shape, settings.gpu_memory_utilization))
    return {"model_id": body.model_id, "shape": shape.__dict__, "candidates": candidates}


@router.get("/deployments")
async def list_deployments(
    request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)
) -> list:
    clusters = {c["name"]: c for c in await cluster_views(request, session)}
    rows = (await session.execute(select(Deployment).order_by(Deployment.created_at.desc()))).scalars()
    return [_view(d, clusters) for d in rows]


@router.get("/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: str, request: Request, session: AsyncSession = Depends(get_session), _: User = Depends(current_user)
) -> dict:
    d = await session.get(Deployment, deployment_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such deployment")
    clusters = {c["name"]: c for c in await cluster_views(request, session)}
    return {**_view(d, clusters), "manifests": d.manifests}


@router.post("/deployments", status_code=status.HTTP_201_CREATED)
async def create_deployment(
    body: DeploymentCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require("engineer")),
) -> dict:
    state = request.app.state
    settings = state.settings
    cluster_row = await session.get(Cluster, body.cluster)
    if cluster_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No cluster named {body.cluster}")
    clusters = {c["name"]: c for c in await cluster_views(request, session)}
    cluster = clusters[body.cluster]

    is_hub = cluster["source"] == "hub"
    if settings.gitops_enabled:
        delivery = "gitops"
    elif is_hub and state.kube is not None:
        delivery = "direct"
    else:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Deploying to a remote cluster needs GitOps delivery: set TH_DELIVERY_MODE=gitops and the GitLab settings.",
        )

    curated = next((m for m in state.catalog.curated if m["id"] == body.model_id), None)
    storage_uri = body.storage_uri or (curated or {}).get("storage_uri") or f"hf://{body.model_id}"
    shape = await state.catalog.shape_for(body.model_id)
    try:
        prepared = prepare(body, cluster, cluster_row.runtimes or [], shape, storage_uri, settings)
    except DeploymentError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    spec = prepared.spec
    duplicate = await session.execute(
        select(Deployment).where(
            Deployment.cluster == body.cluster, Deployment.namespace == spec.namespace, Deployment.name == spec.name
        )
    )
    if duplicate.scalars().first():
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{spec.name} already exists in {spec.namespace} on {body.cluster}"
        )

    deployment = Deployment(
        name=spec.name,
        team=spec.team,
        namespace=spec.namespace,
        cluster=body.cluster,
        model_id=spec.model_id,
        storage_uri=spec.storage_uri,
        runtime=spec.runtime["name"],
        precision=spec.precision,
        gpus_per_replica=spec.tensor_parallel,
        gpu_product=spec.gpu_product,
        min_replicas=spec.min_replicas,
        max_replicas=spec.max_replicas,
        max_model_len=spec.max_model_len,
        delivery=delivery,
        manifests=prepared.manifests,
        created_by=user.name,
    )
    try:
        if delivery == "gitops":
            deployment.git_commit, deployment.git_paths = await deliver_gitops(
                _gitlab(request), settings, prepared, body.cluster, user.name
            )
            deployment.status = "committed"
            deployment.message = "Committed to Git; Argo CD applies it on the next sync"
        else:
            await deliver_direct(state.kube, prepared)
            deployment.status = "applied"
            deployment.message = "Applied to the hub cluster"
    except (GitOpsError, httpx.HTTPError) as exc:
        deployment.status, deployment.message = "failed", f"GitOps commit failed: {exc}"
    except Exception as exc:  # Kubernetes API errors carry useful text; keep it for the user.
        log.exception("direct apply failed")
        deployment.status, deployment.message = "failed", f"Apply failed: {exc}"

    target = f"{body.cluster}/{spec.namespace}/{spec.name}"
    if deployment.status == "failed":
        # Keep only the audit trail, so the same name can be retried once the cause is fixed.
        audit.record(session, user.name, "deployment.failed", target, model=spec.model_id, error=deployment.message)
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, deployment.message)

    session.add(deployment)
    audit.record(
        session,
        user.name,
        "deployment.created",
        target,
        model=spec.model_id,
        gpus_per_replica=spec.tensor_parallel,
        delivery=delivery,
        commit=deployment.git_commit,
    )
    await session.commit()
    return {**_view(deployment, clusters), "placement": prepared.placement, "manifests": prepared.manifests}


@router.delete("/deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deployment(
    deployment_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require("engineer")),
) -> None:
    state = request.app.state
    d = await session.get(Deployment, deployment_id)
    if d is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such deployment")
    try:
        if d.delivery == "gitops" and d.status != "failed":
            paths = git_paths(
                state.settings, d.cluster, d.team, d.name, ["servingruntime.yaml", "inferenceservice.yaml"]
            )
            await _gitlab(request).commit(
                {p: None for p in paths.values()},
                f"Remove {d.name} from {d.cluster}\n\nRequested by {user.name} via Tensorhelm",
            )
        elif d.delivery == "direct" and state.kube is not None:
            await state.kube.delete("InferenceService", d.namespace, d.name)
            await state.kube.delete("ServingRuntime", d.namespace, d.name)
    except (GitOpsError, httpx.HTTPError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not remove from Git: {exc}") from exc
    audit.record(session, user.name, "deployment.deleted", f"{d.cluster}/{d.namespace}/{d.name}")
    await session.delete(d)
    await session.commit()
