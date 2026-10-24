"""API + pipeline tests.

The ONNX engine is replaced by a deterministic ``FakeRemover`` (injected via a
FastAPI dependency override) so the suite is fast and hermetic — no model
downloads, no onnxruntime inference — while still exercising the real storage,
database registration, image encoding, and HTTP layers.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

# Point the app at a throwaway data dir and skip model warmup BEFORE importing
# the app, so app.database binds its engine to this temp SQLite file.
_TMP = Path(tempfile.mkdtemp(prefix="nukki-test-"))
import os  # noqa: E402

os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["WARMUP_ON_START"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.engine import CutoutResult  # noqa: E402
from app.service import NukkiService  # noqa: E402
from app.storage import build_storage  # noqa: E402


class FakeRemover:
    """Deterministic stand-in for BackgroundRemover: an opaque centre square."""

    def cutout(self, image: Image.Image, model=None) -> CutoutResult:
        img = image.convert("RGB")
        w, h = img.size
        cut = Image.new("RGBA", img.size, (0, 0, 0, 0))
        box = Image.new("RGBA", (w // 2, h // 2), (10, 120, 200, 255))
        cut.paste(box, (w // 4, h // 4))
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(box.convert("RGB"), (w // 4, h // 4))
        thumb = cut.copy(); thumb.thumbnail((64, 64))
        return CutoutResult(
            original=img, cutout=cut, flat=flat, thumbnail=thumb,
            model=model or "fake", width=w, height=h, elapsed_ms=7,
            foreground_ratio=0.25,
        )


@pytest.fixture(scope="module")
def client():
    settings = get_settings()
    fake_service = NukkiService(settings, FakeRemover(), build_storage(settings))
    main.app.dependency_overrides[main.get_service] = lambda: fake_service
    with TestClient(main.app) as c:
        # Replace the real-engine service created in lifespan so the async
        # background path (which reads app.state.service) also uses the fake.
        main.app.state.service = fake_service
        yield c
    main.app.dependency_overrides.clear()


def _png_bytes(color=(200, 30, 30), size=(240, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# --- tests -----------------------------------------------------------------
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_upload_processes_and_registers(client):
    r = client.post(
        "/api/v1/images",
        files={"file": ("product.png", _png_bytes(), "image/png")},
        data={"external_id": "SKU-1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "done"
    assert body["external_id"] == "SKU-1"
    assert body["width"] == 240 and body["height"] == 180
    assert body["cutout_url"].endswith("cutout.png")
    assert body["foreground_ratio"] == 0.25
    assert body["needs_review"] is False

    got = client.get(f"/api/v1/images/{body['id']}")
    assert got.status_code == 200
    listed = client.get("/api/v1/images", params={"external_id": "SKU-1"})
    assert listed.json()["total"] == 1


def test_cutout_file_is_served_and_transparent(client):
    r = client.post("/api/v1/images", files={"file": ("p.png", _png_bytes(), "image/png")})
    cutout_url = r.json()["cutout_url"]
    rel = cutout_url.split("/files/", 1)[1]
    served = client.get(f"/files/{rel}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    im = Image.open(io.BytesIO(served.content))
    assert im.mode == "RGBA"
    # Corner pixel is transparent, centre is opaque.
    assert im.getpixel((0, 0))[3] == 0
    assert im.getpixel((im.width // 2, im.height // 2))[3] == 255


def test_rejects_non_image_and_records_failure(client):
    r = client.post(
        "/api/v1/images",
        files={"file": ("notes.txt", b"this is not an image", "text/plain")},
    )
    assert r.status_code == 422
    assert "image" in r.json()["detail"].lower()
    # The upload must be registered as failed, not left stuck at "processing".
    failed = client.get("/api/v1/images", params={"status": "failed"})
    assert failed.json()["total"] >= 1


def test_404_and_delete(client):
    assert client.get("/api/v1/images/does-not-exist").status_code == 404
    r = client.post("/api/v1/images", files={"file": ("p.png", _png_bytes(), "image/png")})
    aid = r.json()["id"]
    assert client.delete(f"/api/v1/images/{aid}").status_code == 204
    assert client.get(f"/api/v1/images/{aid}").status_code == 404


def test_background_mode_eventually_done(client):
    r = client.post(
        "/api/v1/images?background=true",
        files={"file": ("p.png", _png_bytes(), "image/png")},
    )
    assert r.status_code == 202
    aid = r.json()["id"]
    # TestClient runs the background task synchronously after the response,
    # so by now it is finished.
    got = client.get(f"/api/v1/images/{aid}").json()
    assert got["status"] == "done"
    assert got["cutout_url"]
