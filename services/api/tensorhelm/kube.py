"""Thin async wrapper around the Kubernetes API. Every method returns plain dicts so the rest of the
code (and the tests) never depend on generated client classes."""

import base64
import logging
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.rest import ApiException

log = logging.getLogger(__name__)

# group, version, plural for the custom resources Tensorhelm reads or writes.
INFERENCE_SERVICE = ("serving.kserve.io", "v1beta1", "inferenceservices")
SERVING_RUNTIME = ("serving.kserve.io", "v1alpha1", "servingruntimes")
MANAGED_CLUSTER = ("cluster.open-cluster-management.io", "v1", "managedclusters")
DISCOVERED_CLUSTER = ("discovery.open-cluster-management.io", "v1", "discoveredclusters")
DATA_SCIENCE_CLUSTER = ("datasciencecluster.opendatahub.io", "v1", "datascienceclusters")
TEMPLATE = ("template.openshift.io", "v1", "templates")
CLUSTER_VERSION = ("config.openshift.io", "v1", "clusterversions")
INFRASTRUCTURE = ("config.openshift.io", "v1", "infrastructures")
GROUP = ("user.openshift.io", "v1", "groups")

KIND_TO_RESOURCE = {
    "InferenceService": INFERENCE_SERVICE,
    "ServingRuntime": SERVING_RUNTIME,
}


class KubeClient:
    def __init__(self, api: client.ApiClient):
        self.api = api
        self.core = client.CoreV1Api(api)
        self.custom = client.CustomObjectsApi(api)

    @classmethod
    async def connect(cls) -> "KubeClient":
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        return cls(client.ApiClient())

    async def close(self) -> None:
        await self.api.close()

    def _plain(self, obj: Any) -> Any:
        return self.api.sanitize_for_serialization(obj)

    # ---- core -------------------------------------------------------------------------------------

    async def list_nodes(self) -> list[dict]:
        return self._plain(await self.core.list_node()).get("items", [])

    async def list_active_pods(self) -> list[dict]:
        pods = await self.core.list_pod_for_all_namespaces(
            field_selector="status.phase!=Succeeded,status.phase!=Failed"
        )
        return self._plain(pods).get("items", [])

    async def read_secret_value(self, namespace: str, name: str, key: str) -> str | None:
        try:
            secret = await self.core.read_namespaced_secret(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        raw = (secret.data or {}).get(key)
        return base64.b64decode(raw).decode() if raw else None

    async def write_secret(self, namespace: str, name: str, values: dict[str, str]) -> None:
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=name, namespace=namespace),
            string_data=values,
            type="Opaque",
        )
        try:
            await self.core.create_namespaced_secret(namespace, body)
        except ApiException as exc:
            if exc.status != 409:
                raise
            await self.core.replace_namespaced_secret(name, namespace, body)

    async def ensure_namespace(self, name: str, labels: dict[str, str]) -> None:
        try:
            await self.core.read_namespace(name)
            return
        except ApiException as exc:
            if exc.status != 404:
                raise
        body = client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels=labels))
        await self.core.create_namespace(body)

    # ---- custom resources ---------------------------------------------------------------------------

    async def _list(self, resource: tuple[str, str, str], namespace: str | None = None) -> list[dict]:
        group, version, plural = resource
        try:
            if namespace:
                result = await self.custom.list_namespaced_custom_object(group, version, namespace, plural)
            else:
                result = await self.custom.list_cluster_custom_object(group, version, plural)
        except ApiException as exc:
            # 404: the CRD is not installed (for example no RHACM or no OpenShift AI). 403: not allowed.
            if exc.status in (403, 404):
                log.debug("cannot list %s/%s: %s", group, plural, exc.status)
                return []
            raise
        return result.get("items", [])

    async def _get_cluster_scoped(self, resource: tuple[str, str, str], name: str) -> dict | None:
        group, version, plural = resource
        try:
            return await self.custom.get_cluster_custom_object(group, version, plural, name)
        except ApiException as exc:
            if exc.status in (403, 404):
                return None
            raise

    async def list_inferenceservices(self) -> list[dict]:
        return await self._list(INFERENCE_SERVICE)

    async def list_runtime_templates(self, namespace: str) -> list[dict]:
        return await self._list(TEMPLATE, namespace)

    async def list_datascienceclusters(self) -> list[dict]:
        return await self._list(DATA_SCIENCE_CLUSTER)

    async def list_managed_clusters(self) -> list[dict]:
        return await self._list(MANAGED_CLUSTER)

    async def list_discovered_clusters(self) -> list[dict]:
        return await self._list(DISCOVERED_CLUSTER)

    async def list_groups(self) -> list[dict]:
        return await self._list(GROUP)

    async def get_cluster_version(self) -> dict | None:
        return await self._get_cluster_scoped(CLUSTER_VERSION, "version")

    async def get_infrastructure(self) -> dict | None:
        return await self._get_cluster_scoped(INFRASTRUCTURE, "cluster")

    async def apply(self, obj: dict) -> None:
        """Create the object, or replace it if it exists. Supports Namespace and the kinds in KIND_TO_RESOURCE."""
        kind = obj["kind"]
        meta = obj["metadata"]
        if kind == "Namespace":
            await self.ensure_namespace(meta["name"], meta.get("labels", {}))
            return
        group, version, plural = KIND_TO_RESOURCE[kind]
        namespace, name = meta["namespace"], meta["name"]
        try:
            current = await self.custom.get_namespaced_custom_object(group, version, namespace, plural, name)
        except ApiException as exc:
            if exc.status != 404:
                raise
            await self.custom.create_namespaced_custom_object(group, version, namespace, plural, obj)
            return
        obj = {**obj, "metadata": {**meta, "resourceVersion": current["metadata"]["resourceVersion"]}}
        await self.custom.replace_namespaced_custom_object(group, version, namespace, plural, name, obj)

    async def delete(self, kind: str, namespace: str, name: str) -> None:
        group, version, plural = KIND_TO_RESOURCE[kind]
        try:
            await self.custom.delete_namespaced_custom_object(group, version, namespace, plural, name)
        except ApiException as exc:
            if exc.status != 404:
                raise
