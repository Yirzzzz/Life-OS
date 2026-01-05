from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agent.base import Skill
from app.domain.models import DayLog


class PeriodEntry(BaseModel):
    period: str
    text: str
    tags: List[str] = Field(default_factory=list)


class DayLogInput(BaseModel):
    date: date
    period_entries: List[PeriodEntry] = Field(default_factory=list)
    journal_md: str = ""
    tags: List[str] = Field(default_factory=list)


class DayLogOutput(BaseModel):
    id: int
    date: date


class LogUpsertDayLogSkill(Skill):
    name = "log.upsert_day_log"
    description = "Upsert day log entries and journal."
    input_schema = DayLogInput
    output_schema = DayLogOutput

    def run(self, data: DayLogInput, context: dict) -> DayLogOutput:
        session: Session = context["session"]
        log = session.exec(select(DayLog).where(DayLog.date == data.date)).first()
        payload: List[Dict[str, Any]] = [entry.dict() for entry in data.period_entries]
        if not log:
            log = DayLog(date=data.date, period_entries=payload, journal_md=data.journal_md, tags=data.tags)
        else:
            log.period_entries = payload
            log.journal_md = data.journal_md
            log.tags = data.tags
        session.add(log)
        session.commit()
        session.refresh(log)
        return DayLogOutput(id=log.id, date=log.date)


def get_skill() -> Skill:
    return LogUpsertDayLogSkill()
