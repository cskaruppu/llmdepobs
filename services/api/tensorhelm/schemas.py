from pydantic import BaseModel, Field, field_validator

from tensorhelm.fit import PRECISION_BYTES

CLUSTER_NAME = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"
VLLM_ARG = r"^--[a-z0-9][a-z0-9-]*(=[^\s]+)?$"


class PlanRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=255)
    precision: str = "bf16"
    max_model_len: int = Field(8192, ge=512, le=1_048_576)
    concurrency: int = Field(16, ge=1, le=1024)
    min_replicas: int = Field(1, ge=1, le=64)
    gpu_product: str | None = None
    required_labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("precision")
    @classmethod
    def known_precision(cls, value: str) -> str:
        if value not in PRECISION_BYTES:
            raise ValueError(f"precision must be one of {', '.join(PRECISION_BYTES)}")
        return value


class DeploymentCreate(PlanRequest):
    name: str = Field(min_length=1, max_length=40)
    team: str = Field(min_length=1, max_length=40)
    cluster: str = Field(min_length=1, max_length=253)
    namespace: str | None = None
    storage_uri: str | None = None
    runtime: str | None = None
    max_replicas: int = Field(1, ge=1, le=64)
    tensor_parallel: int | None = Field(None, ge=1, le=8)
    prefix_caching: bool = True
    extra_args: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("extra_args")
    @classmethod
    def safe_args(cls, values: list[str]) -> list[str]:
        import re

        for value in values:
            if not re.match(VLLM_ARG, value):
                raise ValueError(f"vLLM arguments must look like --flag or --flag=value: {value!r}")
        return values


class AgentRegister(BaseModel):
    cluster_name: str = Field(pattern=CLUSTER_NAME)
    agent_version: str = Field("unknown", max_length=32)
    labels: dict[str, str] = Field(default_factory=dict)
    facts: dict = Field(default_factory=dict)


class AgentReport(AgentRegister):
    nodes: list[dict] = Field(default_factory=list, max_length=5000)
    endpoints: list[dict] = Field(default_factory=list, max_length=5000)
    runtimes: list[dict] = Field(default_factory=list, max_length=200)
