from __future__ import annotations

import os
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.getenv("LIFEOS_DATABASE_URL", "sqlite:///lifeos.db")

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
