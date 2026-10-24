"""Pydantic response/ request schemas for the HTTP API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .models import AssetStatus


class AssetOut(BaseModel):
    """The registration record as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    external_id: str | None = None
    status: AssetStatus
    original_filename: str | None = None
    model: str | None = None

    original_url: str | None = None
    cutout_url: str | None = None
    flat_url: str | None = None
    thumbnail_url: str | None = None

    width: int | None = None
    height: int | None = None
    processing_ms: int | None = None
    foreground_ratio: float | None = None
    needs_review: bool | None = None
    error: str | None = None

    created_at: datetime
    updated_at: datetime


class AssetList(BaseModel):
    total: int
    items: list[AssetOut]


class HealthOut(BaseModel):
    status: str
    default_model: str
    loaded_models: list[str]
    storage_backend: str
    version: str
