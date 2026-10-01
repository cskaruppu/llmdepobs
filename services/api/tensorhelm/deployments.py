"""Turns a deployment request into manifests and delivers them, directly or through GitOps."""

import logging
from dataclasses import dataclass

from tensorhelm.config import Settings
from tensorhelm.fit import FitRequest, ModelShape
from tensorhelm.gitops import GitLabClient
from tensorhelm.placement import PlacementRequest, evaluate
from tensorhelm.render import DeploySpec, render, to_yaml
from tensorhelm.schemas import DeploymentCreate, PlanRequest

log = logging.getLogger(__name__)


class DeploymentError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def placement_request(body: PlanRequest, shape: ModelShape, gpu_utilization: float) -> PlacementRequest:
    return PlacementRequest(
        shape=shape,
        fit=FitRequest(
            precision=body.precision,
            max_model_len=body.max_model_len,
            concurrency=body.concurrency,
            gpu_memory_utilization=gpu_utilization,
        ),
        min_replicas=body.min_replicas,
        gpu_product=body.gpu_product,
        required_labels=body.required_labels,
    )


def default_vllm_runtime(image: str) -> dict:
    """A vLLM ServingRuntime equivalent to the OpenShift AI template, for clusters that do not report one."""
    return {
        "name": "vllm-runtime",
        "template": None,
        "object": {
            "apiVersion": "serving.kserve.io/v1alpha1",
            "kind": "ServingRuntime",
            "metadata": {
                "name": "vllm-runtime",
                "annotations": {
                    "openshift.io/display-name": "vLLM",
                    "opendatahub.io/recommended-accelerators": '["nvidia.com/gpu"]',
                },
            },
            "spec": {
                "annotations": {"prometheus.io/port": "8080", "prometheus.io/path": "/metrics"},
                "multiModel": False,
                "supportedModelFormats": [{"autoSelect": True, "name": "vLLM"}],
                "containers": [
                    {
                        "name": "kserve-container",
                        "image": image,
                        "command": ["python", "-m", "vllm.entrypoints.openai.api_server"],
                        "args": ["--port=8080", "--model=/mnt/models", "--served-model-name={{.Name}}"],
                        "env": [{"name": "HF_HOME", "value": "/tmp/hf_home"}],
                        "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                    }
                ],
            },
        },
    }


def choose_runtime(cluster: dict, runtimes: list[dict], requested: str | None, settings: Settings) -> dict:
    if requested:
        for runtime in runtimes:
            if requested in (runtime.get("name"), runtime.get("template")):
                return runtime
        raise DeploymentError(f"Serving runtime {requested!r} is not available on {cluster['name']}")
    vllm = [r for r in runtimes if "vllm" in (r.get("name") or "").lower() and r.get("object")]
    # Prefer the CUDA build of vLLM when OpenShift AI offers several (CUDA, ROCm, Gaudi, CPU).
    vllm.sort(key=lambda r: ("cuda" not in (r.get("name") or "").lower(), r.get("name") or ""))
    if vllm:
        return vllm[0]
    if settings.vllm_image:
        return default_vllm_runtime(settings.vllm_image)
    raise DeploymentError(
        f"No vLLM serving runtime found on {cluster['name']}. Check that OpenShift AI is installed there, "
        "or set TH_VLLM_IMAGE."
    )


@dataclass
class Prepared:
    spec: DeploySpec
    manifests: dict[str, str]
    placement: dict


def prepare(
    body: DeploymentCreate,
    cluster: dict,
    runtimes: list[dict],
    shape: ModelShape | None,
    storage_uri: str,
    settings: Settings,
) -> Prepared:
    if shape is None and body.tensor_parallel is None:
        raise DeploymentError("Model size is unknown; set tensor_parallel explicitly", 422)
    if shape is not None:
        placement = evaluate(cluster, placement_request(body, shape, settings.gpu_memory_utilization))
        if placement["result"] not in ("fits", "fits_with_changes"):
            raise DeploymentError(f"{cluster['name']}: {placement['reason']}")
        tensor_parallel = body.tensor_parallel or placement["tensor_parallel"]
        gpu_product = placement["gpu_product"]
        weights_gb = placement["estimate"]["weights_gb"]
    else:
        placement = {"result": "unchecked", "reason": "Model size unknown; capacity not checked"}
        tensor_parallel = body.tensor_parallel
        gpu_product = None
        weights_gb = 16.0
    spec = DeploySpec(
        name=body.name,
        namespace=body.namespace or f"team-{body.team}",
        team=body.team,
        model_id=body.model_id,
        storage_uri=storage_uri,
        runtime=choose_runtime(cluster, runtimes, body.runtime, settings),
        tensor_parallel=tensor_parallel,
        max_model_len=body.max_model_len,
        precision=body.precision,
        min_replicas=body.min_replicas,
        max_replicas=max(body.max_replicas, body.min_replicas),
        gpu_product=gpu_product,
        gpu_memory_utilization=settings.gpu_memory_utilization,
        prefix_caching=body.prefix_caching,
        weights_gb=weights_gb,
        extra_args=body.extra_args,
    )
    objects = render(spec)
    return Prepared(spec=spec, manifests=to_yaml(objects), placement=placement)


def git_paths(settings: Settings, cluster: str, team: str, name: str, files: list[str]) -> dict[str, str]:
    """deployments/<cluster>/<team>/namespace.yaml is shared by the team; everything else sits in the
    deployment's own folder, so deleting a deployment never touches another one."""
    team_dir = f"{settings.gitops_path_prefix}/{cluster}/{team}"
    return {f: f"{team_dir}/{f}" if f == "namespace.yaml" else f"{team_dir}/{name}/{f}" for f in files}


async def deliver_gitops(
    gitlab: GitLabClient, settings: Settings, prepared: Prepared, cluster: str, actor: str
) -> tuple[str, list[str]]:
    spec = prepared.spec
    paths = git_paths(settings, cluster, spec.team, spec.name, list(prepared.manifests))
    files = {paths[f]: content for f, content in prepared.manifests.items()}
    sha = await gitlab.commit(
        files,
        f"Deploy {spec.name} ({spec.model_id}) to {cluster} for {spec.team}\n\nRequested by {actor} via Tensorhelm",
    )
    return sha, list(files)


async def deliver_direct(kube, prepared: Prepared) -> None:
    import yaml

    for name in ("namespace.yaml", "servingruntime.yaml", "inferenceservice.yaml"):
        await kube.apply(yaml.safe_load(prepared.manifests[name]))
