from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Tuple

from sqlmodel import Session, select

from app.domain.models import HabitTemplate, PlanItem, DailyPlan


def _date_range(end_date: date, days: int) -> List[date]:
    return [end_date - timedelta(days=offset) for offset in range(days)]


def habit_metrics(session: Session, habit: HabitTemplate, end_date: date) -> Dict[str, int]:
    dates = _date_range(end_date, 30)
    plan_ids = session.exec(
        select(DailyPlan.id).where(DailyPlan.date.in_(dates))
    ).all()
    if not plan_ids:
        return {
            "completed_30": 0,
            "total_30": 0,
            "completed_14": 0,
            "completed_7": 0,
            "streak": 0,
            "max_gap": 30,
        }

    items = session.exec(
        select(PlanItem).where(
            PlanItem.daily_plan_id.in_(plan_ids),
            PlanItem.linked_habit_id == habit.id,
        )
    ).all()

    completed_dates = sorted({item.completed_at.date() for item in items if item.completed_at})
    completed_last = [d for d in completed_dates if d in dates]
    completed_30 = len(completed_last)

    completed_14 = len([d for d in completed_last if d >= end_date - timedelta(days=13)])
    completed_7 = len([d for d in completed_last if d >= end_date - timedelta(days=6)])

    streak = 0
    for d in _date_range(end_date, 30):
        if d in completed_last:
            streak += 1
        else:
            break

    max_gap = 0
    if dates:
        last_done = None
        gap = 0
        for d in dates:
            if d in completed_last:
                if last_done is not None:
                    gap = (last_done - d).days - 1
                    max_gap = max(max_gap, gap)
                last_done = d
        if last_done is None:
            max_gap = 30

    total_30 = len(dates)
    return {
        "completed_30": completed_30,
        "total_30": total_30,
        "completed_14": completed_14,
        "completed_7": completed_7,
        "streak": streak,
        "max_gap": max_gap,
    }


def habit_preferred_period(
    session: Session, habit: HabitTemplate, end_date: date
) -> Tuple[str, int]:
    dates = _date_range(end_date, 30)
    plan_ids = session.exec(
        select(DailyPlan.id).where(DailyPlan.date.in_(dates))
    ).all()
    if not plan_ids:
        return ("unknown", 0)

    items = session.exec(
        select(PlanItem).where(
            PlanItem.daily_plan_id.in_(plan_ids),
            PlanItem.linked_habit_id == habit.id,
            PlanItem.completed_at.is_not(None),
        )
    ).all()
    counts: Dict[str, int] = {}
    for item in items:
        hour = item.completed_at.hour if item.completed_at else 0
        period = "morning" if hour < 11 else "afternoon" if hour < 18 else "evening"
        counts[period] = counts.get(period, 0) + 1
    if not counts:
        return ("unknown", 0)
    best = max(counts.items(), key=lambda x: x[1])
    return best
