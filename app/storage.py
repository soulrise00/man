"""Pluggable storage backends for the generated assets.

Two backends share one interface so the pipeline code never changes:

* ``LocalStorage`` — writes under ``data/storage`` and serves them back via the
  app's ``/files`` static mount. The default; zero external dependencies.
* ``S3Storage``    — uploads to S3 / MinIO / R2 and returns public URLs.

``save(key, data, content_type)`` persists bytes and returns a public URL.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from .config import Settings


class Storage(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes, content_type: str) -> str:
        """Persist ``data`` at ``key`` and return a publicly reachable URL."""

    @abstractmethod
    def url_for(self, key: str) -> str:
        ...


class LocalStorage(Storage):
    def __init__(self, settings: Settings):
        self._root = (settings.data_dir / "storage").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._base = settings.public_base_url.rstrip("/")

    def save(self, key: str, data: bytes, content_type: str) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename, so a reader never sees a half file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return self.url_for(key)

    def url_for(self, key: str) -> str:
        return f"{self._base}/files/{key}"

    @property
    def root(self) -> Path:
        return self._root


class S3Storage(Storage):
    def __init__(self, settings: Settings):
        import boto3

        if not settings.s3_bucket:
            raise ValueError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set")
        self._bucket = settings.s3_bucket
        self._prefix = settings.s3_prefix.strip("/")
        client_kwargs: dict = {}
        if settings.s3_region:
            client_kwargs["region_name"] = settings.s3_region
        if settings.s3_endpoint_url:
            client_kwargs["endpoint_url"] = settings.s3_endpoint_url
        self._client = boto3.client("s3", **client_kwargs)
        self._public_base = (
            settings.s3_public_base_url.rstrip("/")
            or settings.s3_endpoint_url.rstrip("/")
            or f"https://{self._bucket}.s3.amazonaws.com"
        )

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def save(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._full_key(key),
            Body=data,
            ContentType=content_type,
        )
        return self.url_for(key)

    def url_for(self, key: str) -> str:
        return f"{self._public_base}/{self._full_key(key)}"


def build_storage(settings: Settings) -> Storage:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalStorage(settings)
    if backend == "s3":
        return S3Storage(settings)
    raise ValueError(f"Unknown STORAGE_BACKEND: {settings.storage_backend!r}")
