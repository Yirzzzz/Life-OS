from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import markdown
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.agent.executor import Executor
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


def _get_session(request: Request) -> Session:
    return request.app.state.session()


def _get_executor(request: Request) -> Executor:
    return request.app.state.executor


def _get_periods(session: Session) -> List[str]:
    return ["morning", "afternoon", "evening"]


def _period_labels() -> Dict[str, str]:
    return {"morning": "上午", "afternoon": "下午", "evening": "晚上"}


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


def _sync_plan_items(session: Session, plan: DailyPlan) -> None:
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    existing_habit_ids = {item.linked_habit_id for item in items if item.linked_habit_id}
    existing_objective_ids = {
        item.linked_objective_id for item in items if item.linked_objective_id
    }
    suppressions = session.exec(
        select(PlanItemSuppression).where(PlanItemSuppression.date == plan.date)
    ).all()
    suppressed_habit_ids = {
        row.linked_habit_id for row in suppressions if row.linked_habit_id
    }
    suppressed_objective_ids = {
        row.linked_objective_id for row in suppressions if row.linked_objective_id
    }
    changed = False

    templates = session.exec(
        select(HabitTemplate).where(HabitTemplate.active == True)  # noqa: E712
    ).all()
    week_start = _week_start(plan.date)
    week_end = week_start + timedelta(days=6)
    for template in templates:
        if not _template_included(template, plan.date):
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
        select(ShortTermObjective).where(ShortTermObjective.status != "completed")
    ).all()
    for obj in objectives:
        if obj.status == "pending" and obj.due_date < plan.date:
            obj.status = "expired"
            session.add(obj)
            changed = True
    for obj in objectives:
        if obj.status != "pending":
            continue
        if obj.due_date < plan.date:
            continue
        if obj.id in existing_objective_ids:
            continue
        if obj.id in suppressed_objective_ids:
            continue
        session.add(
            PlanItem(
                daily_plan_id=plan.id,
                title=obj.title,
                linked_goal_id=obj.linked_goal_id,
                linked_objective_id=obj.id,
                status="pending",
            )
        )
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


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> Response:
    session = _get_session(request)
    today = _today()
    periods = _get_periods(session)
    log = _ensure_day_log(session, today)
    period_rows = _build_period_rows(log, periods)
    plan = _ensure_daily_plan(session, today)
    _sync_plan_items(session, plan)
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    completed = len([item for item in items if item.completed_at])
    habits = session.exec(select(HabitTemplate).where(HabitTemplate.active == True)).all()  # noqa: E712
    suggestions = session.exec(select(Suggestion).where(Suggestion.status == "open")).all()

    overload = False
    overload_reason = ""
    if len(items) > 8:
        overload = True
        overload_reason = "今日计划超过 8 项，建议减少密度或拆分。"
    else:
        week_start = today - timedelta(days=6)
        week_plans = session.exec(
            select(DailyPlan).where(DailyPlan.date >= week_start, DailyPlan.date <= today)
        ).all()
        plan_ids = [plan.id for plan in week_plans]
        week_items = session.exec(
            select(PlanItem).where(PlanItem.daily_plan_id.in_(plan_ids))
        ).all() if plan_ids else []
        week_completed = len([item for item in week_items if item.completed_at])
        week_rate = week_completed / len(week_items) if week_items else 1
        if week_rate < 0.3:
            overload = True
            overload_reason = "连续一周完成率偏低，建议降低计划数量。"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "today": today,
            "plan_items": items,
            "completed": completed,
            "habits": habits,
            "log": log,
            "suggestions": suggestions,
            "overload": overload,
            "overload_reason": overload_reason,
            "period_rows": period_rows,
            "periods": periods,
            "period_labels": _period_labels(),
        },
    )


@router.get("/goals", response_class=HTMLResponse)
def goals(request: Request) -> Response:
    session = _get_session(request)
    goals_list = session.exec(select(Goal)).all()
    milestones = session.exec(select(Milestone)).all()
    return templates.TemplateResponse(
        "goals.html",
        {"request": request, "goals": goals_list, "milestones": milestones},
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
    goal = session.exec(select(Goal).where(Goal.id == goal_id)).first()
    if not goal:
        return Response(status_code=404)
    milestones = session.exec(select(Milestone).where(Milestone.goal_id == goal.id)).all()
    return templates.TemplateResponse(
        "partials/goal_card.html",
        {"request": request, "goal": goal, "milestones": milestones},
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
        {"request": request, "goal": goal, "milestones": milestones},
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
    items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    habits = session.exec(select(HabitTemplate).where(HabitTemplate.active == True)).all()  # noqa: E712
    goals_list = session.exec(select(Goal)).all()
    return templates.TemplateResponse(
        "plans.html",
        {
            "request": request,
            "selected_date": selected_date,
            "plan_items": items,
            "habits": habits,
            "goals": goals_list,
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
    plan_date = _parse_date(date_value, _today())
    plan = _ensure_daily_plan(session, plan_date)
    _sync_plan_items(session, plan)
    return Response(status_code=303, headers={"Location": f"/plans?target_date={date_value}"})


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
    recent_start = _today() - timedelta(days=30)
    logs_recent = session.exec(
        select(DayLog).where(DayLog.date >= recent_start, DayLog.date <= _today())
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
    start = _parse_date(start_date, _today() - timedelta(days=30))
    end = _parse_date(end_date, _today())
    logs = session.exec(select(DayLog).where(DayLog.date >= start, DayLog.date <= end)).all()

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
        {"request": request, "message": "已保存"},
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
    suggestions = session.exec(select(Suggestion).where(Suggestion.status == "open")).all()
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
    suggestions = session.exec(select(Suggestion).where(Suggestion.status == "open")).all()
    return templates.TemplateResponse(
        "partials/suggestions.html",
        {"request": request, "suggestions": suggestions},
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
    return templates.TemplateResponse(
        "settings.html", {"request": request, "periods": periods}
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
        {"request": request, "message": "导入完成"},
    )
