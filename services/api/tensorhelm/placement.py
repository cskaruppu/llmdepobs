"""Chooses which clusters can run a model, and explains why the others cannot."""

from collections import defaultdict
from dataclasses import dataclass, field

from tensorhelm.fit import FitRequest, ModelShape, estimate

RESULT_ORDER = {"fits": 0, "fits_with_changes": 1, "blocked": 2, "not_eligible": 3}


@dataclass
class PlacementRequest:
    shape: ModelShape
    fit: FitRequest
    min_replicas: int = 1
    gpu_product: str | None = None  # preferred GPU, matched as a substring, e.g. "H100"
    required_labels: dict[str, str] = field(default_factory=dict)


def _matches(product: str, preferred: str | None) -> bool:
    return not preferred or preferred.lower() in product.lower()


def _options(cluster: dict, request: PlacementRequest) -> list[dict]:
    by_product: dict[str, list[dict]] = defaultdict(list)
    for node in cluster["nodes"]:
        if node["ready"] and node["schedulable"]:
            by_product[node["gpu_product"]].append(node)
    options = []
    for product, nodes in by_product.items():
        memory_mib = max(n.get("gpu_memory_mib") or 0 for n in nodes)
        if not memory_mib:
            continue
        est = estimate(request.shape, request.fit, memory_mib * 1024 * 1024 / 1e9)
        free_per_node = [max(n["gpu_allocatable"] - n["gpu_used"], 0) for n in nodes]
        tp = est["tensor_parallel"]
        options.append(
            {
                "gpu_product": product,
                "estimate": est,
                "tensor_parallel": tp,
                "gpus_free": sum(free_per_node),
                # Tensor parallelism stays inside one node, so count replicas node by node.
                "replicas_possible": sum(f // tp for f in free_per_node) if tp else 0,
            }
        )
    return options


def evaluate(cluster: dict, request: PlacementRequest) -> dict:
    base = {
        "cluster": cluster["name"],
        "type": cluster["type"],
        "region": cluster.get("region"),
        "gpu_product": None,
        "tensor_parallel": None,
        "gpus_free": sum(max(n["gpu_allocatable"] - n["gpu_used"], 0) for n in cluster["nodes"]),
        "replicas_possible": 0,
        "estimate": None,
    }

    def result(kind: str, reason: str, **extra) -> dict:
        return {**base, **extra, "result": kind, "reason": reason}

    if cluster["status"] == "managed":
        return result(
            "not_eligible", "Managed by RHACM, but the Tensorhelm agent is not installed, so GPU capacity is unknown"
        )
    if cluster["status"] != "active":
        reasons = {
            "pending": "Agent is waiting for approval on the Clusters page",
            "offline": "Agent has stopped reporting; capacity is unknown",
            "unreachable": "RHACM cannot reach this cluster",
        }
        return result("not_eligible", reasons.get(cluster["status"], f"Cluster is {cluster['status']}"))
    for key, value in request.required_labels.items():
        if cluster["labels"].get(key) != value:
            return result("not_eligible", f"Requires label {key}={value}")
    if not cluster["nodes"]:
        if cluster["type"] == "hub":
            return result("not_eligible", "Hub cluster runs management services only; it has no GPUs")
        return result("not_eligible", "CPU-only cluster; this model needs GPUs")

    options = _options(cluster, request)
    if not any(o["tensor_parallel"] for o in options):
        return result("not_eligible", "Model does not fit on this cluster's GPUs, even split across 8 GPUs")

    with_capacity = [o for o in options if o["tensor_parallel"] and o["replicas_possible"] >= request.min_replicas]
    with_capacity.sort(
        key=lambda o: (not _matches(o["gpu_product"], request.gpu_product), o["tensor_parallel"], -o["gpus_free"])
    )
    if with_capacity:
        best = with_capacity[0]
        fields = {k: best[k] for k in ("gpu_product", "tensor_parallel", "replicas_possible", "estimate")}
        if _matches(best["gpu_product"], request.gpu_product):
            return result(
                "fits",
                f"{best['replicas_possible']} replica(s) of {best['tensor_parallel']}× {best['gpu_product']} "
                "available now",
                **fields,
            )
        return result(
            "fits_with_changes",
            f"No free {request.gpu_product}; runs on {best['tensor_parallel']}× {best['gpu_product']} per replica",
            **fields,
        )

    fitting = sorted((o for o in options if o["tensor_parallel"]), key=lambda o: o["tensor_parallel"])
    best = fitting[0]
    needed = best["tensor_parallel"] * request.min_replicas
    return result(
        "blocked",
        f"Needs {needed}× {best['gpu_product']} ({best['tensor_parallel']} per replica on one node); "
        f"{best['gpus_free']} free",
        gpu_product=best["gpu_product"],
        tensor_parallel=best["tensor_parallel"],
        estimate=best["estimate"],
    )


def plan(clusters: list[dict], request: PlacementRequest) -> list[dict]:
    candidates = [evaluate(c, request) for c in clusters]
    # Fewest GPUs per replica first: smaller tensor-parallel groups mean lower latency and less contention.
    candidates.sort(key=lambda c: (RESULT_ORDER[c["result"]], c["tensor_parallel"] or 99, -c["gpus_free"]))
    for candidate in candidates:
        candidate["recommended"] = False
    for candidate in candidates:
        if candidate["result"] in ("fits", "fits_with_changes"):
            candidate["recommended"] = True
            break
    return candidates
