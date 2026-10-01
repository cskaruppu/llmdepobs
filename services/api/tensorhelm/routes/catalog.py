from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from tensorhelm.auth import User, current_user
from tensorhelm.catalog import CatalogError

router = APIRouter(prefix="/api/v1")

SOURCES = ("curated", "huggingface", "registry", "openrouter")


@router.get("/catalog/sources")
async def sources(request: Request, _: User = Depends(current_user)) -> list:
    settings = request.app.state.settings
    return [
        {"id": "curated", "name": "Starter catalog", "enabled": True},
        {"id": "registry", "name": "OpenShift AI Model Registry", "enabled": bool(settings.model_registry_url)},
        {"id": "huggingface", "name": "Hugging Face", "enabled": True},
        {"id": "openrouter", "name": "OpenRouter (external API)", "enabled": settings.openrouter_enabled},
    ]


@router.get("/catalog/models")
async def models(
    request: Request,
    source: str = Query("curated"),
    q: str = Query("", max_length=100),
    _: User = Depends(current_user),
) -> dict:
    if source not in SOURCES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"source must be one of {', '.join(SOURCES)}")
    catalog = request.app.state.catalog
    try:
        if source == "curated":
            items = catalog.curated
        elif source == "huggingface":
            items = await catalog.search_huggingface(q or "instruct")
        elif source == "registry":
            items = await catalog.model_registry()
        else:
            items = await catalog.openrouter()
    except CatalogError as exc:
        return {"models": [], "warning": str(exc)}
    if q and source != "huggingface":
        needle = q.lower()
        items = [m for m in items if needle in (m.get("id") or "").lower() or needle in (m.get("name") or "").lower()]
    return {"models": items, "warning": None}
