"""The background-removal (누끼) engine.

Thin, production-shaped wrapper around `rembg`:

* Lazily loads and **caches** one ONNX session per model name.
* A bounded semaphore limits concurrent inferences so heavy models
  (BiRefNet can spike to ~9 GB RAM) cannot OOM the box.
* Produces the three asset variants a platform typically needs from one
  inference pass:
    - ``cutout``    : transparent PNG (RGBA) — the actual 누끼
    - ``flat``      : subject composited onto a solid background (RGB/JPEG),
                      which most marketplaces require (e.g. white background)
    - ``thumbnail`` : small transparent preview
* Reports a ``foreground_ratio`` quality signal so the caller can flag
  suspicious results (e.g. a near-empty or near-full mask).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from PIL import Image

from .config import Settings

# rembg is imported lazily inside the engine so that importing this module
# (e.g. for type hints or tests that monkeypatch it) does not pay the
# onnxruntime import cost until the engine is actually constructed.


@dataclass
class CutoutResult:
    """Outputs + metadata from a single removal."""

    original: Image.Image  # RGB, possibly downscaled to max_output_px
    cutout: Image.Image  # RGBA, transparent background
    flat: Image.Image  # RGB, subject on a solid background
    thumbnail: Image.Image  # RGBA preview
    model: str
    width: int
    height: int
    elapsed_ms: int
    foreground_ratio: float  # fraction of opaque pixels in the cutout (0..1)

    @property
    def is_suspicious(self) -> bool:
        """Heuristic: an almost-empty or almost-full mask usually means the
        model failed to find a clean subject. The caller can warn on this."""
        return self.foreground_ratio < 0.01 or self.foreground_ratio > 0.995


class BackgroundRemover:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._sessions: dict[str, object] = {}
        self._session_lock = threading.Lock()
        self._gpu = threading.BoundedSemaphore(settings.max_concurrency)
        self._flat_bg = settings.flat_bg_rgb()

        # Imported here (not at module top) — see note above.
        import onnxruntime as ort
        from rembg import new_session, remove

        self._new_session = new_session
        self._remove = remove

        self._sess_opts = None
        if settings.onnx_threads > 0:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = settings.onnx_threads
            opts.inter_op_num_threads = 1
            self._sess_opts = opts

    # -- session management -------------------------------------------------
    def _session(self, model: str):
        """Return a cached rembg session for ``model``, loading it once."""
        sess = self._sessions.get(model)
        if sess is not None:
            return sess
        with self._session_lock:
            sess = self._sessions.get(model)
            if sess is None:
                sess = self._new_session(model, sess_opts=self._sess_opts)
                self._sessions[model] = sess
        return sess

    def warmup(self, models: list[str]) -> None:
        """Pre-load the given models (downloads weights if missing)."""
        for m in models:
            self._session(m)

    @property
    def loaded_models(self) -> list[str]:
        return sorted(self._sessions.keys())

    # -- core ---------------------------------------------------------------
    def cutout(self, image: Image.Image, model: str | None = None) -> CutoutResult:
        """Remove the background from a PIL image and build the asset set."""
        model = model or self._settings.rembg_model
        session = self._session(model)

        original = self._prepare_original(image)

        t0 = time.perf_counter()
        with self._gpu:
            cut = self._remove(
                original,
                session=session,
                alpha_matting=self._settings.alpha_matting,
                post_process_mask=self._settings.post_process_mask,
            )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if cut.mode != "RGBA":
            cut = cut.convert("RGBA")

        flat = self._composite(cut, self._flat_bg)
        thumb = self._thumbnail(cut)
        ratio = self._foreground_ratio(cut)

        return CutoutResult(
            original=original,
            cutout=cut,
            flat=flat,
            thumbnail=thumb,
            model=model,
            width=cut.width,
            height=cut.height,
            elapsed_ms=elapsed_ms,
            foreground_ratio=ratio,
        )

    # -- helpers ------------------------------------------------------------
    def _prepare_original(self, image: Image.Image) -> Image.Image:
        img = image
        # Respect EXIF orientation so phone photos are not sideways.
        try:
            from PIL import ImageOps

            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = img.convert("RGB")
        cap = self._settings.max_output_px
        if cap and max(img.size) > cap:
            img = self._downscale(img, cap)
        return img

    @staticmethod
    def _downscale(img: Image.Image, longest: int) -> Image.Image:
        w, h = img.size
        if max(w, h) <= longest:
            return img
        scale = longest / max(w, h)
        return img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)

    def _composite(self, cutout: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
        canvas = Image.new("RGB", cutout.size, bg)
        canvas.paste(cutout, mask=cutout.split()[-1])  # use alpha as mask
        return canvas

    def _thumbnail(self, cutout: Image.Image) -> Image.Image:
        px = self._settings.thumbnail_px
        if not px:
            return cutout
        thumb = cutout.copy()
        thumb.thumbnail((px, px), Image.LANCZOS)
        return thumb

    @staticmethod
    def _foreground_ratio(cutout: Image.Image) -> float:
        alpha = cutout.split()[-1]
        # Average alpha / 255 == fraction of the frame that is kept.
        hist = alpha.histogram()
        total = sum(hist)
        if not total:
            return 0.0
        weighted = sum(i * c for i, c in enumerate(hist))
        return weighted / (255.0 * total)
