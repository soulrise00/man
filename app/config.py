"""Application configuration, loaded from environment variables / .env.

All settings have production-sensible defaults so the service also runs with a
bare `uvicorn app.main:app`. See `.env.example` for the full list.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Service ---
    app_name: str = "nukki-service"
    data_dir: Path = Path("data")

    # --- Background-removal engine ---
    # Default is the CPU sweet-spot proven in the benchmark (BENCHMARK.md):
    # isnet-general-use — ~1.2s/image, ~1.7GB RAM, matches heavy models on
    # product shots. Switch to "birefnet-general" when a GPU is available.
    rembg_model: str = Field(default="isnet-general-use")
    # Extra models to pre-warm at startup (comma-separated), so the first
    # request for them is not slowed by lazy loading.
    rembg_preload: str = Field(default="")
    # Cap simultaneous inferences to protect RAM (BiRefNet can spike to ~9GB).
    max_concurrency: int = Field(default=2, ge=1)
    # Pre-load models at startup (downloads weights). Disable in tests.
    warmup_on_start: bool = Field(default=True)
    # Threads per ONNX inference. 0 => let onnxruntime decide.
    onnx_threads: int = Field(default=0, ge=0)
    # Alpha matting refines fuzzy edges (hair/fur) at a speed cost. Off by
    # default — product/object shots rarely need it.
    alpha_matting: bool = Field(default=False)
    # Post-process the mask (rembg's morphology cleanup) to drop stray islands.
    post_process_mask: bool = Field(default=True)

    # --- Outputs ---
    # Longest side the stored assets are capped to (0 = keep original size).
    max_output_px: int = Field(default=2000, ge=0)
    thumbnail_px: int = Field(default=400, ge=0)
    # Background color for the composited "flat" JPG that many marketplaces
    # require (e.g. Coupang/Amazon white background). RGB tuple as "R,G,B".
    flat_background: str = Field(default="255,255,255")
    # JPEG quality for original + flat outputs.
    jpeg_quality: int = Field(default=90, ge=1, le=100)

    # --- Upload limits ---
    max_upload_mb: int = Field(default=25, ge=1)

    # --- Storage ---
    storage_backend: str = Field(default="local")  # "local" | "s3"
    public_base_url: str = Field(default="http://localhost:8000")
    # S3 (only used when storage_backend == "s3")
    s3_bucket: str = Field(default="")
    s3_prefix: str = Field(default="nukki")
    s3_region: str = Field(default="")
    s3_endpoint_url: str = Field(default="")  # for MinIO / R2 / etc.
    s3_public_base_url: str = Field(default="")  # CDN / bucket public URL

    # --- Database ---
    database_url: str = Field(default="")  # empty => sqlite in data_dir

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    def flat_bg_rgb(self) -> tuple[int, int, int]:
        parts = [int(x) for x in self.flat_background.split(",")]
        if len(parts) != 3:
            raise ValueError("flat_background must be 'R,G,B'")
        return parts[0], parts[1], parts[2]

    def preload_models(self) -> list[str]:
        names = {self.rembg_model}
        names.update(m.strip() for m in self.rembg_preload.split(",") if m.strip())
        return sorted(names)


@lru_cache
def get_settings() -> Settings:
    return Settings()
