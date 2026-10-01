"""Estimates the GPU memory a model needs on vLLM and the smallest tensor-parallel size that fits.

The numbers are a planning estimate: weights at the chosen precision, plus a KV cache sized for
`concurrency` requests that each use the full context, plus a fixed per-GPU overhead for activations
and CUDA graphs. vLLM shares KV cache between requests, so real usage is usually lower.
"""

from dataclasses import dataclass

PRECISION_BYTES = {"bf16": 2.0, "fp16": 2.0, "fp8": 1.0, "int8": 1.0, "int4": 0.5}
TENSOR_PARALLEL_SIZES = (1, 2, 4, 8)
OVERHEAD_GB_PER_GPU = 2.0


@dataclass(frozen=True)
class ModelShape:
    params_b: float  # billions of parameters
    layers: int | None = None
    kv_heads: int | None = None
    head_dim: int | None = None
    attention_heads: int | None = None


@dataclass(frozen=True)
class FitRequest:
    precision: str = "bf16"
    kv_cache_precision: str | None = None  # defaults to fp8 when weights are fp8, else bf16
    max_model_len: int = 8192
    concurrency: int = 16
    gpu_memory_utilization: float = 0.90


def weights_gb(shape: ModelShape, precision: str) -> float:
    return shape.params_b * PRECISION_BYTES[precision]


def kv_cache_gb(shape: ModelShape, request: FitRequest) -> float | None:
    if not (shape.layers and shape.kv_heads and shape.head_dim):
        return None
    kv_precision = request.kv_cache_precision or ("fp8" if request.precision == "fp8" else "bf16")
    per_token = 2 * shape.layers * shape.kv_heads * shape.head_dim * PRECISION_BYTES[kv_precision]
    return per_token * request.max_model_len * request.concurrency / 1e9


def estimate(shape: ModelShape, request: FitRequest, gpu_memory_gb: float) -> dict:
    """Return the memory estimate and the smallest tensor-parallel size that fits on GPUs of this size."""
    if request.precision not in PRECISION_BYTES:
        raise ValueError(f"unknown precision {request.precision!r}")
    weights = weights_gb(shape, request.precision)
    kv = kv_cache_gb(shape, request)
    # Without layer data, assume a KV cache of 20% of the weights as a rough placeholder.
    kv_effective = kv if kv is not None else weights * 0.2
    usable_per_gpu = gpu_memory_gb * request.gpu_memory_utilization

    chosen = None
    for tp in TENSOR_PARALLEL_SIZES:
        if shape.attention_heads and shape.attention_heads % tp:
            continue
        needed = weights + kv_effective + OVERHEAD_GB_PER_GPU * tp
        if needed <= usable_per_gpu * tp:
            chosen = tp
            break
    return {
        "weights_gb": round(weights, 1),
        "kv_cache_gb": round(kv, 1) if kv is not None else None,
        "kv_cache_estimated": kv is None,
        "total_gb": round(weights + kv_effective + OVERHEAD_GB_PER_GPU * (chosen or 1), 1),
        "gpu_memory_gb": round(gpu_memory_gb, 1),
        "tensor_parallel": chosen,
        "fits": chosen is not None,
    }
