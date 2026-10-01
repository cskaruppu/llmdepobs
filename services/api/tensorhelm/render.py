"""Renders the Kubernetes objects for one model deployment: a Namespace, the vLLM ServingRuntime copied
from the OpenShift AI template, and a KServe InferenceService in RawDeployment mode."""

import copy
import math
import re
from dataclasses import dataclass, field

import yaml

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")
STRIP_METADATA = ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation", "ownerReferences")


class RenderError(ValueError):
    pass


def validate_name(value: str, what: str) -> str:
    if not NAME_RE.match(value):
        raise RenderError(f"{what} must be lowercase letters, digits and dashes, at most 40 characters: {value!r}")
    return value


@dataclass
class DeploySpec:
    name: str
    namespace: str
    team: str
    model_id: str
    storage_uri: str
    runtime: dict  # an entry of a cluster's runtimes list, including the ServingRuntime "object"
    tensor_parallel: int
    max_model_len: int
    precision: str = "bf16"
    min_replicas: int = 1
    max_replicas: int = 1
    gpu_product: str | None = None
    gpu_memory_utilization: float = 0.90
    prefix_caching: bool = True
    weights_gb: float = 16.0
    extra_args: list[str] = field(default_factory=list)


def vllm_args(spec: DeploySpec) -> list[str]:
    args = [
        f"--tensor-parallel-size={spec.tensor_parallel}",
        f"--max-model-len={spec.max_model_len}",
        f"--gpu-memory-utilization={spec.gpu_memory_utilization:.2f}",
    ]
    if spec.precision == "fp8":
        args += ["--quantization=fp8", "--kv-cache-dtype=fp8"]
    if spec.prefix_caching:
        args.append("--enable-prefix-caching")
    return args + list(spec.extra_args)


def serving_runtime(spec: DeploySpec) -> dict:
    obj = copy.deepcopy(spec.runtime["object"])
    meta = obj.setdefault("metadata", {})
    for key in STRIP_METADATA:
        meta.pop(key, None)
    obj.pop("status", None)
    # One runtime per deployment, named after it, as the OpenShift AI dashboard does. This keeps every
    # GitOps file owned by exactly one deployment.
    meta["name"] = spec.name
    meta["namespace"] = spec.namespace
    meta.setdefault("labels", {})["opendatahub.io/dashboard"] = "true"
    meta.setdefault("annotations", {})["opendatahub.io/template-name"] = (
        spec.runtime.get("template") or spec.runtime["name"]
    )
    obj.setdefault("apiVersion", "serving.kserve.io/v1alpha1")
    obj.setdefault("kind", "ServingRuntime")
    return obj


def inference_service(spec: DeploySpec) -> dict:
    memory_gi = max(24, math.ceil(spec.weights_gb * 1.5))
    predictor: dict = {
        "minReplicas": spec.min_replicas,
        "maxReplicas": spec.max_replicas,
        "model": {
            "modelFormat": {"name": "vLLM"},
            "runtime": spec.name,
            "storageUri": spec.storage_uri,
            "args": vllm_args(spec),
            "resources": {
                "requests": {"cpu": "4", "memory": f"{memory_gi}Gi", "nvidia.com/gpu": str(spec.tensor_parallel)},
                "limits": {"cpu": "8", "memory": f"{memory_gi * 2}Gi", "nvidia.com/gpu": str(spec.tensor_parallel)},
            },
        },
        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
    }
    if spec.gpu_product:
        # Node labels use dashes where the inventory shows spaces.
        predictor["nodeSelector"] = {"nvidia.com/gpu.product": spec.gpu_product.replace(" ", "-")}
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {
            "name": spec.name,
            "namespace": spec.namespace,
            "labels": {
                "opendatahub.io/dashboard": "true",
                "networking.kserve.io/visibility": "exposed",
                "tensorhelm.io/managed": "true",
                "tensorhelm.io/team": spec.team,
            },
            "annotations": {
                "serving.kserve.io/deploymentMode": "RawDeployment",
                "openshift.io/display-name": spec.name,
                "tensorhelm.io/model": spec.model_id,
            },
        },
        "spec": {"predictor": predictor},
    }


def namespace(spec: DeploySpec) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": spec.namespace,
            "labels": {
                "opendatahub.io/dashboard": "true",
                "modelmesh-enabled": "false",
                "tensorhelm.io/team": spec.team,
                # Lets the default OpenShift GitOps Argo CD instance manage this namespace.
                "argocd.argoproj.io/managed-by": "openshift-gitops",
            },
        },
    }


def render(spec: DeploySpec) -> dict[str, dict]:
    validate_name(spec.name, "Deployment name")
    validate_name(spec.namespace, "Namespace")
    validate_name(spec.team, "Team")
    if not spec.storage_uri:
        raise RenderError("A storage URI is required (hf://, oci://, s3:// or pvc://)")
    return {
        "namespace.yaml": namespace(spec),
        "servingruntime.yaml": serving_runtime(spec),
        "inferenceservice.yaml": inference_service(spec),
    }


def to_yaml(objects: dict[str, dict]) -> dict[str, str]:
    return {name: yaml.safe_dump(obj, sort_keys=False) for name, obj in objects.items()}
