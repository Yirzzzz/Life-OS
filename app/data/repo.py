from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlmodel import Session, select

from app.domain.models import DailyPlan, DayLog, PlanItem, Settings


def get_settings(session: Session) -> Optional[Settings]:
    return session.exec(select(Settings).where(Settings.id == 1)).first()


def get_daily_plan(session: Session, target_date: date) -> Optional[DailyPlan]:
    return session.exec(select(DailyPlan).where(DailyPlan.date == target_date)).first()


def get_plan_items(session: Session, plan_id: int) -> List[PlanItem]:
    return session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan_id)).all()


def get_day_log(session: Session, target_date: date) -> Optional[DayLog]:
    return session.exec(select(DayLog).where(DayLog.date == target_date)).first()
