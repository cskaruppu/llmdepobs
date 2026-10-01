import pytest

from tensorhelm.catalog import load_curated, shape_from_config
from tensorhelm.clusters import parse_managed_cluster
from tensorhelm.fit import FitRequest, ModelShape, estimate
from tensorhelm.inventory import collect, gpu_requests_by_node, parse_cpu, parse_gpu_node
from tensorhelm.placement import PlacementRequest, plan
from tensorhelm.render import DeploySpec, RenderError, render

from .conftest import VLLM_TEMPLATE, FakeKube, gpu_node, gpu_pod

LLAMA_70B = ModelShape(params_b=70.6, layers=80, kv_heads=8, head_dim=128, attention_heads=64)
LLAMA_8B = ModelShape(params_b=8.0, layers=32, kv_heads=8, head_dim=128, attention_heads=32)


def test_parse_cpu():
    assert parse_cpu("95500m") == 95.5
    assert parse_cpu("32") == 32.0


def test_parse_gpu_node_reads_gpu_feature_discovery_labels():
    node = parse_gpu_node(gpu_node("gpu-1"), used=3)
    assert node["gpu_product"] == "NVIDIA H100 80GB HBM3"
    assert node["gpu_allocatable"] == 8
    assert node["gpu_used"] == 3
    assert node["driver_version"] == "550.90.07"
    assert node["ready"] and node["schedulable"]


def test_cpu_nodes_are_not_gpu_nodes():
    assert parse_gpu_node({"metadata": {"name": "w"}, "status": {}}, 0) is None


def test_gpu_requests_sum_per_node():
    assert gpu_requests_by_node([gpu_pod("a", 2), gpu_pod("a", 1), gpu_pod("b", 4), {"spec": {}}]) == {"a": 3, "b": 4}


async def test_collect_inventory():
    inventory = await collect(FakeKube(), "redhat-ods-applications")
    assert inventory["facts"]["gpu_total"] == 12
    assert inventory["facts"]["gpu_used"] == 3
    assert inventory["facts"]["openshift_version"] == "4.18.5"
    assert inventory["facts"]["rhoai_version"] == "2.22.0"
    assert inventory["runtimes"][0]["name"] == "vllm-cuda-runtime"
    assert len(inventory["nodes"]) == 2


@pytest.mark.parametrize(
    "shape,precision,memory,expected_tp",
    [
        (LLAMA_8B, "bf16", 80, 1),
        (LLAMA_70B, "fp8", 80, 2),
        (LLAMA_70B, "bf16", 80, 4),
        (LLAMA_70B, "bf16", 48, 8),
    ],
)
def test_fit_chooses_smallest_tensor_parallel(shape, precision, memory, expected_tp):
    result = estimate(shape, FitRequest(precision=precision), memory)
    assert result["fits"]
    assert result["tensor_parallel"] == expected_tp


def test_fit_reports_no_fit():
    huge = ModelShape(params_b=405, layers=126, kv_heads=8, head_dim=128, attention_heads=128)
    assert not estimate(huge, FitRequest(precision="bf16"), 24)["fits"]


def test_shape_from_hf_config():
    config = {"num_hidden_layers": 32, "num_attention_heads": 32, "num_key_value_heads": 8, "hidden_size": 4096}
    shape = shape_from_config(config, 8_030_000_000)
    assert shape == ModelShape(params_b=8.03, layers=32, kv_heads=8, head_dim=128, attention_heads=32)


def test_curated_catalog_loads():
    models = load_curated()
    assert any(m["id"] == "meta-llama/Llama-3.1-70B-Instruct" for m in models)
    assert all(m["storage_uri"].startswith("hf://") for m in models)


def _cluster(name, nodes, status="active", labels=None, ctype="gpu", source="agent"):
    return {"name": name, "type": ctype, "status": status, "labels": labels or {}, "nodes": nodes, "source": source}


def _node(product, free, total=8, memory_mib=81559):
    return {
        "name": f"n-{product}",
        "gpu_product": product,
        "gpu_memory_mib": memory_mib,
        "gpu_allocatable": total,
        "gpu_used": total - free,
        "ready": True,
        "schedulable": True,
    }


def test_placement_explains_every_cluster():
    clusters = [
        _cluster("prod-east", [_node("NVIDIA H100 80GB HBM3", free=5)], labels={"region": "eu"}),
        _cluster("prod-west", [_node("NVIDIA L40S", free=8, memory_mib=46068)], labels={"region": "eu"}),
        _cluster("cpu-edge", [], ctype="cpu", labels={"region": "eu"}),
        _cluster("hub", [], ctype="hub", source="hub"),
        _cluster("new-one", [_node("NVIDIA H100 80GB HBM3", free=8)], status="pending"),
    ]
    request = PlacementRequest(shape=LLAMA_70B, fit=FitRequest(precision="fp8"), gpu_product="H100")
    result = {c["cluster"]: c for c in plan(clusters, request)}
    assert result["prod-east"]["result"] == "fits" and result["prod-east"]["recommended"]
    assert result["prod-east"]["tensor_parallel"] == 2
    assert result["prod-west"]["result"] == "fits_with_changes"
    assert result["cpu-edge"]["result"] == "not_eligible" and "CPU-only" in result["cpu-edge"]["reason"]
    assert "Hub" in result["hub"]["reason"]
    assert "waiting for approval" in result["new-one"]["reason"]


def test_placement_blocks_when_capacity_is_short():
    clusters = [_cluster("busy", [_node("NVIDIA H100 80GB HBM3", free=1)])]
    [candidate] = plan(clusters, PlacementRequest(shape=LLAMA_70B, fit=FitRequest(precision="fp8")))
    assert candidate["result"] == "blocked"
    assert "Needs 2×" in candidate["reason"]


def test_placement_respects_required_labels():
    clusters = [_cluster("us", [_node("NVIDIA H100 80GB HBM3", free=8)], labels={"region": "us"})]
    request = PlacementRequest(shape=LLAMA_8B, fit=FitRequest(), required_labels={"region": "eu"})
    assert plan(clusters, request)[0]["result"] == "not_eligible"


def _spec(**overrides):
    runtime = {
        "name": "vllm-cuda-runtime",
        "template": "vllm-cuda-runtime-template",
        "object": VLLM_TEMPLATE["objects"][0],
    }
    values = dict(
        name="contracts-rag",
        namespace="team-legal",
        team="legal",
        model_id="meta-llama/Llama-3.1-70B-Instruct",
        storage_uri="hf://meta-llama/Llama-3.1-70B-Instruct",
        runtime=runtime,
        tensor_parallel=2,
        max_model_len=32768,
        precision="fp8",
        gpu_product="NVIDIA H100 80GB HBM3",
    )
    values.update(overrides)
    return DeploySpec(**values)


def test_render_inference_service():
    objects = render(_spec())
    isvc = objects["inferenceservice.yaml"]
    predictor = isvc["spec"]["predictor"]
    assert isvc["metadata"]["annotations"]["serving.kserve.io/deploymentMode"] == "RawDeployment"
    assert predictor["model"]["runtime"] == "contracts-rag"
    assert predictor["model"]["resources"]["limits"]["nvidia.com/gpu"] == "2"
    assert "--tensor-parallel-size=2" in predictor["model"]["args"]
    assert "--quantization=fp8" in predictor["model"]["args"]
    assert predictor["nodeSelector"] == {"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}
    runtime = objects["servingruntime.yaml"]
    assert runtime["metadata"]["name"] == "contracts-rag"
    assert runtime["metadata"]["namespace"] == "team-legal"
    assert runtime["spec"]["containers"][0]["image"] == "quay.io/modh/vllm@sha256:abc"
    # The template object must not be modified in place.
    assert VLLM_TEMPLATE["objects"][0]["metadata"]["name"] == "vllm-cuda-runtime"


def test_render_rejects_bad_names():
    with pytest.raises(RenderError):
        render(_spec(name="Bad_Name"))


def test_parse_managed_cluster():
    mc = {
        "metadata": {"name": "prod-east", "labels": {"tensorhelm.io/gpu": "true"}},
        "spec": {"managedClusterClientConfigs": [{"url": "https://api.prod-east:6443"}]},
        "status": {
            "conditions": [
                {"type": "ManagedClusterConditionAvailable", "status": "True"},
                {"type": "ManagedClusterJoined", "status": "True"},
            ],
            "clusterClaims": [
                {"name": "version.openshift.io", "value": "4.18.5"},
                {"name": "region.open-cluster-management.io", "value": "eu-central-1"},
            ],
        },
    }
    view = parse_managed_cluster(mc)
    assert view["available"] and view["joined"]
    assert view["openshift_version"] == "4.18.5"
    assert view["region"] == "eu-central-1"


def test_placement_prefers_fewer_gpus_per_replica():
    clusters = [
        _cluster("l40s", [_node("NVIDIA L40S", free=8, memory_mib=46068)]),
        _cluster("h100", [_node("NVIDIA H100 80GB HBM3", free=2)]),
    ]
    candidates = plan(clusters, PlacementRequest(shape=LLAMA_70B, fit=FitRequest(precision="fp8")))
    assert [c["cluster"] for c in candidates] == ["h100", "l40s"]
    assert candidates[0]["recommended"] and candidates[0]["tensor_parallel"] == 2
