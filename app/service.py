"""The processing pipeline: upload -> cut out -> store -> register.

``NukkiService`` is the single place where an uploaded photo is turned into a
set of stored assets and a database record. The HTTP layer (``main.py``) and
the batch CLI (``scripts/batch.py``) both call it, so the automatic behaviour
is identical however a photo enters the system.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from .config import Settings
from .engine import BackgroundRemover
from .models import AssetStatus, ImageAsset
from .storage import Storage

log = logging.getLogger("nukki.service")


class ProcessingError(Exception):
    """Raised for user-correctable problems (bad file, too large)."""


class NukkiService:
    def __init__(self, settings: Settings, engine: BackgroundRemover, storage: Storage):
        self._settings = settings
        self._engine = engine
        self._storage = storage

    # -- public API ---------------------------------------------------------
    def register(
        self,
        db: Session,
        *,
        data: bytes,
        filename: str | None,
        external_id: str | None = None,
    ) -> ImageAsset:
        """Create the pending registration row for an upload (fast, no ML)."""
        if len(data) > self._settings.max_upload_mb * 1024 * 1024:
            raise ProcessingError(
                f"File exceeds the {self._settings.max_upload_mb} MB limit"
            )
        asset = ImageAsset(
            external_id=external_id,
            original_filename=filename,
            status=AssetStatus.pending,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return asset

    def process(
        self,
        db: Session,
        asset_id: str,
        data: bytes,
        *,
        model: str | None = None,
    ) -> ImageAsset:
        """Run the 누끼 pipeline for an already-registered asset and store it."""
        asset = db.get(ImageAsset, asset_id)
        if asset is None:
            raise ProcessingError(f"Unknown asset {asset_id}")

        asset.status = AssetStatus.processing
        db.commit()

        try:
            image = self._decode(data)
            result = self._engine.cutout(image, model=model)

            prefix = self._key_prefix(asset.id)
            asset.original_url = self._storage.save(
                f"{prefix}/original.jpg", self._encode_jpeg(result.original), "image/jpeg"
            )
            asset.cutout_url = self._storage.save(
                f"{prefix}/cutout.png", self._encode_png(result.cutout), "image/png"
            )
            asset.flat_url = self._storage.save(
                f"{prefix}/flat.jpg", self._encode_jpeg(result.flat), "image/jpeg"
            )
            asset.thumbnail_url = self._storage.save(
                f"{prefix}/thumb.png", self._encode_png(result.thumbnail), "image/png"
            )

            asset.model = result.model
            asset.width = result.width
            asset.height = result.height
            asset.processing_ms = result.elapsed_ms
            asset.foreground_ratio = round(result.foreground_ratio, 4)
            asset.needs_review = result.is_suspicious
            asset.status = AssetStatus.done
            asset.error = None
            db.commit()
            db.refresh(asset)
            log.info(
                "processed asset=%s model=%s ms=%d fg=%.3f review=%s",
                asset.id, asset.model, asset.processing_ms,
                asset.foreground_ratio, asset.needs_review,
            )
            return asset
        except Exception as exc:  # noqa: BLE001 — record then surface any failure
            # Covers a bad/unreadable image (ProcessingError) and any engine or
            # storage error: the row must not be left stuck at "processing".
            db.rollback()
            failed = db.get(ImageAsset, asset_id)
            if failed is not None:
                failed.status = AssetStatus.failed
                failed.error = f"{type(exc).__name__}: {exc}"
                db.commit()
            if isinstance(exc, ProcessingError):
                log.info("rejected asset=%s: %s", asset_id, exc)
            else:
                log.exception("failed to process asset=%s", asset_id)
            raise

    def register_and_process(
        self,
        db: Session,
        *,
        data: bytes,
        filename: str | None,
        external_id: str | None = None,
        model: str | None = None,
    ) -> ImageAsset:
        """Synchronous convenience path: register then process in one call."""
        asset = self.register(db, data=data, filename=filename, external_id=external_id)
        return self.process(db, asset.id, data, model=model)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _decode(data: bytes) -> Image.Image:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except (UnidentifiedImageError, OSError) as exc:
            raise ProcessingError("Not a readable image file") from exc

    @staticmethod
    def _key_prefix(asset_id: str) -> str:
        now = datetime.now(timezone.utc)
        return f"{now:%Y/%m}/{asset_id}"

    def _encode_jpeg(self, img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.convert("RGB").save(
            buf, format="JPEG", quality=self._settings.jpeg_quality, optimize=True
        )
        return buf.getvalue()

    @staticmethod
    def _encode_png(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
