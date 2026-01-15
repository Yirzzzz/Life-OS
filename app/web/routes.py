from __future__ import annotations

from datetime import date, datetime, timedelta
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import markdown
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.agent.executor import Executor
from app.config import APP_VERSION, DEVELOPER_ID
from app.domain.models import (
    DayLog,
    DailyPlan,
    Goal,
    HabitTemplate,
    Milestone,
    PlanItem,
    PlanItemSuppression,
    ShortTermObjective,
    Settings,
    Suggestion,
    SuggestionDecision,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")
templates.env.globals.update(app_version=APP_VERSION, developer_id=DEVELOPER_ID)


def _get_session(request: Request) -> Session:
    return request.app.state.session()


def _get_executor(request: Request) -> Executor:
    return request.app.state.executor


def _get_periods(session: Session) -> List[str]:
    return ["morning", "afternoon", "evening"]


def _period_labels() -> Dict[str, str]:
    return {
        "morning": "period.morning",
        "afternoon": "period.afternoon",
        "evening": "period.evening",
    }


def _get_locale(request: Request) -> str:
    value = request.cookies.get("lifeos_locale", "")
    if value in {"zh", "en"}:
        return value
    return "zh"


def _ensure_day_log(session: Session, target_date: date) -> DayLog:
    log = session.exec(select(DayLog).where(DayLog.date == target_date)).first()
    if not log:
        log = DayLog(
            date=target_date,
            period_entries=[
                {"period": "morning", "text": "", "tags": []},
                {"period": "afternoon", "text": "", "tags": []},
                {"period": "evening", "text": "", "tags": []},
            ],
            journal_md="",
            tags=[],
        )
        session.add(log)
        session.commit()
        session.refresh(log)
    return log


def _ensure_daily_plan(session: Session, target_date: date) -> DailyPlan:
    plan = session.exec(select(DailyPlan).where(DailyPlan.date == target_date)).first()
    if not plan:
        plan = DailyPlan(date=target_date)
        session.add(plan)
        session.commit()
        session.refresh(plan)
    return plan


def _template_included(template: HabitTemplate, target_date: date) -> bool:
    if not template.active:
        return False
    if template.frequency == "daily":
        return True
    if template.frequency == "weekly":
        return True
    return True


def _week_start(target_date: date) -> date:
    return target_date - timedelta(days=target_date.weekday())


def _week_end(target_date: date) -> date:
    return _week_start(target_date) + timedelta(days=6)


def _month_start(target_date: date) -> date:
    return date(target_date.year, target_date.month, 1)


def _shift_month(target_date: date, delta: int) -> date:
    year = target_date.year + (target_date.month - 1 + delta) // 12
    month = (target_date.month - 1 + delta) % 12 + 1
    return date(year, month, 1)


def _month_end(target_date: date) -> date:
    return _shift_month(target_date, 1) - timedelta(days=1)


def _heatmap_color(rate: Optional[float]) -> str:
    if rate is None:
        return "bg-slate-100"
    if rate >= 0.67:
        return "bg-emerald-500"
    if rate >= 0.34:
        return "bg-emerald-300"
    if rate > 0:
        return "bg-emerald-100"
    return "bg-slate-100"


def _habit_progress_summary(
    session: Session, anchor_date: date, weeks: int = 6
) -> Dict[str, Any]:
    habits = session.exec(
        select(HabitTemplate).where(HabitTemplate.active == True)  # noqa: E712
    ).all()
    if not habits:
        return {
            "weeks": [],
            "weeks_count": weeks,
            "anchor_month_label": anchor_date.strftime("%Y-%m"),
            "week_rate_pct": 0,
            "week_completed_days": 0,
            "week_eligible_days": 0,
            "month_rate_pct": 0,
            "month_completed_days": 0,
            "month_eligible_days": 0,
            "since_days": 0,
            "since_rate_pct": 0,
        }

    habit_map = {habit.id: habit for habit in habits}
    habit_ids = list(habit_map.keys())

    end_week_start = _week_start(anchor_date)
    start_week_start = end_week_start - timedelta(days=(weeks - 1) * 7)
    end_date = end_week_start + timedelta(days=6)
    month_start = _month_start(anchor_date)
    earliest_start = min(
        (habit.start_date or date(2026, 1, 1) for habit in habits),
        default=date(2026, 1, 1),
    )
    stats_start = min(start_week_start, month_start, earliest_start)

    plans = session.exec(
        select(DailyPlan).where(DailyPlan.date >= stats_start, DailyPlan.date <= end_date)
    ).all()
    plan_by_id = {plan.id: plan.date for plan in plans}
    plan_ids = list(plan_by_id.keys())

    daily_has: Dict[int, set[date]] = {}
    daily_done: Dict[int, set[date]] = {}
    weekly_day_has: Dict[int, set[date]] = {}
    weekly_done: Dict[int, set[date]] = {}

    if plan_ids and habit_ids:
        items = session.exec(
            select(PlanItem).where(
                PlanItem.daily_plan_id.in_(plan_ids),
                PlanItem.linked_habit_id.in_(habit_ids),
            )
        ).all()
        for item in items:
            plan_date = plan_by_id.get(item.daily_plan_id)
            if not plan_date:
                continue
            habit = habit_map.get(item.linked_habit_id)
            if not habit:
                continue
            start_date = habit.start_date or date(2026, 1, 1)
            if plan_date < start_date:
                continue
            if habit.frequency == "weekly":
                week_key = _week_start(plan_date)
                weekly_day_has.setdefault(habit.id, set()).add(plan_date)
                if item.completed_at:
                    weekly_done.setdefault(habit.id, set()).add(week_key)
            else:
                daily_has.setdefault(habit.id, set()).add(plan_date)
                if item.completed_at:
                    daily_done.setdefault(habit.id, set()).add(plan_date)

    daily_stats: Dict[date, Dict[str, int]] = {}
    heatmap_cells: List[Dict[str, Any]] = []
    for offset in range((end_date - stats_start).days + 1):
        current_date = stats_start + timedelta(days=offset)
        eligible = 0
        completed = 0
        for habit in habits:
            start_date = habit.start_date or date(2026, 1, 1)
            if current_date < start_date:
                continue
            if habit.frequency == "weekly":
                week_key = _week_start(current_date)
                if current_date not in weekly_day_has.get(habit.id, set()):
                    continue
                eligible += 1
                if week_key in weekly_done.get(habit.id, set()):
                    completed += 1
            else:
                if current_date not in daily_has.get(habit.id, set()):
                    continue
                eligible += 1
                if current_date in daily_done.get(habit.id, set()):
                    completed += 1
        rate = (completed / eligible) if eligible else None
        daily_stats[current_date] = {"eligible": eligible, "completed": completed}
        heatmap_cells.append(
            {
                "date": current_date.isoformat(),
                "rate": rate,
                "color": _heatmap_color(rate),
            }
        )

    weeks_rows: List[List[Dict[str, Any]]] = []
    heatmap_cells_trimmed = heatmap_cells[
        (start_week_start - stats_start).days : (end_date - stats_start).days + 1
    ]
    for i in range(0, len(heatmap_cells_trimmed), 7):
        weeks_rows.append(heatmap_cells_trimmed[i : i + 7])

    def _period_summary(dates: List[date]) -> tuple[int, int, int]:
        completed_days = 0
        eligible_days = 0
        for d in dates:
            stats = daily_stats.get(d)
            if not stats or stats["eligible"] == 0:
                continue
            eligible_days += 1
            if stats["completed"] == stats["eligible"]:
                completed_days += 1
        rate_pct = int(round((completed_days / eligible_days) * 100)) if eligible_days else 0
        return rate_pct, completed_days, eligible_days

    week_start = _week_start(anchor_date)
    week_dates = [week_start + timedelta(days=i) for i in range(7)]
    week_rate_pct, week_completed_days, week_eligible_days = _period_summary(week_dates)

    month_dates = [
        month_start + timedelta(days=i)
        for i in range((anchor_date - month_start).days + 1)
    ]
    month_rate_pct, month_completed_days, month_eligible_days = _period_summary(month_dates)

    since_dates = [
        earliest_start + timedelta(days=i)
        for i in range((anchor_date - earliest_start).days + 1)
    ]
    since_rate_sum = 0.0
    since_rate_days = 0
    since_completed_days = 0
    for d in since_dates:
        stats = daily_stats.get(d)
        if not stats or stats["eligible"] == 0:
            continue
        rate = stats["completed"] / stats["eligible"]
        since_rate_sum += rate
        since_rate_days += 1
        if stats["completed"] == stats["eligible"]:
            since_completed_days += 1
    since_rate_pct = (
        int(round((since_rate_sum / since_rate_days) * 100)) if since_rate_days else 0
    )

    return {
        "weeks": weeks_rows,
        "weeks_count": weeks,
        "anchor_month_label": anchor_date.strftime("%Y-%m"),
        "week_rate_pct": week_rate_pct,
        "week_completed_days": week_completed_days,
        "week_eligible_days": week_eligible_days,
        "month_rate_pct": month_rate_pct,
        "month_completed_days": month_completed_days,
        "month_eligible_days": month_eligible_days,
        "since_days": since_completed_days,
        "since_rate_pct": since_rate_pct,
    }


def _daily_plan_completion(
    session: Session,
    start: date,
    end: date,
    habit_by_id: Dict[int, HabitTemplate],
) -> tuple[int, int]:
    plans = session.exec(
        select(DailyPlan).where(DailyPlan.date >= start, DailyPlan.date <= end)
    ).all()
    if not plans:
        return 0, 0
    plan_ids = [plan.id for plan in plans]
    items = session.exec(
        select(PlanItem).where(
            PlanItem.daily_plan_id.in_(plan_ids),
            PlanItem.linked_objective_id.is_(None),
        )
    ).all()
    filtered = []
    for item in items:
        if item.linked_habit_id:
            habit = habit_by_id.get(item.linked_habit_id)
            if habit and habit.frequency == "weekly":
                continue
        filtered.append(item)
    completed = len([item for item in filtered if item.completed_at])
    return completed, len(filtered)


def _weekly_plan_completion(
    session: Session,
    habits: List[HabitTemplate],
    week_start: date,
    week_end: date,
) -> tuple[int, int, set[int]]:
    weekly_habits = [
        habit
        for habit in habits
        if habit.frequency == "weekly"
        and habit.active
        and (habit.start_date or date(2026, 1, 1)) <= week_end
    ]
    if not weekly_habits:
        return 0, 0, set()
    weekly_ids = [habit.id for habit in weekly_habits]
    week_plans = session.exec(
        select(DailyPlan).where(DailyPlan.date >= week_start, DailyPlan.date <= week_end)
    ).all()
    plan_ids = [plan.id for plan in week_plans]
    completed_ids: set[int] = set()
    if plan_ids:
        week_items = session.exec(
            select(PlanItem).where(
                PlanItem.daily_plan_id.in_(plan_ids),
                PlanItem.linked_habit_id.in_(weekly_ids),
                PlanItem.completed_at.is_not(None),
            )
        ).all()
        completed_ids = {item.linked_habit_id for item in week_items if item.linked_habit_id}
    done = 0
    for habit in weekly_habits:
        target = habit.target_per_week if habit.target_per_week else 1
        completed_count = 1 if habit.id in completed_ids else 0
        if completed_count >= target:
            done += 1
    return done, len(weekly_habits), completed_ids


def _weekly_plan_month_completion(
    session: Session,
    habits: List[HabitTemplate],
    anchor_date: date,
) -> tuple[int, int]:
    monthly_start = _month_start(anchor_date)
    monthly_end = _month_end(anchor_date)
    week_start = _week_start(monthly_start)
    week_end = _week_start(monthly_end) + timedelta(days=6)
    weekly_habits = [
        habit for habit in habits if habit.frequency == "weekly" and habit.active
    ]
    if not weekly_habits:
        return 0, 0
    weekly_ids = [habit.id for habit in weekly_habits]
    week_plans = session.exec(
        select(DailyPlan).where(DailyPlan.date >= week_start, DailyPlan.date <= week_end)
    ).all()
    plan_by_id = {plan.id: plan.date for plan in week_plans}
    plan_ids = list(plan_by_id.keys())
    completed_by_week: set[tuple[int, date]] = set()
    if plan_ids:
        week_items = session.exec(
            select(PlanItem).where(
                PlanItem.daily_plan_id.in_(plan_ids),
                PlanItem.linked_habit_id.in_(weekly_ids),
                PlanItem.completed_at.is_not(None),
            )
        ).all()
        for item in week_items:
            plan_date = plan_by_id.get(item.daily_plan_id)
            if not plan_date or not item.linked_habit_id:
                continue
            completed_by_week.add((item.linked_habit_id, _week_start(plan_date)))
    total_weeks = 0
    done_weeks = 0
    cursor = week_start
    while cursor <= monthly_end:
        cursor_end = cursor + timedelta(days=6)
        for habit in weekly_habits:
            start_date = habit.start_date or date(2026, 1, 1)
            if start_date > cursor_end:
                continue
            total_weeks += 1
            if (habit.id, cursor) in completed_by_week:
                done_weeks += 1
        cursor += timedelta(days=7)
    return done_weeks, total_weeks


def _sync_plan_items(session: Session, plan: DailyPlan) -> None:
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    objective_items = [item for item in items if item.linked_objective_id]
    if objective_items:
        for item in objective_items:
            session.delete(item)
    existing_habit_ids = {
        item.linked_habit_id
        for item in items
        if item.linked_habit_id and not item.linked_objective_id
    }
    suppressions = session.exec(
        select(PlanItemSuppression).where(PlanItemSuppression.date == plan.date)
    ).all()
    suppressed_habit_ids = {
        row.linked_habit_id for row in suppressions if row.linked_habit_id
    }
    changed = bool(objective_items)

    templates = session.exec(
        select(HabitTemplate).where(HabitTemplate.active == True)  # noqa: E712
    ).all()
    week_start = _week_start(plan.date)
    week_end = week_start + timedelta(days=6)
    for template in templates:
        if not _template_included(template, plan.date):
            continue
        start_date = template.start_date or date(2026, 1, 1)
        if plan.date < start_date:
            continue
        if template.id in existing_habit_ids:
            continue
        if template.id in suppressed_habit_ids:
            continue
        completed_at = None
        status = "pending"
        if template.frequency == "weekly":
            completed_item = session.exec(
                select(PlanItem)
                .join(DailyPlan, PlanItem.daily_plan_id == DailyPlan.id)
                .where(
                    PlanItem.linked_habit_id == template.id,
                    PlanItem.completed_at.is_not(None),
                    DailyPlan.date >= week_start,
                    DailyPlan.date <= week_end,
                )
            ).first()
            if completed_item:
                status = "completed"
                completed_at = completed_item.completed_at
        session.add(
            PlanItem(
                daily_plan_id=plan.id,
                title=template.title,
                linked_habit_id=template.id,
                status=status,
                completed_at=completed_at,
            )
        )
        changed = True

    objectives = session.exec(
        select(ShortTermObjective).where(ShortTermObjective.status == "pending")
    ).all()
    for obj in objectives:
        if obj.status == "pending" and obj.due_date < plan.date:
            obj.status = "expired"
            session.add(obj)
            changed = True

    if changed:
        session.commit()


def _build_period_rows(log: DayLog, periods: List[str]) -> List[Dict[str, str]]:
    period_label_map = _period_labels()
    period_text_map = {entry.get("period"): entry.get("text", "") for entry in log.period_entries}
    period_rows = []
    for period in periods:
        text = period_text_map.get(period, "")
        period_rows.append(
            {
                "period": period,
                "label": period_label_map.get(period, period),
                "text": text,
                "html": markdown.markdown(text or "", extensions=["extra", "sane_lists"]),
            }
        )
    return period_rows


def _today() -> date:
    return date.today()


def _parse_date(value: Optional[str], fallback: date) -> date:
    if not value:
        return fallback
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return fallback


def _parse_date_with_notice(value: Optional[str], fallback: date) -> tuple[date, bool]:
    if not value:
        return fallback, False
    if isinstance(value, date):
        return value, False
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date(), False
    except (ValueError, TypeError):
        return fallback, True


def _goal_progress_payload(goal: Goal, today: date) -> Dict[str, Any]:
    start_date = goal.start_date or today
    end_date = goal.end_date or today
    total_days = (end_date - start_date).days
    elapsed_raw = (today - start_date).days
    if total_days <= 0:
        progress = 1.0 if today >= end_date else 0.0
        elapsed_days = 0 if today < start_date else max(total_days, 0)
        total_days = max(total_days, 0)
    else:
        progress = max(0.0, min(elapsed_raw / total_days, 1.0))
        elapsed_days = max(0, min(elapsed_raw, total_days))
    return {
        "progress": progress,
        "progress_pct": int(round(progress * 100)),
        "elapsed_days": elapsed_days,
        "total_days": total_days,
    }


def _goal_progress_labels(locale: str) -> Dict[str, str]:
    if locale == "en":
        return {"elapsed": "Elapsed", "days": "days"}
    return {"elapsed": "已过去", "days": "天"}


def _mask_key(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _env_file_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".env"


def _write_env_value(key: str, value: str) -> None:
    env_path = _env_file_path()
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    updated = False
    for idx, line in enumerate(lines):
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        existing_key = line.split("=", 1)[0].strip()
        if existing_key == key:
            lines[idx] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _day_bounds(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end


def _weekly_reflection_snoozed(session: Session, as_of: date) -> bool:
    week_start = datetime.combine(_week_start(as_of), datetime.min.time())
    week_end = datetime.combine(_week_end(as_of) + timedelta(days=1), datetime.min.time())
    row = session.exec(
        select(SuggestionDecision)
        .join(Suggestion, SuggestionDecision.suggestion_id == Suggestion.id)
        .where(
            Suggestion.type == "weekly_reflection",
            SuggestionDecision.decision == "snooze_week",
            SuggestionDecision.created_at >= week_start,
            SuggestionDecision.created_at < week_end,
        )
    ).first()
    return row is not None


def _weekly_reflection_for_date(session: Session, as_of: date) -> Optional[Suggestion]:
    start, end = _day_bounds(as_of)
    return session.exec(
        select(Suggestion)
        .where(
            Suggestion.type == "weekly_reflection",
            Suggestion.created_at >= start,
            Suggestion.created_at < end,
        )
        .order_by(Suggestion.created_at.desc())
    ).first()


def _goal_analysis_for_date(
    session: Session, goal_id: int, as_of: date
) -> Optional[Suggestion]:
    candidates = session.exec(
        select(Suggestion)
        .where(Suggestion.type == "goal_analysis")
        .order_by(Suggestion.created_at.desc())
        .limit(100)
    ).all()
    as_of_value = as_of.isoformat()
    for suggestion in candidates:
        metrics = suggestion.metrics_json or {}
        meta = metrics.get("metrics") or {}
        if meta.get("goal_id") == goal_id and meta.get("as_of") == as_of_value:
            return suggestion
    return None


def _latest_goal_analysis(session: Session, goal_id: int) -> Optional[Suggestion]:
    candidates = session.exec(
        select(Suggestion)
        .where(Suggestion.type == "goal_analysis")
        .order_by(Suggestion.created_at.desc())
        .limit(200)
    ).all()
    for suggestion in candidates:
        metrics = suggestion.metrics_json or {}
        meta = metrics.get("metrics") or {}
        if meta.get("goal_id") == goal_id:
            return suggestion
    return None


def _format_goal_analysis_card(suggestion: Suggestion) -> Dict[str, Any]:
    metrics = suggestion.metrics_json or {}
    return {
        "id": suggestion.id,
        "progress_summary": metrics.get("progress_summary") or suggestion.reason,
        "highlights": metrics.get("highlights") or [],
        "risks": metrics.get("risks") or [],
        "next_steps": metrics.get("next_steps") or [],
        "assumptions": metrics.get("assumptions") or [],
        "ask_back": metrics.get("ask_back") or "",
        "notice": metrics.get("notice") or "",
        "metrics": metrics,
        "intent": metrics.get("intent") or {},
        "evidence": metrics.get("evidence") or {},
    }


def _format_weekly_reflection_card(suggestion: Suggestion) -> Dict[str, Any]:
    metrics = suggestion.metrics_json or {}
    return {
        "id": suggestion.id,
        "opener": metrics.get("opener") or suggestion.reason,
        "highlights": metrics.get("highlights") or [],
        "gaps": metrics.get("gaps") or {"missing_dates": [], "message": "", "links": []},
        "next_steps": metrics.get("next_steps") or [],
        "notice": metrics.get("notice") or "",
        "metrics": metrics,
    }


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, habit_month: Optional[str] = None) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    today = _today()
    anchor_date = today
    if habit_month:
        try:
            month_start = datetime.strptime(habit_month, "%Y-%m").date()
            anchor_date = _month_end(month_start)
        except ValueError:
            anchor_date = today
    anchor_month_start = _month_start(anchor_date)
    prev_month = _shift_month(anchor_month_start, -1).strftime("%Y-%m")
    next_month = _shift_month(anchor_month_start, 1).strftime("%Y-%m")
    periods = _get_periods(session)
    log = _ensure_day_log(session, today)
    period_rows = _build_period_rows(log, periods)
    log_has_content = any(
        (entry.get("text", "") or "").strip() for entry in log.period_entries
    )
    plan = _ensure_daily_plan(session, today)
    _sync_plan_items(session, plan)
    items = session.exec(
        select(PlanItem).where(
            PlanItem.daily_plan_id == plan.id,
            PlanItem.linked_objective_id.is_(None),
        )
    ).all()
    habits = session.exec(select(HabitTemplate).where(HabitTemplate.active == True)).all()  # noqa: E712
    habit_by_id = {habit.id: habit for habit in habits}
    plan_items = [
        item
        for item in items
        if not (
            item.linked_habit_id
            and habit_by_id.get(item.linked_habit_id)
            and habit_by_id[item.linked_habit_id].frequency == "weekly"
        )
    ]
    completed = len([item for item in plan_items if item.completed_at])
    objectives = session.exec(
        select(ShortTermObjective).where(
            ShortTermObjective.status == "pending",
            ShortTermObjective.due_date >= today,
        ).order_by(ShortTermObjective.due_date)
    ).all()
    short_term_objectives = [
        {
            "title": obj.title,
            "due_date": obj.due_date,
            "remaining_days": (obj.due_date - today).days,
        }
        for obj in objectives
    ]
    suggestions = session.exec(
        select(Suggestion).where(
            Suggestion.status == "open", Suggestion.type != "weekly_reflection"
        )
    ).all()
    habit_progress = _habit_progress_summary(session, anchor_date)

    weekly_reflection = None
    weekly_reflection_snoozed = _weekly_reflection_snoozed(session, today)
    if not weekly_reflection_snoozed:
        lang = _get_locale(request)
        suggestion = _weekly_reflection_for_date(session, today)
        if suggestion:
            existing_lang = (suggestion.metrics_json or {}).get("lang")
            if existing_lang and existing_lang != lang:
                try:
                    executor.execute(
                        session,
                        "review.weekly_reflection",
                        {
                            "as_of": today,
                            "window_days": 7,
                            "lang": lang,
                            "existing_id": suggestion.id,
                        },
                    )
                except RuntimeError:
                    # Keep the existing card instead of dropping the section entirely.
                    pass
                else:
                    suggestion = _weekly_reflection_for_date(session, today)
        if not suggestion:
            try:
                executor.execute(
                    session,
                    "review.weekly_reflection",
                    {"as_of": today, "window_days": 7, "lang": lang},
                )
            except RuntimeError:
                suggestion = None
            else:
                suggestion = _weekly_reflection_for_date(session, today)
        if suggestion:
            weekly_reflection = _format_weekly_reflection_card(suggestion)

    week_start = _week_start(today)
    week_end = week_start + timedelta(days=6)
    weekly_habits = [
        habit
        for habit in habits
        if habit.frequency == "weekly"
        and habit.active
        and (habit.start_date or date(2026, 1, 1)) <= week_end
    ]
    weekly_habit_ids = [habit.id for habit in weekly_habits]
    weekly_plan_items: List[Dict[str, Any]] = []
    weekly_week_done, weekly_week_total, completed_weekly_ids = _weekly_plan_completion(
        session, habits, week_start, week_end
    )
    if weekly_habit_ids:
        for habit in weekly_habits:
            target = habit.target_per_week if habit.target_per_week else 1
            completed_count = 1 if habit.id in completed_weekly_ids else 0
            if completed_count >= target:
                status = "completed"
                emoji = "😁"
            elif completed_count == 0:
                status = "not_started"
                emoji = "😞"
            else:
                status = "near" if (completed_count / target) >= 0.8 else "in_progress"
                emoji = "😊" if status == "near" else "💪"
            weekly_plan_items.append(
                {
                    "title": habit.title,
                    "completed": completed_count,
                    "target": target,
                    "status": status,
                    "emoji": emoji,
                }
            )

    if weekly_week_total:
        weekly_week_rate = weekly_week_done / weekly_week_total
        if weekly_week_rate >= 1:
            weekly_week_emoji = "😁"
        elif weekly_week_rate >= 0.8:
            weekly_week_emoji = "😊"
        elif weekly_week_rate > 0:
            weekly_week_emoji = "💪"
        else:
            weekly_week_emoji = "😞"
    else:
        weekly_week_emoji = "😞"

    daily_week_done, daily_week_total = _daily_plan_completion(
        session, week_start, week_end, habit_by_id
    )
    month_start = _month_start(today)
    daily_month_done, daily_month_total = _daily_plan_completion(
        session, month_start, today, habit_by_id
    )
    weekly_month_done, weekly_month_total = _weekly_plan_month_completion(
        session, habits, today
    )
    daily_week_rate_pct = (
        int(round((daily_week_done / daily_week_total) * 100)) if daily_week_total else 0
    )
    daily_month_rate_pct = (
        int(round((daily_month_done / daily_month_total) * 100)) if daily_month_total else 0
    )
    habit_progress.update(
        {
            "daily_week_done": daily_week_done,
            "daily_week_total": daily_week_total,
            "daily_week_rate_pct": daily_week_rate_pct,
            "daily_month_done": daily_month_done,
            "daily_month_total": daily_month_total,
            "daily_month_rate_pct": daily_month_rate_pct,
            "weekly_week_done": weekly_week_done,
            "weekly_week_total": weekly_week_total,
            "weekly_week_emoji": weekly_week_emoji,
            "weekly_month_done": weekly_month_done,
            "weekly_month_total": weekly_month_total,
        }
    )

    overload = False
    overload_reason = ""
    if len(plan_items) > 8:
        overload = True
        overload_reason = "dashboard.overload.reason.too_many"
    else:
        week_start = today - timedelta(days=6)
        week_plans = session.exec(
            select(DailyPlan).where(DailyPlan.date >= week_start, DailyPlan.date <= today)
        ).all()
        plan_ids = [plan.id for plan in week_plans]
        week_items = session.exec(
            select(PlanItem).where(PlanItem.daily_plan_id.in_(plan_ids))
        ).all() if plan_ids else []
        filtered_week_items = [
            item
            for item in week_items
            if not (
                item.linked_habit_id
                and habit_by_id.get(item.linked_habit_id)
                and habit_by_id[item.linked_habit_id].frequency == "weekly"
            )
        ]
        week_completed = len([item for item in filtered_week_items if item.completed_at])
        week_rate = week_completed / len(filtered_week_items) if filtered_week_items else 1
        if week_rate < 0.3:
            overload = True
            overload_reason = "dashboard.overload.reason.low_completion"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "today": today,
            "plan_items": plan_items,
            "completed": completed,
            "habits": habits,
            "log": log,
            "log_has_content": log_has_content,
            "short_term_objectives": short_term_objectives,
            "weekly_plan_items": weekly_plan_items,
            "suggestions": suggestions,
            "weekly_reflection": weekly_reflection,
            "weekly_reflection_snoozed": weekly_reflection_snoozed,
            "overload": overload,
            "overload_reason": overload_reason,
            "period_rows": period_rows,
            "periods": periods,
            "period_labels": _period_labels(),
            "habit_progress": habit_progress,
            "habit_progress_prev_month": prev_month,
            "habit_progress_next_month": next_month,
        },
    )


@router.get("/api/info", response_class=JSONResponse)
def api_info() -> Response:
    return JSONResponse({"version": APP_VERSION, "developer": DEVELOPER_ID})


@router.get("/goals", response_class=HTMLResponse)
def goals(request: Request) -> Response:
    session = _get_session(request)
    locale = _get_locale(request)
    executor = _get_executor(request)
    goals_list = session.exec(select(Goal)).all()
    milestones = session.exec(select(Milestone)).all()
    today = _today()
    settings_row = session.exec(select(Settings).where(Settings.id == 1)).first()
    env_key = os.getenv("LIFEOS_LLM_API_KEY", "").strip()
    llm_key_present = bool(env_key or (settings_row.llm_api_key if settings_row else ""))
    goal_progress = {goal.id: _goal_progress_payload(goal, today) for goal in goals_list}
    goal_analyses: Dict[int, Dict[str, Any]] = {}
    goal_actual_progress: Dict[int, Optional[int]] = {}
    goal_agent_notice_by_id: Dict[int, str] = {}
    for goal in goals_list:
        latest_suggestion = _latest_goal_analysis(session, goal.id)
        latest_metrics = (latest_suggestion.metrics_json or {}).get("metrics") if latest_suggestion else {}
        progress_pct = latest_metrics.get("progress_pct")
        if isinstance(progress_pct, (int, float)):
            progress_pct = max(0, min(int(round(progress_pct)), 100))
        else:
            progress_pct = None
        goal_actual_progress[goal.id] = progress_pct
        suggestion = _goal_analysis_for_date(session, goal.id, today)
        if not suggestion:
            payload = {
                "goal_id": goal.id,
                "as_of": today,
                "lang": locale,
            }
            try:
                executor.execute(session, "review.goal_analysis", payload)
            except RuntimeError:
                suggestion = _goal_analysis_for_date(session, goal.id, today)
            else:
                suggestion = _goal_analysis_for_date(session, goal.id, today)
        card = _format_goal_analysis_card(suggestion) if suggestion else None
        has_content = bool(
            card
            and (
                card.get("progress_summary")
                or card.get("highlights")
                or card.get("next_steps")
                or card.get("risks")
                or card.get("assumptions")
                or card.get("ask_back")
            )
        )
        if has_content:
            goal_analyses[goal.id] = card
        elif not llm_key_present:
            goal_agent_notice_by_id[goal.id] = (
                "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
                if locale == "en"
                else "暂未没有配置LLM_API_KEY. 请前往 Settings  LLM Settings进行配置"
            )
        else:
            goal_agent_notice_by_id[goal.id] = (
                "No goal analysis generated yet. Please click regenerate."
                if locale == "en"
                else "本次未生成内容，请点击重新生成。"
            )
    agent_message = (
        "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
        if locale == "en"
        else "暂无可用 LLM_API_KEY（请到 Settings  LLM Settings 配置）"
    )
    agent_title = "Goal Analysis Agent" if locale == "en" else "Goal 分析 Agent"
    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "goals": goals_list,
            "milestones": milestones,
            "goal_progress": goal_progress,
            "goal_progress_labels": _goal_progress_labels(locale),
            "goal_agent_message": agent_message,
            "goal_agent_title": agent_title,
            "goal_analyses": goal_analyses,
            "goal_actual_progress": goal_actual_progress,
            "goal_agent_notice_by_id": goal_agent_notice_by_id,
        },
    )


@router.post("/goals", response_class=HTMLResponse)
def create_goal(
    request: Request,
    title: str = Form(...),
    type: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    description_md: str = Form(""),
    tags: str = Form(""),
) -> Response:
    session = _get_session(request)
    tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    goal = Goal(
        title=title,
        type=type,
        start_date=_parse_date(start_date, _today()),
        end_date=_parse_date(end_date, _today()),
        description_md=description_md,
        tags=tags_list,
    )
    session.add(goal)
    session.commit()
    return Response(status_code=303, headers={"Location": "/goals"})


@router.get("/goals/{goal_id}", response_class=HTMLResponse)
def view_goal(request: Request, goal_id: int) -> Response:
    session = _get_session(request)
    locale = _get_locale(request)
    executor = _get_executor(request)
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal.id)).all()
    today = _today()
    settings_row = session.exec(select(Settings).where(Settings.id == 1)).first()
    env_key = os.getenv("LIFEOS_LLM_API_KEY", "").strip()
    llm_key_present = bool(env_key or (settings_row.llm_api_key if settings_row else ""))
    suggestion = _goal_analysis_for_date(session, goal.id, today)
    if not suggestion:
        payload = {
            "goal_id": goal.id,
            "as_of": today,
            "lang": locale,
        }
        try:
            executor.execute(session, "review.goal_analysis", payload)
        except RuntimeError:
            suggestion = _goal_analysis_for_date(session, goal.id, today)
        else:
            suggestion = _goal_analysis_for_date(session, goal.id, today)
    goal_analysis = _format_goal_analysis_card(suggestion) if suggestion else None
    has_content = bool(
        goal_analysis
        and (
            goal_analysis.get("progress_summary")
            or goal_analysis.get("highlights")
            or goal_analysis.get("next_steps")
            or goal_analysis.get("risks")
            or goal_analysis.get("assumptions")
            or goal_analysis.get("ask_back")
        )
    )
    goal_agent_notice = ""
    if not has_content and not llm_key_present:
        goal_agent_notice = (
            "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
            if locale == "en"
            else "暂未没有配置LLM_API_KEY. 请前往 Settings  LLM Settings进行配置"
        )
    elif not has_content and llm_key_present:
        goal_agent_notice = (
            "No goal analysis generated yet. Please click regenerate."
            if locale == "en"
            else "本次未生成内容，请点击重新生成。"
        )
    return templates.TemplateResponse(
        "partials/goal_card.html",
        {
            "request": request,
            "goal": goal,
            "milestones": milestones,
            "goal_progress": {goal.id: _goal_progress_payload(goal, _today())},
            "goal_progress_labels": _goal_progress_labels(locale),
            "goal_analyses": {goal.id: goal_analysis} if goal_analysis else {},
            "goal_agent_message": (
                "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
                if locale == "en"
                else "暂无可用 LLM_API_KEY（请到 Settings  LLM Settings 配置）"
            ),
            "goal_agent_title": "Goal Analysis Agent" if locale == "en" else "Goal 分析 Agent",
        },
    )


@router.get("/goals/{goal_id}/edit", response_class=HTMLResponse)
def edit_goal(request: Request, goal_id: int) -> Response:
    session = _get_session(request)
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    return templates.TemplateResponse(
        "partials/goal_edit.html", {"request": request, "goal": goal}
    )


@router.post("/goals/{goal_id}/edit", response_class=HTMLResponse)
def update_goal(
    request: Request,
    goal_id: int,
    title: str = Form(...),
    type: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    description_md: str = Form(""),
    tags: str = Form(""),
) -> Response:
    session = _get_session(request)
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    goal.title = title
    goal.type = type
    goal.start_date = _parse_date(start_date, _today())
    goal.end_date = _parse_date(end_date, _today())
    goal.description_md = description_md
    goal.tags = [t.strip() for t in tags.split(",") if t.strip()]
    session.add(goal)
    session.commit()
    session.refresh(goal)
    milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal.id)).all()
    return templates.TemplateResponse(
        "partials/goal_card.html",
        {
            "request": request,
            "goal": goal,
            "milestones": milestones,
            "goal_progress": {goal.id: _goal_progress_payload(goal, _today())},
            "goal_progress_labels": _goal_progress_labels(_get_locale(request)),
            "goal_agent_message": (
                "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
                if _get_locale(request) == "en"
                else "暂无可用 LLM_API_KEY（请到 Settings  LLM Settings 配置）"
            ),
            "goal_agent_title": (
                "Goal Analysis Agent" if _get_locale(request) == "en" else "Goal 分析 Agent"
            ),
        },
    )


@router.post("/goals/{goal_id}/delete", response_class=HTMLResponse)
def delete_goal(request: Request, goal_id: int) -> Response:
    session = _get_session(request)
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal_id)).all()
    for milestone in milestones:
        session.delete(milestone)
    linked_items = session.exec(select(PlanItem).where(PlanItem.linked_goal_id == goal_id)).all()
    for item in linked_items:
        item.linked_goal_id = None
        session.add(item)
    session.delete(goal)
    session.commit()
    return Response(content="")


@router.post("/milestones", response_class=HTMLResponse)
def create_milestone(
    request: Request,
    goal_id: int = Form(...),
    title: str = Form(...),
    due_date: str = Form(...),
    status: str = Form("pending"),
) -> Response:
    session = _get_session(request)
    milestone = Milestone(
        goal_id=goal_id,
        title=title,
        due_date=_parse_date(due_date, _today()),
        status=status,
    )
    session.add(milestone)
    session.commit()
    return Response(status_code=303, headers={"Location": "/goals"})


@router.get("/plans", response_class=HTMLResponse)
def plans(request: Request, target_date: Optional[str] = None) -> Response:
    session = _get_session(request)
    selected_date = _parse_date(target_date, _today())
    plan = _ensure_daily_plan(session, selected_date)
    _sync_plan_items(session, plan)
    items = session.exec(
        select(PlanItem).where(
            PlanItem.daily_plan_id == plan.id,
            PlanItem.linked_objective_id.is_(None),
        )
    ).all()
    habits = session.exec(select(HabitTemplate).where(HabitTemplate.active == True)).all()  # noqa: E712
    objectives = session.exec(
        select(ShortTermObjective).order_by(ShortTermObjective.due_date)
    ).all()
    goals_list = session.exec(select(Goal)).all()
    goals_by_id = {goal.id: goal.title for goal in goals_list}
    return templates.TemplateResponse(
        "plans.html",
        {
            "request": request,
            "selected_date": selected_date,
            "plan_items": items,
            "habits": habits,
            "objectives": objectives,
            "goals": goals_list,
            "goals_by_id": goals_by_id,
        },
    )


@router.post("/plans/items", response_class=HTMLResponse)
def create_plan_item(
    request: Request,
    date_value: str = Form(...),
    title: str = Form(...),
    linked_goal_id: int = Form(...),
    due_date: str = Form(...),
    status: str = Form("pending"),
    note: str = Form(""),
) -> Response:
    session = _get_session(request)
    objective = ShortTermObjective(
        title=title,
        linked_goal_id=linked_goal_id,
        due_date=_parse_date(due_date, _today()),
        status=status,
        note=note,
    )
    session.add(objective)
    session.commit()
    session.refresh(objective)
    return Response(status_code=303, headers={"Location": f"/plans?target_date={date_value}"})


@router.post("/objectives/{objective_id}/complete", response_class=HTMLResponse)
def complete_objective(request: Request, objective_id: int) -> Response:
    session = _get_session(request)
    objective = session.exec(
        select(ShortTermObjective).where(ShortTermObjective.id == objective_id)
    ).first()
    if not objective:
        return Response(status_code=404)
    objective.status = "completed"
    session.add(objective)
    session.commit()
    return Response(status_code=303, headers={"Location": "/plans"})


@router.post("/objectives/{objective_id}/delete", response_class=HTMLResponse)
def delete_objective(request: Request, objective_id: int) -> Response:
    session = _get_session(request)
    objective = session.exec(
        select(ShortTermObjective).where(ShortTermObjective.id == objective_id)
    ).first()
    if not objective:
        return Response(status_code=404)
    linked_items = session.exec(
        select(PlanItem).where(PlanItem.linked_objective_id == objective_id)
    ).all()
    for item in linked_items:
        session.delete(item)
    session.delete(objective)
    session.commit()
    return Response(status_code=303, headers={"Location": "/plans"})


@router.post("/plans/items/daily", response_class=HTMLResponse)
def create_daily_item(
    request: Request,
    date_value: str = Form(...),
    title: str = Form(...),
    linked_goal_id: Optional[int] = Form(None),
    note: str = Form(""),
) -> Response:
    session = _get_session(request)
    plan_date = _parse_date(date_value, _today())
    plan = _ensure_daily_plan(session, plan_date)
    session.add(
        PlanItem(
            daily_plan_id=plan.id,
            title=title,
            linked_goal_id=linked_goal_id,
            note=note,
            status="pending",
        )
    )
    session.commit()
    return Response(status_code=303, headers={"Location": f"/plans?target_date={date_value}"})


@router.post("/plans/items/{item_id}/toggle", response_class=HTMLResponse)
def toggle_plan_item(
    request: Request, item_id: int, completed: bool = Form(False), fragment: Optional[str] = None
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    try:
        executor.execute(
            session,
            "plan.toggle_complete",
            {"plan_item_id": item_id, "completed": completed},
        )
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/plans"},
            status_code=400,
        )
    item = session.exec(select(PlanItem).where(PlanItem.id == item_id)).first()
    if fragment == "align":
        return templates.TemplateResponse(
            "partials/align_done.html", {"request": request, "item": item}
        )
    return templates.TemplateResponse(
        "partials/plan_item.html", {"request": request, "item": item}
    )


@router.get("/plans/items/{item_id}", response_class=HTMLResponse)
def view_plan_item(request: Request, item_id: int) -> Response:
    session = _get_session(request)
    item = session.exec(select(PlanItem).where(PlanItem.id == item_id)).first()
    if not item:
        return Response(status_code=404)
    return templates.TemplateResponse(
        "partials/plan_item.html", {"request": request, "item": item}
    )


@router.get("/plans/items/{item_id}/edit", response_class=HTMLResponse)
def edit_plan_item(request: Request, item_id: int) -> Response:
    session = _get_session(request)
    item = session.exec(select(PlanItem).where(PlanItem.id == item_id)).first()
    if not item:
        return Response(status_code=404)
    goals_list = session.exec(select(Goal)).all()
    objective = None
    if item.linked_objective_id:
        objective = session.exec(
            select(ShortTermObjective).where(ShortTermObjective.id == item.linked_objective_id)
        ).first()
    return templates.TemplateResponse(
        "partials/plan_item_edit.html",
        {"request": request, "item": item, "goals": goals_list, "objective": objective},
    )


@router.post("/plans/items/{item_id}/edit", response_class=HTMLResponse)
def update_plan_item(
    request: Request,
    item_id: int,
    title: str = Form(...),
    linked_goal_id: Optional[int] = Form(None),
    due_date: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    note: str = Form(""),
) -> Response:
    session = _get_session(request)
    item = session.exec(select(PlanItem).where(PlanItem.id == item_id)).first()
    if not item:
        return Response(status_code=404)
    if item.linked_objective_id:
        objective = session.exec(
            select(ShortTermObjective).where(ShortTermObjective.id == item.linked_objective_id)
        ).first()
        if objective:
            objective.title = title
            if linked_goal_id is not None:
                objective.linked_goal_id = linked_goal_id
            if due_date:
                objective.due_date = _parse_date(due_date, objective.due_date)
            if status:
                objective.status = status
            objective.note = note
            session.add(objective)
            item.title = objective.title
            item.linked_goal_id = objective.linked_goal_id
    else:
        item.title = title
        item.linked_goal_id = linked_goal_id
    item.note = note
    session.add(item)
    session.commit()
    session.refresh(item)
    return templates.TemplateResponse(
        "partials/plan_item.html", {"request": request, "item": item}
    )


@router.post("/plans/items/{item_id}/delete", response_class=HTMLResponse)
def delete_plan_item(request: Request, item_id: int) -> Response:
    session = _get_session(request)
    item = session.exec(select(PlanItem).where(PlanItem.id == item_id)).first()
    if not item:
        return Response(status_code=404)
    plan = session.exec(select(DailyPlan).where(DailyPlan.id == item.daily_plan_id)).first()
    plan_date = plan.date if plan else _today()
    if item.linked_habit_id:
        existing = session.exec(
            select(PlanItemSuppression).where(
                PlanItemSuppression.date == plan_date,
                PlanItemSuppression.linked_habit_id == item.linked_habit_id,
            )
        ).first()
        if not existing:
            session.add(
                PlanItemSuppression(
                    date=plan_date, linked_habit_id=item.linked_habit_id
                )
            )
    if item.linked_objective_id:
        existing = session.exec(
            select(PlanItemSuppression).where(
                PlanItemSuppression.date == plan_date,
                PlanItemSuppression.linked_objective_id == item.linked_objective_id,
            )
        ).first()
        if not existing:
            session.add(
                PlanItemSuppression(
                    date=plan_date, linked_objective_id=item.linked_objective_id
                )
            )
    session.delete(item)
    session.commit()
    return Response(content="")


@router.post("/habits", response_class=HTMLResponse)
def create_habit(
    request: Request,
    title: str = Form(...),
    frequency: str = Form(...),
    target_per_week: int = Form(7),
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    payload = {
        "title": title,
        "frequency": frequency,
        "target_per_week": target_per_week,
        "active": True,
    }
    try:
        executor.execute(session, "habit.create_or_update", payload)
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/plans"},
            status_code=400,
        )
    plan = _ensure_daily_plan(session, _today())
    _sync_plan_items(session, plan)
    return Response(status_code=303, headers={"Location": "/plans"})


@router.post("/habits/{habit_id}/delete", response_class=HTMLResponse)
def delete_habit(request: Request, habit_id: int) -> Response:
    session = _get_session(request)
    habit = session.exec(select(HabitTemplate).where(HabitTemplate.id == habit_id)).first()
    if not habit:
        return Response(status_code=404)
    habit.active = False
    session.add(habit)
    future_items = session.exec(
        select(PlanItem)
        .join(DailyPlan, PlanItem.daily_plan_id == DailyPlan.id)
        .where(
            PlanItem.linked_habit_id == habit.id,
            DailyPlan.date >= _today(),
        )
    ).all()
    for future_item in future_items:
        session.delete(future_item)
    session.commit()
    return Response(content="")


@router.get("/logs", response_class=HTMLResponse)
def logs(request: Request, target_date: Optional[str] = None) -> Response:
    session = _get_session(request)
    selected_date, used_fallback = _parse_date_with_notice(target_date, _today())
    log = _ensure_day_log(session, selected_date)
    plan = _ensure_daily_plan(session, selected_date)
    _sync_plan_items(session, plan)
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    periods = _get_periods(session)
    journal_html = markdown.markdown(log.journal_md) if log else ""
    period_rows = _build_period_rows(log, periods)
    recent_start = _today() - timedelta(days=6)
    logs_recent = session.exec(
        select(DayLog)
        .where(DayLog.date >= recent_start, DayLog.date <= _today())
        .order_by(DayLog.date.asc())
    ).all()
    return templates.TemplateResponse(
        "logs.html",
        {
            "request": request,
            "selected_date": selected_date,
            "log": log,
            "plan_items": items,
            "periods": periods,
            "journal_html": journal_html,
            "logs": logs_recent,
            "period_rows": period_rows,
            "period_labels": _period_labels(),
            "used_fallback": used_fallback,
        },
    )


@router.get("/logs/table", response_class=HTMLResponse)
def logs_table(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    tag: Optional[str] = None,
    plan_item: Optional[str] = None,
) -> Response:
    session = _get_session(request)
    start = _parse_date(start_date, _today() - timedelta(days=6))
    end = _parse_date(end_date, _today())
    logs = session.exec(
        select(DayLog)
        .where(DayLog.date >= start, DayLog.date <= end)
        .order_by(DayLog.date.asc())
    ).all()

    filtered: List[DayLog] = []
    for log in logs:
        if tag and tag not in log.tags:
            continue
        if plan_item:
            plan = session.exec(select(DailyPlan).where(DailyPlan.date == log.date)).first()
            if not plan:
                continue
            items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
            if not any(plan_item.lower() in item.title.lower() for item in items):
                continue
        filtered.append(log)

    return templates.TemplateResponse(
        "partials/log_table.html",
        {
            "request": request,
            "logs": filtered,
            "start_date": start,
            "end_date": end,
            "plan_item": plan_item or "",
            "period_labels": _period_labels(),
        },
    )


@router.post("/logs/save", response_class=HTMLResponse)
def save_log(
    request: Request,
    date_value: str = Form(...),
    journal_md: str = Form(""),
    tags: str = Form(""),
    periods_text: Optional[str] = Form(None),
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    periods = _get_periods(session)
    entries: List[Dict[str, Any]] = []
    if periods_text:
        for part in periods_text.split("||"):
            if not part.strip():
                continue
            segments = part.split("::", 1)
            if len(segments) == 2:
                period, text = segments
            else:
                period, text = segments[0], ""
            entries.append({"period": period, "text": text, "tags": []})

    entry_map = {entry["period"]: entry for entry in entries}
    normalized_entries = []
    for period in periods:
        normalized_entries.append(
            entry_map.get(period, {"period": period, "text": "", "tags": []})
        )

    payload = {
        "date": _parse_date(date_value, _today()),
        "period_entries": normalized_entries,
        "journal_md": journal_md,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
    }
    try:
        executor.execute(session, "log.upsert_day_log", payload)
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/logs"},
            status_code=400,
        )
    return templates.TemplateResponse(
        "partials/log_save_result.html",
        {"request": request, "message": "logs.save_success"},
    )


@router.post("/logs/align", response_class=HTMLResponse)
def align_log(request: Request, date_value: str = Form(...)) -> Response:
    session = _get_session(request)
    target = _parse_date(date_value, _today())
    log = session.exec(select(DayLog).where(DayLog.date == target)).first()
    plan = session.exec(select(DailyPlan).where(DailyPlan.date == target)).first()
    items: List[PlanItem] = []
    if plan:
        items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    habits = session.exec(select(HabitTemplate)).all()

    suggestions: List[Dict[str, Any]] = []
    if log:
        texts = " ".join([entry.get("text", "") for entry in log.period_entries]).lower()
        for item in items:
            if item.completed_at:
                continue
            if item.title.lower() in texts:
                suggestions.append({"type": "plan", "id": item.id, "title": item.title})
        for habit in habits:
            if habit.title.lower() in texts:
                suggestions.append({"type": "habit", "id": habit.id, "title": habit.title})

    return templates.TemplateResponse(
        "partials/align_suggestions.html",
        {"request": request, "suggestions": suggestions},
    )


@router.post("/suggestions/generate", response_class=HTMLResponse)
def generate_suggestions(request: Request) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    try:
        executor.execute(session, "agent.generate_suggestions", {"as_of": _today()})
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/dashboard"},
            status_code=400,
        )
    suggestions = session.exec(
        select(Suggestion).where(
            Suggestion.status == "open", Suggestion.type != "weekly_reflection"
        )
    ).all()
    return templates.TemplateResponse(
        "partials/suggestions.html",
        {"request": request, "suggestions": suggestions},
    )


@router.post("/suggestions/{suggestion_id}/decide", response_class=HTMLResponse)
def decide_suggestion(
    request: Request,
    suggestion_id: int,
    decision: str = Form(...),
    note: str = Form(""),
) -> Response:
    session = _get_session(request)
    suggestion = session.exec(select(Suggestion).where(Suggestion.id == suggestion_id)).first()
    if not suggestion:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Suggestion not found", "retry": "/dashboard"},
            status_code=404,
        )
    suggestion.status = decision
    session.add(suggestion)
    decision_row = SuggestionDecision(
        suggestion_id=suggestion_id, decision=decision, note=note
    )
    session.add(decision_row)
    session.commit()
    suggestions = session.exec(
        select(Suggestion).where(
            Suggestion.status == "open", Suggestion.type != "weekly_reflection"
        )
    ).all()
    return templates.TemplateResponse(
        "partials/suggestions.html",
        {"request": request, "suggestions": suggestions},
    )


@router.post("/suggestions/goal_analysis/decide", response_class=HTMLResponse)
def decide_goal_analysis(
    request: Request,
    suggestion_id: int = Form(...),
    decision: str = Form(...),
    note: str = Form(""),
    goal_id: int = Form(...),
) -> Response:
    session = _get_session(request)
    suggestion = session.exec(select(Suggestion).where(Suggestion.id == suggestion_id)).first()
    if not suggestion or suggestion.type != "goal_analysis":
        return Response(status_code=404)
    suggestion.status = decision
    session.add(suggestion)
    decision_row = SuggestionDecision(
        suggestion_id=suggestion_id, decision=decision, note=note
    )
    session.add(decision_row)
    session.commit()
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal_id)).all()
    locale = _get_locale(request)
    today = _today()
    suggestion = _goal_analysis_for_date(session, goal.id, today)
    goal_analysis = _format_goal_analysis_card(suggestion) if suggestion else None
    return templates.TemplateResponse(
        "partials/goal_card.html",
        {
            "request": request,
            "goal": goal,
            "milestones": milestones,
            "goal_progress": {goal.id: _goal_progress_payload(goal, today)},
            "goal_progress_labels": _goal_progress_labels(locale),
            "goal_agent_message": (
                "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
                if locale == "en" else "暂未没有配置LLM_API_KEY. 请前往 Settings → LLM Settings进行配置"
            ),
            "goal_agent_title": "Goal Analysis Agent" if locale == "en" else "Goal 分析 Agent",
            "goal_analyses": {goal.id: goal_analysis} if goal_analysis else {},
            "goal_agent_notice": goal_agent_notice,
        },
    )


@router.post("/suggestions/goal_analysis/regenerate", response_class=HTMLResponse)
def regenerate_goal_analysis(
    request: Request,
    goal_id: int = Form(...),
    as_of: Optional[str] = Form(None),
    mode: str = Query("llm"),
    lang: Optional[str] = Form(None),
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    locale = _get_locale(request)
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    date_value = _parse_date(as_of, _today())
    suggestion = _goal_analysis_for_date(session, goal_id, date_value)
    lang_value = (lang or locale).strip()
    if lang_value not in {"zh", "en"}:
        lang_value = locale
    payload = {
        "goal_id": goal_id,
        "as_of": date_value,
        "lang": lang_value,
        "mode": "rules" if mode == "rules" else "llm",
        "trigger": "manual_regenerate",
    }
    if suggestion:
        payload["existing_id"] = suggestion.id
    try:
        executor.execute(session, "review.goal_analysis", payload)
    except RuntimeError as exc:
        suggestion = _goal_analysis_for_date(session, goal_id, date_value)
        if suggestion:
            card = _format_goal_analysis_card(suggestion)
            card["notice"] = str(exc)
            milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal_id)).all()
            return templates.TemplateResponse(
                "partials/goal_card.html",
                {
                    "request": request,
                    "goal": goal,
                    "milestones": milestones,
                    "goal_progress": {goal.id: _goal_progress_payload(goal, date_value)},
                    "goal_progress_labels": _goal_progress_labels(locale),
                    "goal_agent_message": (
                        "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
                        if locale == "en" else "暂未没有配置LLM_API_KEY. 请前往 Settings → LLM Settings进行配置"
                    ),
                    "goal_agent_title": "Goal Analysis Agent"
                    if locale == "en"
                    else "Goal 分析 Agent",
                    "goal_analyses": {goal.id: card},
                },
            )
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/goals"},
            status_code=400,
        )
    suggestion = _goal_analysis_for_date(session, goal_id, date_value)
    if not suggestion:
        return Response(status_code=404)
    milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal_id)).all()
    goal_analysis = _format_goal_analysis_card(suggestion)
    return templates.TemplateResponse(
        "partials/goal_card.html",
        {
            "request": request,
            "goal": goal,
            "milestones": milestones,
            "goal_progress": {goal.id: _goal_progress_payload(goal, date_value)},
            "goal_progress_labels": _goal_progress_labels(locale),
            "goal_agent_message": (
                "No LLM_API_KEY configured. Please set it in Settings  LLM Settings."
                if locale == "en" else "暂未没有配置LLM_API_KEY. 请前往 Settings → LLM Settings进行配置"
            ),
            "goal_agent_title": "Goal Analysis Agent" if locale == "en" else "Goal 分析 Agent",
            "goal_analyses": {goal.id: goal_analysis},
        },
    )


@router.post("/weekly-reflection/decide", response_class=HTMLResponse)
def decide_weekly_reflection(
    request: Request,
    suggestion_id: int = Form(...),
    decision: str = Form(...),
    note: str = Form(""),
) -> Response:
    session = _get_session(request)
    suggestion = session.exec(select(Suggestion).where(Suggestion.id == suggestion_id)).first()
    if not suggestion or suggestion.type != "weekly_reflection":
        return Response(status_code=404)
    suggestion.status = decision
    session.add(suggestion)
    decision_row = SuggestionDecision(
        suggestion_id=suggestion_id, decision=decision, note=note
    )
    session.add(decision_row)
    session.commit()
    if decision == "snooze_week":
        return templates.TemplateResponse(
            "partials/weekly_reflection_empty.html",
            {"request": request, "snoozed": True},
        )
    card = _format_weekly_reflection_card(suggestion)
    return templates.TemplateResponse(
        "partials/weekly_reflection.html",
        {"request": request, "weekly_reflection": card},
    )


@router.post("/suggestions/weekly_reflection/regenerate", response_class=HTMLResponse)
def regenerate_weekly_reflection(
    request: Request,
    target_date: Optional[str] = Form(None),
    as_of: Optional[str] = Form(None),
    window_days: int = Form(7),
    mode: str = Query("llm"),
    lang: Optional[str] = Form(None),
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    date_value = as_of or target_date
    as_of = _parse_date(date_value, _today())
    suggestion = _weekly_reflection_for_date(session, as_of)
    lang_value = (lang or _get_locale(request)).strip()
    if lang_value not in {"zh", "en"}:
        lang_value = _get_locale(request)
    normalized_mode = "rules" if mode == "rules" else "llm"
    payload = {
        "as_of": as_of,
        "window_days": window_days,
        "lang": lang_value,
        "mode": normalized_mode,
    }
    if suggestion:
        payload["existing_id"] = suggestion.id
    payload["trigger"] = "manual_regenerate"
    try:
        executor.execute(session, "review.weekly_reflection", payload)
    except RuntimeError as exc:
        suggestion = _weekly_reflection_for_date(session, as_of)
        if suggestion:
            card = _format_weekly_reflection_card(suggestion)
            card["notice"] = str(exc)
            return templates.TemplateResponse(
                "partials/weekly_reflection.html",
                {"request": request, "weekly_reflection": card},
            )
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/dashboard"},
        )
    suggestion = _weekly_reflection_for_date(session, as_of)
    if not suggestion:
        return Response(status_code=404)
    card = _format_weekly_reflection_card(suggestion)
    return templates.TemplateResponse(
        "partials/weekly_reflection.html",
        {"request": request, "weekly_reflection": card},
    )


@router.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request) -> Response:
    now = _today()
    return templates.TemplateResponse(
        "reviews.html",
        {
            "request": request,
            "year": now.year,
            "month": now.month,
        },
    )


@router.post("/reviews/monthly", response_class=HTMLResponse)
def review_monthly(request: Request, year: int = Form(...), month: int = Form(...)) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    try:
        result = executor.execute(session, "review.generate_monthly", {"year": year, "month": month})
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/reviews"},
            status_code=400,
        )
    return templates.TemplateResponse(
        "partials/review_card.html",
        {"request": request, "review": result["review"], "scope": "monthly"},
    )


@router.post("/reviews/yearly", response_class=HTMLResponse)
def review_yearly(request: Request, year: int = Form(...)) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    try:
        result = executor.execute(session, "review.generate_yearly", {"year": year})
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/reviews"},
            status_code=400,
        )
    return templates.TemplateResponse(
        "partials/review_card.html",
        {"request": request, "review": result["review"], "scope": "yearly"},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> Response:
    session = _get_session(request)
    periods = _get_periods(session)
    llm_status = request.query_params.get("llm_status", "")
    settings_row = session.exec(select(Settings).where(Settings.id == 1)).first()
    llm_model = (
        settings_row.llm_model
        if settings_row and settings_row.llm_model
        else "Qwen/Qwen2.5-Coder-32B-Instruct"
    )
    env_key = os.getenv("LIFEOS_LLM_API_KEY", "").strip()
    llm_key_value = env_key or (settings_row.llm_api_key if settings_row else "")
    llm_key_present = bool(llm_key_value)
    llm_key_source = "env" if env_key else "settings" if llm_key_present else ""
    llm_key_masked = _mask_key(llm_key_value)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "periods": periods,
            "llm_model": llm_model,
            "llm_key_present": llm_key_present,
            "llm_key_source": llm_key_source,
            "llm_key_masked": llm_key_masked,
            "llm_status": llm_status,
        },
    )


@router.post("/settings/periods", response_class=HTMLResponse)
def update_periods(
    request: Request, periods: str = Form("")
) -> Response:
    session = _get_session(request)
    list_value = [p.strip() for p in periods.split(",") if p.strip()]
    settings = session.exec(select(Settings).where(Settings.id == 1)).first()
    if not settings:
        settings = Settings(id=1, periods_json=list_value)
    else:
        settings.periods_json = list_value
    session.add(settings)
    session.commit()
    return Response(status_code=303, headers={"Location": "/settings"})


@router.post("/settings/llm", response_class=HTMLResponse)
def update_llm_settings(
    request: Request,
    llm_api_key: str = Form(""),
    llm_model: str = Form("Qwen/Qwen2.5-Coder-32B-Instruct"),
) -> Response:
    session = _get_session(request)
    llm_model = llm_model.strip() or "Qwen/Qwen2.5-Coder-32B-Instruct"
    settings_row = session.exec(select(Settings).where(Settings.id == 1)).first()
    if not settings_row:
        settings_row = Settings(id=1, periods_json=_get_periods(session))
    key_value = llm_api_key.strip()
    if key_value:
        settings_row.llm_api_key = key_value
    settings_row.llm_model = llm_model
    try:
        if key_value:
            _write_env_value("LIFEOS_LLM_API_KEY", key_value)
            os.environ["LIFEOS_LLM_API_KEY"] = key_value
        session.add(settings_row)
        session.commit()
    except Exception as exc:  # noqa: BLE001
        periods = _get_periods(session)
        llm_key_present = bool(settings_row.llm_api_key)
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "periods": periods,
                "llm_model": llm_model,
                "llm_key_present": llm_key_present,
                "llm_key_masked": _mask_key(settings_row.llm_api_key),
                "llm_status": "error",
                "llm_message": str(exc),
            },
            status_code=400,
        )
    return Response(status_code=303, headers={"Location": "/settings?llm_status=ok"})


@router.get("/export/json", response_class=PlainTextResponse)
def export_json_route(request: Request) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    result = executor.execute(session, "export.json", {})
    return PlainTextResponse(
        result["content"],
        headers={"Content-Disposition": "attachment; filename=lifeos_export.json"},
    )


@router.get("/export/markdown", response_class=PlainTextResponse)
def export_markdown_route(
    request: Request,
    scope: str,
    date_value: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    payload = {"scope": scope}
    if date_value:
        payload["date"] = _parse_date(date_value, _today())
    if year:
        payload["year"] = year
    if month:
        payload["month"] = month
    result = executor.execute(session, "export.markdown", payload)
    filename = f"lifeos_{scope}.md"
    return PlainTextResponse(
        result["content"],
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/import/json", response_class=HTMLResponse)
def import_json_route(
    request: Request,
    content: str = Form(...),
    mode: str = Form("overwrite"),
) -> Response:
    session = _get_session(request)
    executor = _get_executor(request)
    try:
        executor.execute(session, "import.json", {"content": content, "mode": mode})
    except RuntimeError as exc:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(exc), "retry": "/settings"},
            status_code=400,
        )
    return templates.TemplateResponse(
        "partials/log_save_result.html",
        {"request": request, "message": "settings.import_done"},
    )
