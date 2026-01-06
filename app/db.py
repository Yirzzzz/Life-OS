from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.getenv("LIFEOS_DATABASE_URL", "sqlite:///lifeos.db")

engine = create_engine(DATABASE_URL, echo=False)


def _sqlite_path() -> Optional[str]:
    url = make_url(DATABASE_URL)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        return os.path.abspath(url.database)
    return None


def database_exists() -> bool:
    path = _sqlite_path()
    if path:
        return os.path.exists(path)
    return False


def _run_alembic_upgrade() -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


def migrate_db() -> None:
    _run_alembic_upgrade()


def init_db() -> None:
    if database_exists():
        return
    _run_alembic_upgrade()


def get_session() -> Session:
    return Session(engine)
