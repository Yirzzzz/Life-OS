from __future__ import annotations

from typing import Dict, List, Type

from sqlmodel import Session, delete

from app.domain.models import (
    DayLog,
    DailyPlan,
    Goal,
    HabitTemplate,
    Milestone,
    PlanItem,
    PlanItemSuppression,
    Settings,
    ShortTermObjective,
    Suggestion,
    SuggestionDecision,
)


MODEL_MAP: Dict[str, Type] = {
    "goals": Goal,
    "milestones": Milestone,
    "habits": HabitTemplate,
    "objectives": ShortTermObjective,
    "daily_plans": DailyPlan,
    "plan_items": PlanItem,
    "plan_item_suppressions": PlanItemSuppression,
    "day_logs": DayLog,
    "suggestions": Suggestion,
    "suggestion_decisions": SuggestionDecision,
    "settings": Settings,
}


def import_json(session: Session, payload: Dict[str, List[dict]], mode: str) -> None:
    if mode not in {"overwrite", "merge"}:
        raise ValueError("mode must be overwrite or merge")

    if mode == "overwrite":
        for model in MODEL_MAP.values():
            session.exec(delete(model))
        session.commit()

    for key, model in MODEL_MAP.items():
        items = payload.get(key, [])
        for raw in items:
            if key == "habits" and not raw.get("start_date"):
                raw["start_date"] = "2026-01-01"
            instance = model(**raw)
            if mode == "merge":
                session.merge(instance)
            else:
                session.add(instance)
    session.commit()
