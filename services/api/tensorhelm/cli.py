"""Admin commands, run inside the API pod:  oc exec -n tensorhelm deploy/tensorhelm-api -- tensorhelm-admin <command>"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from tensorhelm import audit
from tensorhelm.clusters import cluster_status
from tensorhelm.config import get_settings
from tensorhelm.db import Database
from tensorhelm.models import Cluster, utcnow


async def _list(db: Database) -> int:
    settings = get_settings()
    async with db.sessions() as session:
        rows = (await session.execute(select(Cluster).order_by(Cluster.name))).scalars().all()
        print(f"{'CLUSTER':32} {'SOURCE':8} {'STATUS':12} {'GPUS':>5}  AGENT")
        for c in rows:
            gpus = sum(n["gpu_allocatable"] for n in c.nodes or [])
            print(
                f"{c.name:32} {c.source:8} {cluster_status(c, utcnow(), settings.agent_offline_after_s):12} "
                f"{gpus:>5}  {c.agent_version or '-'}"
            )
    return 0


async def _approve(db: Database, name: str) -> int:
    async with db.sessions() as session:
        cluster = await session.get(Cluster, name)
        if cluster is None or not cluster.agent_token_hash:
            print(f"No agent registration for {name}", file=sys.stderr)
            return 1
        cluster.approved = True
        audit.record(session, "tensorhelm-admin", "cluster.approved", name)
        await session.commit()
    print(f"Approved {name}")
    return 0


async def _remove(db: Database, name: str) -> int:
    async with db.sessions() as session:
        cluster = await session.get(Cluster, name)
        if cluster is None or cluster.source == "hub":
            print(f"Cannot remove {name}", file=sys.stderr)
            return 1
        await session.delete(cluster)
        audit.record(session, "tensorhelm-admin", "cluster.removed", name)
        await session.commit()
    print(f"Removed {name}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    db = Database(get_settings().database_url)
    await db.create_all()
    try:
        if args.command == "list-clusters":
            return await _list(db)
        if args.command == "approve-cluster":
            return await _approve(db, args.name)
        return await _remove(db, args.name)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="tensorhelm-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-clusters", help="List known clusters and their status")
    for name, text in (
        ("approve-cluster", "Approve a pending agent registration"),
        ("remove-cluster", "Forget a cluster so its agent can register again"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("name")
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
