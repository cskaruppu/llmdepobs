import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from tensorhelm import __version__
from tensorhelm.auth import GroupResolver
from tensorhelm.catalog import Catalog
from tensorhelm.clusters import ensure_hub, store_hub_inventory, sync_managed_clusters
from tensorhelm.config import Settings, get_settings
from tensorhelm.db import Database
from tensorhelm.inventory import collect
from tensorhelm.routes import catalog, clusters, deployments, overview

log = logging.getLogger("tensorhelm")


class SinglePageApp(StaticFiles):
    """Serves the built web app and falls back to index.html so deep links like /clusters work."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


async def refresh_once(app: FastAPI) -> None:
    """Inventory the hub itself and mirror RHACM managed clusters."""
    state = app.state
    if state.kube is None:
        return
    settings: Settings = state.settings
    inventory = await collect(state.kube, settings.rhoai_applications_namespace)
    async with state.db.sessions() as session:
        await store_hub_inventory(session, settings.hub_cluster_name, inventory)
    if settings.acm_sync:
        managed = await state.kube.list_managed_clusters()
        async with state.db.sessions() as session:
            await sync_managed_clusters(session, managed, settings.hub_cluster_name)


async def refresh_loop(app: FastAPI) -> None:
    while True:
        try:
            await refresh_once(app)
        except Exception:
            log.exception("inventory refresh failed")
        await asyncio.sleep(app.state.settings.refresh_interval_s)


def create_app(
    settings: Settings | None = None, kube=None, http: httpx.AsyncClient | None = None, start_background: bool = True
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings.database_url)
        await app.state.db.create_all()
        async with app.state.db.sessions() as session:
            await ensure_hub(session, settings.hub_cluster_name)
            await session.commit()

        app.state.kube = kube
        if app.state.kube is None and settings.kube_enabled:
            try:
                from tensorhelm.kube import KubeClient

                app.state.kube = await KubeClient.connect()
            except Exception as exc:
                log.warning("Kubernetes API not available, inventory disabled: %s", exc)
        app.state.http = http or httpx.AsyncClient(timeout=settings.outbound_timeout_s)
        app.state.catalog = Catalog(settings, app.state.http)
        app.state.groups = GroupResolver(app.state.kube, settings.group_cache_s)

        task = asyncio.create_task(refresh_loop(app)) if start_background else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if http is None:
            await app.state.http.aclose()
        if app.state.kube is not None and kube is None:
            await app.state.kube.close()
        await app.state.db.dispose()

    app = FastAPI(title="Tensorhelm API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    for module in (overview, clusters, catalog, deployments):
        app.include_router(module.router)

    static_dir = os.environ.get("TH_STATIC_DIR")
    if static_dir and os.path.isdir(static_dir):
        # Local development convenience; in OpenShift nginx serves the web app.
        app.mount("/", SinglePageApp(directory=static_dir, html=True), name="web")
    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("TH_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), proxy_headers=True)


if __name__ == "__main__":
    run()
