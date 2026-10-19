from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AuditRow(Base):
    """One tool-call attempt. Metadata only: arguments and results are never stored,
    because they can contain customer data. Email and slug are copied in so history
    stays readable after a user is deleted or a server is renamed or removed."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # NULL for auth_failed
    user_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    key_id: Mapped[str | None] = mapped_column(String(8), nullable=True)  # public id, never the secret
    server_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    server_slug: Mapped[str | None] = mapped_column(String(63), nullable=True, index=True)
    tool: Mapped[str | None] = mapped_column(String(200), nullable=True)  # original (un-prefixed) tool name
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(12), index=True)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(8))  # "mcp" | "chat"
