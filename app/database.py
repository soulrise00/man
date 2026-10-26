"""SQLAlchemy engine + session factory.

Defaults to a file-backed SQLite database inside ``data/`` so the service runs
with no external dependencies. Point ``DATABASE_URL`` at Postgres for
production (e.g. ``postgresql+psycopg://user:pass@host/db``).
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_url = _settings.resolved_database_url()

# check_same_thread=False: FastAPI serves sync endpoints across a threadpool.
_connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}
engine = create_engine(_url, connect_args=_connect_args, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    # Import models so their tables are registered on Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
