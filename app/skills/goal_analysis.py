from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
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
)
from app.skills.review_weekly_reflection import (
    LlmCallError,
    _call_llm,
    _clean_llm_text,
    _llm_notice,
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
    next_steps: List[str]
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

        payload = _build_payload(
            goal=goal,
            milestones=milestones,
            objectives=objectives,
            logs=logs,
            plan_items=goal_items,
            plan_by_id=plan_by_id,
            window_start=window_start,
            window_end=window_end,
            window_days=window_days,
            lang=data.lang,
        )

        generator_mode = "rules"
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
            generator_mode = "llm_multi_node_forced_rules"
        if notice:
            output["notice"] = notice

        metrics = output.get("metrics", {})
        metrics["generator_mode"] = generator_mode
        metrics["goal_id"] = goal.id
        metrics["as_of"] = data.as_of.isoformat()
        if data.trigger == "manual_regenerate":
            metrics["regenerated_at"] = datetime.utcnow().isoformat()
        output["metrics"] = metrics

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


def _goal_window(goal: Goal, as_of: date) -> tuple[date, date, int]:
    start_date = goal.start_date or as_of
    days_since = max(1, (as_of - start_date).days + 1)
    window_days = min(max(days_since, 7), 21)
    window_end = as_of
    window_start = max(start_date, window_end - timedelta(days=window_days - 1))
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


def _build_payload(
    goal: Goal,
    milestones: List[Milestone],
    objectives: List[ShortTermObjective],
    logs: List[DayLog],
    plan_items: List[PlanItem],
    plan_by_id: Dict[int, date],
    window_start: date,
    window_end: date,
    window_days: int,
    lang: str,
) -> Dict[str, Any]:
    logs_payload = [_serialize_log(log) for log in logs]
    plan_items_payload = [
        _serialize_plan_item(item, plan_by_id.get(item.daily_plan_id)) for item in plan_items
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
    output["intent"] = result.get("node_a", {})
    output["evidence"] = result.get("node_c", {})
    output["plan"] = result.get("node_b", {})
    return output


def _build_prompt(payload: Dict[str, Any]) -> str:
    schema = (
        '{"progress_summary": str, "highlights": [{"text": str, "evidence_ids": [str]}], '
        '"risks": [str], "next_steps": [str], "assumptions": [str], '
        '"ask_back": str, "notice": str, '
        '"metrics": {"coverage": number, "alignment": number, "confidence": number, '
        '"window_start": str, "window_end": str, "generator_mode": str}}'
    )
    return (
        "Return ONLY valid JSON matching this schema. "
        "No markdown, no explanations. "
        f"Schema: {schema}. "
        f"context: {json.dumps(payload, ensure_ascii=False)}"
    )


def _try_llm_generation(
    model_key: str, api_key: str, payload: Dict[str, Any]
) -> tuple[Optional[Dict[str, Any]], str, Dict[str, Any]]:
    prompt = _build_prompt(payload)
    debug: Dict[str, Any] = {}
    try:
        content, finish_reason, used_response_format, response_meta = _call_llm(
            model_key, api_key, prompt, payload.get("lang", "zh")
        )
    except LlmCallError as exc:
        debug = exc.details
        return None, "llm_error", debug
    debug["llm_finish_reason"] = finish_reason
    debug["llm_response_format"] = used_response_format
    if response_meta:
        debug.update(response_meta)
    if not content:
        return None, "llm_empty", debug
    cleaned = _clean_llm_text(content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        debug["llm_parse_error"] = f"{exc}"
        debug["llm_raw_text"] = content[:1200]
        return None, "llm_invalid_json", debug
    return parsed, "", debug


def get_skill() -> Skill:
    return GoalAnalysisSkill()
