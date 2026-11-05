"""SQLAlchemy models. All persistent state lives in PostgreSQL."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now()


class Audit(Base):
    __tablename__ = "audits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    # queued | running | cancel_requested | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    input: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    settings_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    scores: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    coverage: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    cost: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    report_keys: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_jobs_claim", "status", "run_after", "priority"),)


class Page(Base):
    __tablename__ = "pages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    is_html: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    indexable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    depth: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int | None] = mapped_column(Integer)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    in_sitemap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    inlinks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    template_key: Mapped[str | None] = mapped_column(String(255))
    rendered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    js_dependent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("audit_id", "url", name="uq_pages_audit_url"),)


class PageText(Base):
    """Visible text, kept only for the most recent audits per site (retention setting)."""
    __tablename__ = "page_texts"
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class Link(Base):
    __tablename__ = "links"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), index=True)
    source_page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[str | None] = mapped_column(Text)
    internal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    nofollow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sponsored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ugc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    target_status: Mapped[int | None] = mapped_column(Integer)


class Image(Base):
    __tablename__ = "images"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    src: Mapped[str] = mapped_column(Text, nullable=False)
    alt: Mapped[str | None] = mapped_column(Text)
    has_alt: Mapped[bool] = mapped_column(Boolean, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    width_attr: Mapped[int | None] = mapped_column(Integer)
    height_attr: Mapped[int | None] = mapped_column(Integer)
    rendered_w: Mapped[int | None] = mapped_column(Integer)
    rendered_h: Mapped[int | None] = mapped_column(Integer)
    intrinsic_w: Mapped[int | None] = mapped_column(Integer)
    intrinsic_h: Mapped[int | None] = mapped_column(Integer)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    format: Mapped[str | None] = mapped_column(String(16))
    loading: Mapped[str | None] = mapped_column(String(16))


class SiteFacts(Base):
    __tablename__ = "site_facts"
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ModuleRun(Base):
    __tablename__ = "module_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # done | failed | planned | skipped
    coverage: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class Finding(Base):
    """One row per check per audit (including PASS and NOT_CHECKED)."""
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    module_number: Mapped[int] = mapped_column(Integer, nullable=False)
    check_id: Mapped[str] = mapped_column(String(96), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    why: Mapped[str | None] = mapped_column(Text)
    fix: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(String(8), nullable=False)
    effort: Mapped[str] = mapped_column(String(8), nullable=False)
    priority: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    affected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int | None] = mapped_column(Integer)
    items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    items_truncated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    urls: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    generated_fix: Mapped[dict | None] = mapped_column(JSONB)
    sources: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    missing_label: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_findings_urls", "urls", postgresql_using="gin"),
        Index("ix_findings_audit_status", "audit_id", "status"),
    )


class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audits.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[dict | list | float | int | str | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(128), nullable=False)


class CacheEntry(Base):
    __tablename__ = "cache"
    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    units: Mapped[float] = mapped_column(Float, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    audit_id: Mapped[int | None] = mapped_column(ForeignKey("audits.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_usage_provider_time", "provider", "created_at"),)


class SettingKV(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)


class Blob(Base):
    """Fallback blob store (local dev / when Object Storage is not configured)."""
    __tablename__ = "blobs"
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = _now()
