"""Model catalog: built-in starter list, Hugging Face search, OpenShift AI Model Registry and OpenRouter."""

import logging
from importlib import resources
from typing import Any

import httpx
import yaml

from tensorhelm.config import Settings
from tensorhelm.fit import ModelShape

log = logging.getLogger(__name__)


class CatalogError(Exception):
    pass


def load_curated() -> list[dict]:
    text = resources.files("tensorhelm").joinpath("data/models.yaml").read_text()
    models = []
    for entry in yaml.safe_load(text):
        models.append(
            {
                **{k: v for k, v in entry.items() if k != "shape"},
                "source": "curated",
                "deployable": True,
                "storage_uri": f"hf://{entry['id']}",
                "params_b": entry["shape"]["params_b"],
                "shape": entry["shape"],
            }
        )
    return models


def shape_from_config(config: dict, params_total: int | None) -> ModelShape | None:
    config = config if "num_hidden_layers" in config else config.get("text_config") or config
    if not params_total:
        return None
    heads = config.get("num_attention_heads")
    head_dim = config.get("head_dim") or (
        config["hidden_size"] // heads if heads and config.get("hidden_size") else None
    )
    return ModelShape(
        params_b=round(params_total / 1e9, 2),
        layers=config.get("num_hidden_layers"),
        kv_heads=config.get("num_key_value_heads") or heads,
        head_dim=head_dim,
        attention_heads=heads,
    )


def _license(tags: list[str]) -> str | None:
    for tag in tags:
        if tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


class Catalog:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self.curated = load_curated()
        self._shapes: dict[str, ModelShape] = {m["id"]: ModelShape(**m["shape"]) for m in self.curated}

    def _hf_headers(self) -> dict[str, str]:
        token = self.settings.huggingface_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def search_huggingface(self, query: str, limit: int = 20) -> list[dict]:
        params: dict[str, Any] = {
            "search": query,
            "pipeline_tag": "text-generation",
            "sort": "downloads",
            "direction": -1,
            "limit": limit,
        }
        try:
            response = await self.http.get(
                f"{self.settings.huggingface_url}/api/models", params=params, headers=self._hf_headers()
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalogError(f"Hugging Face search failed: {exc}") from exc
        models = []
        for item in response.json():
            model_id = item.get("id") or item.get("modelId")
            models.append(
                {
                    "id": model_id,
                    "name": model_id.split("/")[-1],
                    "provider": model_id.split("/")[0] if "/" in model_id else None,
                    "source": "huggingface",
                    "deployable": True,
                    "gated": bool(item.get("gated")),
                    "downloads": item.get("downloads"),
                    "license": _license(item.get("tags") or []),
                    "task": item.get("pipeline_tag"),
                    "storage_uri": f"hf://{model_id}",
                }
            )
        return models

    async def shape_for(self, model_id: str) -> ModelShape | None:
        if model_id in self._shapes:
            return self._shapes[model_id]
        try:
            info = await self.http.get(
                f"{self.settings.huggingface_url}/api/models/{model_id}", headers=self._hf_headers()
            )
            info.raise_for_status()
            config = await self.http.get(
                f"{self.settings.huggingface_url}/{model_id}/resolve/main/config.json",
                headers=self._hf_headers(),
                follow_redirects=True,
            )
            config.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("no shape for %s: %s", model_id, exc)
            return None
        total = (info.json().get("safetensors") or {}).get("total")
        shape = shape_from_config(config.json(), total)
        if shape:
            self._shapes[model_id] = shape
        return shape

    async def model_registry(self) -> list[dict]:
        base = self.settings.model_registry_url.rstrip("/")
        if not base:
            return []
        headers = (
            {"Authorization": f"Bearer {self.settings.model_registry_token}"}
            if self.settings.model_registry_token
            else {}
        )
        api = f"{base}/api/model_registry/v1alpha3"
        try:
            response = await self.http.get(f"{api}/registered_models", params={"pageSize": 100}, headers=headers)
            response.raise_for_status()
            models = []
            for item in response.json().get("items") or []:
                uri = None
                versions = await self.http.get(
                    f"{api}/registered_models/{item['id']}/versions", params={"pageSize": 1}, headers=headers
                )
                if versions.status_code == 200 and (versions.json().get("items") or []):
                    version_id = versions.json()["items"][0]["id"]
                    artifacts = await self.http.get(f"{api}/model_versions/{version_id}/artifacts", headers=headers)
                    if artifacts.status_code == 200:
                        uri = next((a.get("uri") for a in artifacts.json().get("items") or [] if a.get("uri")), None)
                models.append(
                    {
                        "id": item.get("name"),
                        "name": item.get("name"),
                        "provider": item.get("owner"),
                        "description": item.get("description"),
                        "source": "registry",
                        "deployable": bool(uri),
                        "storage_uri": uri,
                    }
                )
            return models
        except httpx.HTTPError as exc:
            raise CatalogError(f"Model Registry request failed: {exc}") from exc

    async def openrouter(self) -> list[dict]:
        if not self.settings.openrouter_enabled:
            return []
        try:
            response = await self.http.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CatalogError(f"OpenRouter request failed: {exc}") from exc
        return [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "provider": (item.get("id") or "").split("/")[0],
                "source": "openrouter",
                "deployable": False,
                "external": True,
                "context": item.get("context_length"),
                "pricing": item.get("pricing"),
            }
            for item in response.json().get("data") or []
        ]
