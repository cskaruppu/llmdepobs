import httpx
import pytest

from tensorhelm.config import Settings
from tensorhelm.main import create_app


def gpu_node(name, product="NVIDIA-H100-80GB-HBM3", count=8, memory_mib=81559, ready=True, unschedulable=False):
    return {
        "metadata": {
            "name": name,
            "labels": {
                "nvidia.com/gpu.present": "true",
                "nvidia.com/gpu.product": product,
                "nvidia.com/gpu.memory": str(memory_mib),
                "nvidia.com/cuda.driver-version.full": "550.90.07",
                "nvidia.com/cuda.runtime-version.full": "12.4",
            },
        },
        "spec": {"unschedulable": unschedulable},
        "status": {
            "capacity": {"cpu": "96", "nvidia.com/gpu": str(count)},
            "allocatable": {"cpu": "95500m", "nvidia.com/gpu": str(count)},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def cpu_node(name):
    return {
        "metadata": {"name": name, "labels": {}},
        "spec": {},
        "status": {"allocatable": {"cpu": "32"}, "conditions": [{"type": "Ready", "status": "True"}]},
    }


def gpu_pod(node, gpus):
    return {"spec": {"nodeName": node, "containers": [{"resources": {"limits": {"nvidia.com/gpu": str(gpus)}}}]}}


VLLM_TEMPLATE = {
    "metadata": {"name": "vllm-cuda-runtime-template"},
    "objects": [
        {
            "apiVersion": "serving.kserve.io/v1alpha1",
            "kind": "ServingRuntime",
            "metadata": {"name": "vllm-cuda-runtime", "annotations": {"openshift.io/display-name": "vLLM NVIDIA GPU"}},
            "spec": {
                "supportedModelFormats": [{"name": "vLLM", "autoSelect": True}],
                "containers": [{"name": "kserve-container", "image": "quay.io/modh/vllm@sha256:abc"}],
            },
        }
    ],
}


class FakeKube:
    def __init__(self):
        self.nodes = [gpu_node("gpu-1"), gpu_node("gpu-2", count=4), cpu_node("worker-1")]
        self.pods = [gpu_pod("gpu-1", 3)]
        self.isvcs = []
        self.templates = [VLLM_TEMPLATE]
        self.managed = []
        self.discovered = []
        self.groups = []
        self.applied = []
        self.deleted = []
        self.secrets = {}

    async def list_nodes(self):
        return self.nodes

    async def list_active_pods(self):
        return self.pods

    async def list_inferenceservices(self):
        return self.isvcs

    async def list_runtime_templates(self, namespace):
        return self.templates

    async def list_datascienceclusters(self):
        return [{"status": {"release": {"version": "2.22.0"}}}]

    async def get_cluster_version(self):
        return {"spec": {"clusterID": "abc-123"}, "status": {"desired": {"version": "4.18.5"}}}

    async def get_infrastructure(self):
        return {"status": {"apiServerURL": "https://api.hub.example.com:6443", "platformStatus": {"type": "BareMetal"}}}

    async def list_managed_clusters(self):
        return self.managed

    async def list_discovered_clusters(self):
        return self.discovered

    async def list_groups(self):
        return self.groups

    async def apply(self, obj):
        self.applied.append(obj)

    async def delete(self, kind, namespace, name):
        self.deleted.append((kind, namespace, name))

    async def read_secret_value(self, namespace, name, key):
        return self.secrets.get((namespace, name, key))

    async def write_secret(self, namespace, name, values):
        for key, value in values.items():
            self.secrets[(namespace, name, key)] = value

    async def close(self):
        pass


@pytest.fixture
def kube():
    return FakeKube()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        auth_mode="dev",
        agent_bootstrap_token="boot-secret",
        kube_enabled=False,
        huggingface_url="http://hf.invalid",
    )


@pytest.fixture
def gitlab_calls():
    return []


@pytest.fixture
def outbound(gitlab_calls):
    """Outbound HTTP: a fake GitLab that records commits; everything else is unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gitlab.example.com":
            if request.method == "HEAD":
                return httpx.Response(404)
            gitlab_calls.append(request)
            return httpx.Response(201, json={"id": "4f2a91c0ffee"})
        return httpx.Response(503)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
async def make_client(settings, kube, outbound):
    clients = []

    async def factory(**overrides):
        app = create_app(settings.model_copy(update=overrides), kube=kube, http=outbound, start_background=False)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append((client, ctx))
        return app, client

    yield factory
    for client, ctx in clients:
        await client.aclose()
        await ctx.__aexit__(None, None, None)
