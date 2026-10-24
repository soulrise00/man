#!/usr/bin/env python
"""Bulk-ingest a folder of photos through the exact same pipeline the API uses.

Every image is cut out, its variants are stored, and a row is registered in the
database — identical to uploading each one to ``POST /api/v1/images``. Use this
to back-fill an existing product catalogue.

    python -m scripts.batch /path/to/photos --model isnet-general-use

Add ``--watch`` to keep running and process new files as they appear.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make `app` importable when run as a file.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.engine import BackgroundRemover  # noqa: E402
from app.service import NukkiService, ProcessingError  # noqa: E402
from app.storage import build_storage  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


def iter_images(folder: Path):
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            yield p


def process_one(svc: NukkiService, path: Path, model: str | None) -> None:
    db = SessionLocal()
    try:
        data = path.read_bytes()
        asset = svc.register_and_process(
            db, data=data, filename=path.name, external_id=path.stem, model=model
        )
        flag = "  ⚠ review" if asset.needs_review else ""
        print(f"  ✓ {path.name:<32} {asset.processing_ms:>5}ms  fg={asset.foreground_ratio:.3f}{flag}")
    except ProcessingError as exc:
        print(f"  ✗ {path.name:<32} {exc}")
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch 누끼 processing")
    ap.add_argument("folder", type=Path, help="Folder of images to process")
    ap.add_argument("--model", default=None, help="rembg model (default: REMBG_MODEL)")
    ap.add_argument("--watch", action="store_true", help="Keep watching for new files")
    ap.add_argument("--interval", type=float, default=5.0, help="Watch poll interval (s)")
    args = ap.parse_args()

    if not args.folder.is_dir():
        ap.error(f"{args.folder} is not a directory")

    settings = get_settings()
    init_db()
    engine = BackgroundRemover(settings)
    engine.warmup([args.model or settings.rembg_model])
    svc = NukkiService(settings, engine, build_storage(settings))

    seen: set[Path] = set()

    def sweep() -> int:
        n = 0
        for img in iter_images(args.folder):
            if img in seen:
                continue
            seen.add(img)
            process_one(svc, img, args.model)
            n += 1
        return n

    print(f"Processing {args.folder} with model={args.model or settings.rembg_model}")
    count = sweep()
    print(f"Done: {count} image(s).")

    if args.watch:
        print(f"Watching for new files (every {args.interval}s). Ctrl-C to stop.")
        try:
            while True:
                time.sleep(args.interval)
                sweep()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
