"""Fills a local development hub with three example spoke clusters, as if their agents had reported.

TH_AUTH_MODE=dev TH_KUBE_ENABLED=false TH_AGENT_BOOTSTRAP_TOKEN=dev-boot tensorhelm-api &
python scripts/dev_seed.py http://localhost:8000 dev-boot
"""

import sys

import httpx

VLLM = {
    "template": "vllm-cuda-runtime-template",
    "name": "vllm-cuda-runtime",
    "display_name": "vLLM NVIDIA GPU ServingRuntime for KServe",
    "image": "quay.io/modh/vllm:example",
    "formats": ["vLLM"],
    "accelerators": '["nvidia.com/gpu"]',
    "object": {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "ServingRuntime",
        "metadata": {"name": "vllm-cuda-runtime"},
        "spec": {
            "supportedModelFormats": [{"name": "vLLM", "autoSelect": True}],
            "containers": [{"name": "kserve-container", "image": "quay.io/modh/vllm:example"}],
        },
    },
}


def node(name, product, count, used, memory_mib, ready=True, schedulable=True):
    return {
        "name": name,
        "gpu_product": product,
        "gpu_memory_mib": memory_mib,
        "gpu_capacity": count,
        "gpu_allocatable": count,
        "gpu_used": used,
        "mig_strategy": None,
        "driver_version": "550.90.07",
        "cuda_version": "12.4",
        "pool": None,
        "ready": ready,
        "schedulable": schedulable,
    }


def endpoint(name, namespace, gpus, ready=True):
    return {
        "name": name,
        "namespace": namespace,
        "ready": ready,
        "url": f"https://{name}-{namespace}.apps.example.com",
        "runtime": name,
        "model_format": "vLLM",
        "storage_uri": "hf://example",
        "gpus": gpus,
        "min_replicas": 1,
        "max_replicas": 2,
        "managed": True,
        "team": namespace.removeprefix("team-"),
    }


CLUSTERS = [
    {
        "cluster_name": "prod-east",
        "labels": {"region": "eu", "tensorhelm.io/role": "serving"},
        "facts": {
            "openshift_version": "4.18.5",
            "rhoai_version": "2.22.0",
            "platform": "BareMetal",
            "node_count": 8,
            "cpu_allocatable": 512.0,
            "api_url": "https://api.prod-east.example.com:6443",
        },
        "nodes": [
            node("gpu-e-01", "NVIDIA H100 80GB HBM3", 8, 6, 81559),
            node("gpu-e-02", "NVIDIA H100 80GB HBM3", 8, 5, 81559),
        ],
        "endpoints": [endpoint("contracts-rag", "team-legal", 4), endpoint("code-assist", "team-eng", 2)],
        "runtimes": [VLLM],
    },
    {
        "cluster_name": "prod-west",
        "labels": {"region": "eu", "tensorhelm.io/role": "serving"},
        "facts": {
            "openshift_version": "4.17.12",
            "rhoai_version": "2.19.1",
            "platform": "VSphere",
            "node_count": 10,
            "cpu_allocatable": 640.0,
            "api_url": "https://api.prod-west.example.com:6443",
        },
        "nodes": [
            node("gpu-w-11", "NVIDIA L40S", 8, 6, 46068),
            node("gpu-w-12", "NVIDIA L40S", 8, 4, 46068),
            node("gpu-w-13", "NVIDIA L40S", 8, 0, 46068, schedulable=False),
        ],
        "endpoints": [endpoint("support-copilot", "team-support", 2, ready=False)],
        "runtimes": [VLLM],
    },
    {
        "cluster_name": "cpu-edge",
        "labels": {"region": "eu"},
        "facts": {"openshift_version": "4.18.5", "platform": "BareMetal", "node_count": 6, "cpu_allocatable": 192.0},
        "nodes": [],
        "endpoints": [],
        "runtimes": [],
    },
]


def main(base: str, bootstrap: str) -> None:
    with httpx.Client(base_url=base, timeout=10) as client:
        for cluster in CLUSTERS:
            reg = client.post(
                "/api/v1/agent/register",
                headers={"Authorization": f"Bearer {bootstrap}"},
                json={
                    "cluster_name": cluster["cluster_name"],
                    "agent_version": "0.1.0",
                    "labels": cluster["labels"],
                    "facts": cluster["facts"],
                },
            )
            reg.raise_for_status()
            token = reg.json()["token"]
            if cluster["cluster_name"] != "cpu-edge":
                client.post(f"/api/v1/clusters/{cluster['cluster_name']}/approve").raise_for_status()
            client.post(
                "/api/v1/agent/report",
                headers={"Authorization": f"Bearer {token}"},
                json={**cluster, "agent_version": "0.1.0"},
            ).raise_for_status()
            print("seeded", cluster["cluster_name"])


if __name__ == "__main__":
    main(
        sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000", sys.argv[2] if len(sys.argv) > 2 else "dev-boot"
    )
