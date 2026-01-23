from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.agent.goal_analysis_graph import GoalAnalysisGraph
from app.data.repo import get_settings
from app.domain.models import (
    DayLog,
    DailyPlan,
    Goal,
    Milestone,
    PlanItem,
    ShortTermObjective,
    Suggestion,
    NextStepFeedback,
)

logger = logging.getLogger(__name__)


class GoalAnalysisInput(BaseModel):
    goal_id: int
    as_of: date
    existing_id: Optional[int] = None
    trigger: Optional[str] = None
    mode: Optional[str] = None
    lang: str = "zh"


class GoalAnalysisOutput(BaseModel):
    progress_summary: str
    highlights: List[Dict[str, Any]]
    risks: List[str] = ["暂无风险事件"]
    next_steps: List[Dict[str, Any] | str]
    assumptions: List[str]
    ask_back: str
    notice: str
    metrics: Dict[str, Any]


class GoalAnalysisSkill(Skill):
    name = "review.goal_analysis"
    description = "Generate a goal analysis card for a specific goal."
    input_schema = GoalAnalysisInput
    output_schema = GoalAnalysisOutput

    def run(self, data: GoalAnalysisInput, context: dict) -> GoalAnalysisOutput:
        session: Session = context["session"]
        goal = session.exec(select(Goal).where(Goal.id == data.goal_id)).first()
        if not goal:
            raise RuntimeError("Goal not found")

        window_start, window_end, window_days = _goal_window(goal, data.as_of)
        milestones = session.exec(
            select(Milestone).where(Milestone.goal_id == goal.id)
        ).all()
        objectives = session.exec(
            select(ShortTermObjective).where(ShortTermObjective.linked_goal_id == goal.id)
        ).all()
        logs = session.exec(
            select(DayLog).where(DayLog.date >= window_start, DayLog.date <= window_end)
        ).all()
        plans = session.exec(
            select(DailyPlan).where(
                DailyPlan.date >= window_start, DailyPlan.date <= window_end
            )
        ).all()
        plan_by_id = {plan.id: plan.date for plan in plans if plan.id}
        plan_ids_list = list(plan_by_id.keys())
        plan_items = (
            session.exec(select(PlanItem).where(PlanItem.daily_plan_id.in_(plan_ids_list))).all()
            if plan_ids_list
            else []
        )
        objective_ids = {obj.id for obj in objectives if obj.id}
        goal_items = [
            item
            for item in plan_items
            if item.linked_goal_id == goal.id
            or (item.linked_objective_id in objective_ids)
        ]
        feedback_start = goal.created_at.date() if goal.created_at else window_start
        feedback_start_dt = datetime.combine(feedback_start, datetime.min.time())
        feedback_end_dt = datetime.combine(data.as_of, datetime.max.time())
        next_step_feedback = session.exec(
            select(NextStepFeedback).where(
                NextStepFeedback.goal_id == goal.id,
                NextStepFeedback.created_at >= feedback_start_dt,
                NextStepFeedback.created_at <= feedback_end_dt,
            )
        ).all()

        payload = _build_payload(
            goal=goal,
            milestones=milestones,
            objectives=objectives,
            logs=logs,
            plan_items=goal_items,
            plan_by_id=plan_by_id,
            next_step_feedback=next_step_feedback,
            window_start=window_start,
            window_end=window_end,
            window_days=window_days,
            lang=data.lang,
        )

        notice = ""
        settings = get_settings(session)
        env_key = os.getenv("LIFEOS_LLM_API_KEY", "").strip()
        env_model = os.getenv("LIFEOS_LLM_MODEL", "").strip()
        llm_key = env_key or (settings.llm_api_key if settings else "")
        llm_model = env_model or (settings.llm_model if settings else "")
        output = _run_rules_graph(payload, llm_model, llm_key)
        if not llm_key:
            notice = "暂无可用 LLM_API_KEY"
        force_rules = data.mode == "rules"
        if force_rules and llm_key:
            output["metrics"]["generator_mode"] = "llm_progress_replan_forced_rules"
        if notice:
            output["notice"] = notice

        metrics = output.get("metrics", {})
        if "generator_mode" not in metrics:
            metrics["generator_mode"] = "llm_progress_replan"
        metrics["goal_id"] = goal.id
        metrics["as_of"] = data.as_of.isoformat()
        if data.trigger == "manual_regenerate":
            metrics["regenerated_at"] = datetime.utcnow().isoformat()
        output["metrics"] = metrics
        _apply_next_step_keys_and_filter(session, goal.id, output)

        suggestion = None
        if data.existing_id:
            suggestion = session.exec(
                select(Suggestion).where(Suggestion.id == data.existing_id)
            ).first()
        if suggestion:
            suggestion.reason = output.get("progress_summary", "")
            suggestion.metrics_json = output
            session.add(suggestion)
            session.commit()
        else:
            suggestion = Suggestion(
                habit_id=None,
                type="goal_analysis",
                reason=output.get("progress_summary", ""),
                metrics_json=output,
            )
            session.add(suggestion)
            session.commit()

        return GoalAnalysisOutput(**output)


def _normalize_step_text(text: str) -> str:
    cleaned = (text or "").lower().strip()
    cleaned = re.sub(r"[^\w\s]", "", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned


def _step_key(goal_id: int, step_text: str) -> str:
    normalized = _normalize_step_text(step_text)
    payload = f"{goal_id}:{normalized}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _apply_next_step_keys_and_filter(
    session: Session, goal_id: int, output: Dict[str, Any]
) -> None:
    steps = output.get("next_steps") or []
    if not isinstance(steps, list):
        return
    filtered_steps = []
    for item in steps:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                item["step_key"] = _step_key(goal_id, text)
            filtered_steps.append(item)
        else:
            filtered_steps.append(item)
    output["next_steps"] = filtered_steps

    step_keys = [
        item.get("step_key")
        for item in filtered_steps
        if isinstance(item, dict) and item.get("step_key")
    ]
    if not step_keys:
        output["next_steps_filtered_count"] = 0
        return

    now = datetime.utcnow()
    permanent_reasons = {"completed", "not_needed"}
    ttl_days = {
        "inaccurate": 30,
        "deprioritized": 14,
        "has_alternative": 30,
        "other": 30,
    }

    feedback = session.exec(
        select(NextStepFeedback)
        .where(
            NextStepFeedback.goal_id == goal_id,
            NextStepFeedback.step_key.in_(step_keys),
        )
        .order_by(NextStepFeedback.created_at.desc())
    ).all()

    latest_by_key: Dict[str, NextStepFeedback] = {}
    for fb in feedback:
        if fb.step_key not in latest_by_key:
            latest_by_key[fb.step_key] = fb

    filtered = []
    for item in filtered_steps:
        if not isinstance(item, dict) or not item.get("step_key"):
            filtered.append(item)
            continue
        step_key = item["step_key"]
        fb = latest_by_key.get(step_key)
        exclude = False
        if fb:
            action = fb.action
            reason = fb.reason
            if action in ("accepted", "completed"):
                exclude = True
            elif action in ("rejected", "dismissed"):
                if reason in permanent_reasons:
                    exclude = True
                else:
                    ttl = ttl_days.get(reason)
                    if ttl is not None and now - fb.created_at < timedelta(days=ttl):
                        exclude = True
            elif action == "delayed":
                if fb.snooze_until and now < fb.snooze_until:
                    exclude = True
        if not exclude:
            filtered.append(item)
    output["next_steps_filtered_count"] = len(filtered_steps) - len(filtered)
    output["next_steps"] = filtered


def _goal_window(goal: Goal, as_of: date) -> tuple[date, date, int]:
    start_date = goal.start_date or as_of
    end_date = goal.end_date if goal.end_date and goal.end_date < as_of else as_of
    window_start = start_date
    window_end = end_date
    window_days = max((window_end - window_start).days + 1, 1)
    return window_start, window_end, window_days


def _serialize_log(log: DayLog) -> Dict[str, Any]:
    return {
        "date": log.date.isoformat(),
        "journal_md": log.journal_md or "",
        "period_entries": log.period_entries or [],
        "tags": log.tags or [],
    }


def _serialize_plan_item(item: PlanItem, plan_date: Optional[date]) -> Dict[str, Any]:
    return {
        "date": plan_date.isoformat() if plan_date else "",
        "title": item.title or "",
        "status": item.status or "",
        "completed_at": item.completed_at.isoformat() if item.completed_at else "",
        "note": item.note or "",
    }


def _serialize_next_step_feedback(
    feedback: NextStepFeedback,
) -> Dict[str, Any]:
    return {
        "id": feedback.id,
        "suggestion_id": feedback.suggestion_id,
        "step_key": feedback.step_key,
        "step_text": feedback.step_text_snapshot or "",
        "action": feedback.action,
        "reason": feedback.reason,
        "reason_detail": feedback.reason_detail or "",
        "completion_note": feedback.completion_note or "",
        "snooze_until": feedback.snooze_until.isoformat()
        if feedback.snooze_until
        else "",
        "user_due_date": feedback.user_due_date.isoformat()
        if feedback.user_due_date
        else "",
        "created_short_term_objective_id": feedback.created_short_term_objective_id,
        "created_plan_item_id": feedback.created_plan_item_id,
        "created_at": feedback.created_at.isoformat() if feedback.created_at else "",
    }


def _build_payload(
    goal: Goal,
    milestones: List[Milestone],
    objectives: List[ShortTermObjective],
    logs: List[DayLog],
    plan_items: List[PlanItem],
    plan_by_id: Dict[int, date],
    next_step_feedback: List[NextStepFeedback],
    window_start: date,
    window_end: date,
    window_days: int,
    lang: str,
) -> Dict[str, Any]:
    logs_payload = [_serialize_log(log) for log in logs]
    plan_items_payload = [
        _serialize_plan_item(item, plan_by_id.get(item.daily_plan_id)) for item in plan_items
    ]
    next_step_feedback_payload = [
        _serialize_next_step_feedback(feedback) for feedback in next_step_feedback
    ]
    objectives_payload = [
        {
            "id": obj.id,
            "title": obj.title,
            "due_date": obj.due_date.isoformat(),
            "status": obj.status,
            "note": obj.note or "",
            "created_at": obj.created_at.date().isoformat() if obj.created_at else "",
        }
        for obj in objectives
    ]
    milestones_payload = [
        {
            "id": ms.id,
            "title": ms.title,
            "due_date": ms.due_date.isoformat(),
            "status": ms.status,
        }
        for ms in milestones
    ]

    raw_evidence: List[Dict[str, Any]] = []
    for log in logs_payload:
        if (log.get("journal_md") or "").strip():
            raw_evidence.append(
                {
                    "id": f"log-{log['date']}",
                    "source_type": "log",
                    "text": log.get("journal_md", ""),
                    "date": log.get("date"),
                    "tags": log.get("tags", []),
                    "link": f"/logs?target_date={log['date']}",
                }
            )
        for entry in log.get("period_entries", []) or []:
            if (entry.get("text") or "").strip():
                raw_evidence.append(
                    {
                        "id": f"log-{log['date']}-{entry.get('period', '')}",
                        "source_type": "log",
                        "text": entry.get("text", ""),
                        "date": log.get("date"),
                        "tags": entry.get("tags", []),
                        "link": f"/logs?target_date={log['date']}",
                    }
                )
    for item in plan_items_payload:
        if (item.get("title") or "").strip():
            date_value = item.get("date") or ""
            raw_evidence.append(
                {
                    "id": f"plan-{date_value}-{item.get('title', '')}",
                    "source_type": "plan",
                    "text": item.get("title", ""),
                    "date": date_value,
                    "tags": [],
                    "link": f"/plans?target_date={date_value}" if date_value else "/plans",
                }
            )
    for obj in objectives_payload:
        raw_evidence.append(
            {
                "id": f"objective-{obj.get('id')}",
                "source_type": "objective",
                "text": obj.get("title", ""),
                "date": obj.get("due_date"),
                "tags": [],
                "link": "/goals",
            }
        )
    for ms in milestones_payload:
        raw_evidence.append(
            {
                "id": f"milestone-{ms.get('id')}",
                "source_type": "milestone",
                "text": ms.get("title", ""),
                "date": ms.get("due_date"),
                "tags": [],
                "link": "/goals",
            }
        )

    return {
        "lang": lang,
        "goal": {
            "id": goal.id,
            "title": goal.title,
            "description_md": goal.description_md or "",
            "start_date": goal.start_date.isoformat(),
            "end_date": goal.end_date.isoformat(),
        },
        "milestones": milestones_payload,
        "short_term_objectives": objectives_payload,
        "recent_logs": logs_payload,
        "recent_plan_items": plan_items_payload,
        "recent_next_step_feedback": next_step_feedback_payload,
        "recent_habits": [],
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "days": window_days,
        },
        "raw_evidence": raw_evidence,
    }


def _run_rules_graph(
    payload: Dict[str, Any], model_key: str, api_key: str
) -> Dict[str, Any]:
    graph = GoalAnalysisGraph()
    result = graph.run(payload, model_key, api_key)
    output = result.get("output", {})
    node_a = result.get("node_a", {})
    related_events = node_a.get("related_events") or []
    evidence_quotes = [
        {
            "id": f"event-{idx}",
            "quote": item.get("quote", ""),
            "link": item.get("url", ""),
            "source_type": item.get("source_type", ""),
            "date": item.get("date", ""),
        }
        for idx, item in enumerate(related_events)
        if item.get("quote")
    ]
    matched_evidence = [
        {
            "id": f"event-{idx}",
            "quote": item.get("quote", ""),
            "link": item.get("url", ""),
            "source_type": item.get("source_type", ""),
            "date": item.get("date", ""),
            "score": 1.0,
        }
        for idx, item in enumerate(related_events)
        if item.get("quote")
    ]
    node_a["evidence_quotes"] = evidence_quotes
    output["intent"] = node_a
    output["evidence"] = {"matched_evidence": matched_evidence}
    output["plan"] = result.get("node_b", {})
    return output


def get_skill() -> Skill:
    return GoalAnalysisSkill()
