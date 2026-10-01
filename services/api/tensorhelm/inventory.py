"""Collects a cluster's GPU nodes, model endpoints and serving runtimes. The hub runs this against itself;
spoke agents run the same code and send the result to the hub."""

import logging
from collections import defaultdict
from typing import Any, Protocol

log = logging.getLogger(__name__)

GPU_RESOURCE = "nvidia.com/gpu"


class InventorySource(Protocol):
    async def list_nodes(self) -> list[dict]: ...
    async def list_active_pods(self) -> list[dict]: ...
    async def list_inferenceservices(self) -> list[dict]: ...
    async def list_runtime_templates(self, namespace: str) -> list[dict]: ...
    async def list_datascienceclusters(self) -> list[dict]: ...
    async def get_cluster_version(self) -> dict | None: ...
    async def get_infrastructure(self) -> dict | None: ...


def parse_cpu(value: str | int | float | None) -> float:
    if value is None:
        return 0.0
    text = str(value)
    if text.endswith("m"):
        return float(text[:-1]) / 1000
    return float(text)


def parse_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def gpu_requests_by_node(pods: list[dict]) -> dict[str, int]:
    used: dict[str, int] = defaultdict(int)
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if not node:
            continue
        for container in (pod.get("spec") or {}).get("containers") or []:
            resources = container.get("resources") or {}
            amount = (resources.get("limits") or {}).get(GPU_RESOURCE) or (resources.get("requests") or {}).get(
                GPU_RESOURCE
            )
            used[node] += parse_int(amount)
    return dict(used)


def _driver_version(labels: dict[str, str]) -> str | None:
    if labels.get("nvidia.com/cuda.driver-version.full"):
        return labels["nvidia.com/cuda.driver-version.full"]
    parts = [labels.get(f"nvidia.com/cuda.driver.{p}") for p in ("major", "minor", "rev")]
    return ".".join(p for p in parts if p) or None


def _cuda_version(labels: dict[str, str]) -> str | None:
    if labels.get("nvidia.com/cuda.runtime-version.full"):
        return labels["nvidia.com/cuda.runtime-version.full"]
    parts = [labels.get(f"nvidia.com/cuda.runtime.{p}") for p in ("major", "minor")]
    return ".".join(p for p in parts if p) or None


def parse_gpu_node(node: dict, used: int) -> dict | None:
    """Turn a Node into a GPU inventory entry, or None when the node has no NVIDIA GPUs."""
    meta = node.get("metadata") or {}
    labels = meta.get("labels") or {}
    status = node.get("status") or {}
    capacity = parse_int((status.get("capacity") or {}).get(GPU_RESOURCE))
    allocatable = parse_int((status.get("allocatable") or {}).get(GPU_RESOURCE))
    if capacity == 0 and labels.get("nvidia.com/gpu.present") != "true":
        return None
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
    return {
        "name": meta.get("name"),
        "gpu_product": labels.get("nvidia.com/gpu.product", "unknown").replace("-", " "),
        "gpu_memory_mib": parse_int(labels.get("nvidia.com/gpu.memory")),
        "gpu_capacity": capacity,
        "gpu_allocatable": allocatable,
        "gpu_used": min(used, allocatable) if allocatable else used,
        "mig_strategy": labels.get("nvidia.com/mig.strategy"),
        "driver_version": _driver_version(labels),
        "cuda_version": _cuda_version(labels),
        "pool": labels.get("tensorhelm.io/pool"),
        "ready": ready,
        "schedulable": not (node.get("spec") or {}).get("unschedulable", False),
    }


def parse_endpoint(isvc: dict) -> dict:
    meta = isvc.get("metadata") or {}
    spec = (isvc.get("spec") or {}).get("predictor") or {}
    model = spec.get("model") or {}
    resources = model.get("resources") or {}
    status = isvc.get("status") or {}
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
    labels = meta.get("labels") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "ready": ready,
        "url": status.get("url"),
        "runtime": model.get("runtime"),
        "model_format": (model.get("modelFormat") or {}).get("name"),
        "storage_uri": model.get("storageUri"),
        "gpus": parse_int((resources.get("limits") or {}).get(GPU_RESOURCE)),
        "min_replicas": spec.get("minReplicas"),
        "max_replicas": spec.get("maxReplicas"),
        "managed": labels.get("tensorhelm.io/managed") == "true",
        "team": labels.get("tensorhelm.io/team"),
    }


def parse_runtime_template(template: dict) -> dict | None:
    """RHOAI ships serving runtimes as OpenShift Templates wrapping one ServingRuntime object."""
    objects = template.get("objects") or []
    runtime = next((o for o in objects if o.get("kind") == "ServingRuntime"), None)
    if runtime is None:
        return None
    meta = template.get("metadata") or {}
    annotations = (runtime.get("metadata") or {}).get("annotations") or {}
    containers = (runtime.get("spec") or {}).get("containers") or []
    formats = [f.get("name") for f in (runtime.get("spec") or {}).get("supportedModelFormats") or []]
    return {
        "template": meta.get("name"),
        "name": (runtime.get("metadata") or {}).get("name") or meta.get("name"),
        "display_name": annotations.get("openshift.io/display-name") or meta.get("name"),
        "image": containers[0].get("image") if containers else None,
        "formats": [f for f in formats if f],
        "accelerators": annotations.get("opendatahub.io/recommended-accelerators"),
        "object": runtime,
    }


async def collect(source: InventorySource, rhoai_namespace: str) -> dict:
    """Return {facts, nodes, endpoints, runtimes} for the cluster `source` points at."""
    nodes = await source.list_nodes()
    used = gpu_requests_by_node(await source.list_active_pods())
    gpu_nodes = [n for n in (parse_gpu_node(node, used.get(node["metadata"]["name"], 0)) for node in nodes) if n]

    cpu_total = sum(parse_cpu((n.get("status") or {}).get("allocatable", {}).get("cpu")) for n in nodes)
    facts: dict[str, Any] = {
        "node_count": len(nodes),
        "cpu_allocatable": round(cpu_total, 1),
        "gpu_total": sum(n["gpu_allocatable"] for n in gpu_nodes),
        "gpu_used": sum(n["gpu_used"] for n in gpu_nodes),
    }

    version = await source.get_cluster_version()
    if version:
        facts["openshift_version"] = ((version.get("status") or {}).get("desired") or {}).get("version")
        facts["cluster_id"] = (version.get("spec") or {}).get("clusterID")
    infra = await source.get_infrastructure()
    if infra:
        infra_status = infra.get("status") or {}
        facts["api_url"] = infra_status.get("apiServerURL")
        facts["platform"] = (infra_status.get("platformStatus") or {}).get("type")
    dscs = await source.list_datascienceclusters()
    if dscs:
        release = (dscs[0].get("status") or {}).get("release") or {}
        facts["rhoai_version"] = release.get("version") or "installed"

    endpoints = [parse_endpoint(i) for i in await source.list_inferenceservices()]
    runtimes = [
        r for r in (parse_runtime_template(t) for t in await source.list_runtime_templates(rhoai_namespace)) if r
    ]
    return {"facts": facts, "nodes": gpu_nodes, "endpoints": endpoints, "runtimes": runtimes}
