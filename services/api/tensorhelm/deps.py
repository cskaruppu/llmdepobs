from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tensorhelm.clusters import cluster_view
from tensorhelm.models import Cluster, utcnow


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db.sessions() as session:
        yield session


async def cluster_views(request: Request, session: AsyncSession) -> list[dict]:
    settings = request.app.state.settings
    now = utcnow()
    clusters = (await session.execute(select(Cluster).order_by(Cluster.name))).scalars().all()
    views = [cluster_view(c, now, settings.agent_offline_after_s) for c in clusters]
    # Hub first, then the rest by name.
    return sorted(views, key=lambda v: (v["source"] != "hub", v["name"]))
