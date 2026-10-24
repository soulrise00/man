"""The registration record — one row per uploaded photo and its 누끼 result."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AssetStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class ImageAsset(Base):
    """A photo registered on the platform together with its cutout outputs.

    This is the "등록" (registration) record: the pipeline writes one of these
    for every upload so downstream systems can query what was processed,
    where the assets live, and whether a result looks trustworthy.
    """

    __tablename__ = "image_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # Optional external reference supplied by the caller (e.g. product SKU),
    # so a platform can tie the cutout back to its own entity.
    external_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)

    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, native_enum=False, length=16),
        default=AssetStatus.pending,
        index=True,
    )
    original_filename: Mapped[str | None] = mapped_column(String(512), default=None)
    model: Mapped[str | None] = mapped_column(String(64), default=None)

    # Public URLs of the stored variants.
    original_url: Mapped[str | None] = mapped_column(Text, default=None)
    cutout_url: Mapped[str | None] = mapped_column(Text, default=None)
    flat_url: Mapped[str | None] = mapped_column(Text, default=None)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, default=None)

    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    processing_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    foreground_ratio: Mapped[float | None] = mapped_column(Float, default=None)
    # True when the mask looks suspicious (near-empty/near-full) — a review hint.
    needs_review: Mapped[bool | None] = mapped_column(default=None)

    error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
