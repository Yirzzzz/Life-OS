from __future__ import annotations

import os
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.getenv("LIFEOS_DATABASE_URL", "sqlite:///lifeos.db")

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(planitem)").fetchall()
        }
        if "linked_objective_id" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE planitem ADD COLUMN linked_objective_id INTEGER"
            )


def get_session() -> Session:
    return Session(engine)
