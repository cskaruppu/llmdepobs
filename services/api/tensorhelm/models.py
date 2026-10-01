import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tensorhelm.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Cluster(Base):
    """A cluster the hub knows about: the hub itself, an RHACM managed cluster, or an agent registration."""

    __tablename__ = "clusters"

    name: Mapped[str] = mapped_column(String(253), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))  # hub | acm | agent
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    labels: Mapped[dict] = mapped_column(JSON, default=dict)  # labels reported by the agent
    facts: Mapped[dict] = mapped_column(JSON, default=dict)  # versions, API URL, CPU totals
    nodes: Mapped[list] = mapped_column(JSON, default=list)  # GPU nodes
    endpoints: Mapped[list] = mapped_column(JSON, default=list)  # InferenceServices
    runtimes: Mapped[list] = mapped_column(JSON, default=list)  # serving runtime templates
    acm: Mapped[dict] = mapped_column(JSON, default=dict)  # RHACM ManagedCluster view
    agent_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(63))
    team: Mapped[str] = mapped_column(String(63))
    namespace: Mapped[str] = mapped_column(String(63))
    cluster: Mapped[str] = mapped_column(String(253))
    model_id: Mapped[str] = mapped_column(String(255))
    storage_uri: Mapped[str] = mapped_column(String(1024))
    runtime: Mapped[str] = mapped_column(String(253))
    precision: Mapped[str] = mapped_column(String(16))
    gpus_per_replica: Mapped[int] = mapped_column(Integer)
    gpu_product: Mapped[str | None] = mapped_column(String(128), nullable=True)
    min_replicas: Mapped[int] = mapped_column(Integer, default=1)
    max_replicas: Mapped[int] = mapped_column(Integer, default=1)
    max_model_len: Mapped[int] = mapped_column(Integer)
    delivery: Mapped[str] = mapped_column(String(16))  # direct | gitops
    status: Mapped[str] = mapped_column(String(32), default="requested")  # requested | applied | committed | failed
    message: Mapped[str] = mapped_column(Text, default="")
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_paths: Mapped[list] = mapped_column(JSON, default=list)
    manifests: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(253))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(253))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(512))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
