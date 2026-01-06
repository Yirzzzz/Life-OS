from __future__ import annotations

import os
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.getenv("LIFEOS_DATABASE_URL", "sqlite:///lifeos.db")

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    from app.domain import models  # noqa: F401
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(planitem)").fetchall()
        }
        if "linked_objective_id" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE planitem ADD COLUMN linked_objective_id INTEGER"
            )
        habit_columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(habit)").fetchall()
        }
        if "start_date" not in habit_columns:
            conn.exec_driver_sql("ALTER TABLE habit ADD COLUMN start_date DATE")
        conn.exec_driver_sql(
            "UPDATE habit SET start_date = '2026-01-01' WHERE start_date IS NULL"
        )


def get_session() -> Session:
    return Session(engine)
