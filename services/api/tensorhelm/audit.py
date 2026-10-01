from sqlalchemy.ext.asyncio import AsyncSession

from tensorhelm.models import AuditEvent


def record(session: AsyncSession, actor: str, action: str, target: str, **detail) -> None:
    """Add an audit event to the session. The caller commits it together with the change it describes."""
    session.add(AuditEvent(actor=actor, action=action, target=target, detail=detail))
