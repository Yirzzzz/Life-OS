from __future__ import annotations

from datetime import date
from typing import Dict, List, Tuple

from sqlmodel import Session, select

from app.domain.models import DayLog, DailyPlan, Habit, PlanItem


def _month_range(year: int, month: int) -> Tuple[date, date]:
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def _year_range(year: int) -> Tuple[date, date]:
    return date(year, 1, 1), date(year + 1, 1, 1)


def _collect_period_counts(logs: List[DayLog]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for log in logs:
        for entry in log.period_entries:
            period = entry.get("period", "unknown")
            counts[period] = counts.get(period, 0) + 1
    return counts


def _top_habits(session: Session, plan_ids: List[int]) -> List[Tuple[str, int]]:
    items = session.exec(
        select(PlanItem).where(
            PlanItem.daily_plan_id.in_(plan_ids),
            PlanItem.linked_habit_id.is_not(None),
            PlanItem.completed_at.is_not(None),
        )
    ).all()
    counts: Dict[int, int] = {}
    for item in items:
        if item.linked_habit_id:
            counts[item.linked_habit_id] = counts.get(item.linked_habit_id, 0) + 1
    if not counts:
        return []
    habits = session.exec(select(Habit).where(Habit.id.in_(counts.keys()))).all()
    name_map = {habit.id: habit.title for habit in habits}
    scored = [(name_map.get(hid, f"Habit {hid}"), count) for hid, count in counts.items()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:5]


def _narrative(title: str, completion_rate: float, active_days: int) -> str:
    if completion_rate >= 0.8:
        tone = "本月执行力很稳定，节奏感强。"
    elif completion_rate >= 0.5:
        tone = "本月执行节奏尚可，仍有提升空间。"
    else:
        tone = "本月执行偏弱，建议降低复杂度并聚焦核心目标。"
    return f"{title}\n\n{tone} 活跃天数 {active_days} 天，完成率 {completion_rate:.0%}。"


def generate_monthly_review(session: Session, year: int, month: int) -> Dict[str, object]:
    start, end = _month_range(year, month)
    plans = session.exec(select(DailyPlan).where(DailyPlan.date >= start, DailyPlan.date < end)).all()
    plan_ids = [plan.id for plan in plans]
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id.in_(plan_ids))).all() if plan_ids else []
    total = len(items)
    completed = len([item for item in items if item.completed_at])
    completion_rate = completed / total if total else 0

    logs = session.exec(select(DayLog).where(DayLog.date >= start, DayLog.date < end)).all()
    active_days = len({log.date for log in logs}) or len({plan.date for plan in plans})
    period_counts = _collect_period_counts(logs)
    top_habits = _top_habits(session, plan_ids)

    narrative = _narrative(f"{year}年{month}月复盘", completion_rate, active_days)

    return {
        "title": f"{year}-{month:02d}",
        "total_items": total,
        "completed_items": completed,
        "completion_rate": completion_rate,
        "top_habits": top_habits,
        "active_days": active_days,
        "period_counts": period_counts,
        "narrative": narrative,
    }


def generate_yearly_review(session: Session, year: int) -> Dict[str, object]:
    start, end = _year_range(year)
    plans = session.exec(select(DailyPlan).where(DailyPlan.date >= start, DailyPlan.date < end)).all()
    plan_ids = [plan.id for plan in plans]
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id.in_(plan_ids))).all() if plan_ids else []
    total = len(items)
    completed = len([item for item in items if item.completed_at])
    completion_rate = completed / total if total else 0

    logs = session.exec(select(DayLog).where(DayLog.date >= start, DayLog.date < end)).all()
    active_days = len({log.date for log in logs}) or len({plan.date for plan in plans})
    period_counts = _collect_period_counts(logs)
    top_habits = _top_habits(session, plan_ids)

    narrative = _narrative(f"{year}年年度复盘", completion_rate, active_days)

    return {
        "title": f"{year}",
        "total_items": total,
        "completed_items": completed,
        "completion_rate": completion_rate,
        "top_habits": top_habits,
        "active_days": active_days,
        "period_counts": period_counts,
        "narrative": narrative,
    }
