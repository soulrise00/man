"""FastAPI application.

Upload a photo and it is **automatically** cut out (누끼) and **registered**:
one row per image, with public URLs for the transparent PNG, the white-
background JPG, and a thumbnail. Processing is synchronous by default (the
default model returns in ~1s) with an opt-in async job mode for bulk work.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__
from .config import Settings, get_settings
from .database import SessionLocal, get_session, init_db
from .engine import BackgroundRemover
from .models import AssetStatus, ImageAsset
from .schemas import AssetList, AssetOut, HealthOut
from .service import NukkiService, ProcessingError
from .storage import LocalStorage, build_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("nukki")

WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    engine = BackgroundRemover(settings)
    storage = build_storage(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.storage = storage
    app.state.service = NukkiService(settings, engine, storage)

    # Warm the models so the first real request is fast (and weights download
    # now rather than mid-request).
    if settings.warmup_on_start:
        log.info("warming models: %s", settings.preload_models())
        await run_in_threadpool(engine.warmup, settings.preload_models())
        log.info("ready; models loaded: %s", engine.loaded_models)
    yield


app = FastAPI(title="누끼 Auto-Registration Service", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- dependencies ----------------------------------------------------------
def get_service() -> NukkiService:
    return app.state.service


def get_settings_dep() -> Settings:
    return app.state.settings


# --- background job --------------------------------------------------------
def _process_in_background(asset_id: str, data: bytes, model: str | None) -> None:
    """Runs after the response is sent, in its own DB session."""
    db = SessionLocal()
    try:
        app.state.service.process(db, asset_id, data, model=model)
    except Exception:  # noqa: BLE001 — status already recorded on the row
        log.exception("background processing failed for %s", asset_id)
    finally:
        db.close()


# --- routes ----------------------------------------------------------------
@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    s: Settings = app.state.settings
    return HealthOut(
        status="ok",
        default_model=s.rembg_model,
        loaded_models=app.state.engine.loaded_models,
        storage_backend=s.storage_backend,
        version=__version__,
    )


@app.post("/api/v1/images", response_model=AssetOut, status_code=201, tags=["images"])
async def upload_image(
    file: UploadFile = File(..., description="The photo to cut out"),
    external_id: str | None = Form(default=None, description="Your SKU/product id"),
    model: str | None = Form(default=None, description="Override the rembg model"),
    background: bool = Query(default=False, description="Process asynchronously (returns pending)"),
    db: Session = Depends(get_session),
    svc: NukkiService = Depends(get_service),
):
    """Upload a photo. The background is removed and the asset registered.

    * ``background=false`` (default): waits and returns the finished record.
    * ``background=true``: returns a ``pending`` record immediately and
      processes the image in the background; poll ``GET /api/v1/images/{id}``.
    """
    data = await file.read()
    try:
        if background:
            from starlette.background import BackgroundTask

            asset = await run_in_threadpool(
                svc.register, db, data=data, filename=file.filename, external_id=external_id
            )
            # Return the pending record now; Starlette runs the task after.
            return _with_background(
                AssetOut.model_validate(asset),
                BackgroundTask(_process_in_background, asset.id, data, model),
            )

        asset = await run_in_threadpool(
            svc.register_and_process,
            db, data=data, filename=file.filename, external_id=external_id, model=model,
        )
        return AssetOut.model_validate(asset)
    except ProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _with_background(payload: AssetOut, task) -> "JSONResponse":
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=202, content=jsonable_encoder(payload), background=task
    )


@app.get("/api/v1/images/{asset_id}", response_model=AssetOut, tags=["images"])
def get_image(asset_id: str, db: Session = Depends(get_session)) -> AssetOut:
    asset = db.get(ImageAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return AssetOut.model_validate(asset)


@app.get("/api/v1/images", response_model=AssetList, tags=["images"])
def list_images(
    status: AssetStatus | None = Query(default=None),
    external_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_session),
) -> AssetList:
    where = []
    if status is not None:
        where.append(ImageAsset.status == status)
    if external_id is not None:
        where.append(ImageAsset.external_id == external_id)

    total = db.scalar(select(func.count()).select_from(ImageAsset).where(*where)) or 0
    rows = db.scalars(
        select(ImageAsset).where(*where)
        .order_by(ImageAsset.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    return AssetList(total=total, items=[AssetOut.model_validate(r) for r in rows])


@app.delete("/api/v1/images/{asset_id}", status_code=204, tags=["images"])
def delete_image(asset_id: str, db: Session = Depends(get_session)) -> None:
    asset = db.get(ImageAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    db.delete(asset)
    db.commit()


# --- static: generated files (local backend) + test UI ---------------------
def _mount_static() -> None:
    settings = get_settings()
    storage = build_storage(settings)
    if isinstance(storage, LocalStorage):
        app.mount("/files", StaticFiles(directory=str(storage.root)), name="files")
    if WEB_DIR.exists():
        app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


_mount_static()
