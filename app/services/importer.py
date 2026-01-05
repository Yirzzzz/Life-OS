from __future__ import annotations

from typing import Dict, List, Type

from sqlmodel import Session, delete

from app.domain.models import (
    DayLog,
    DailyPlan,
    Goal,
    Habit,
    Milestone,
    PlanItem,
    Settings,
    Suggestion,
    SuggestionDecision,
)


MODEL_MAP: Dict[str, Type] = {
    "goals": Goal,
    "milestones": Milestone,
    "habits": Habit,
    "daily_plans": DailyPlan,
    "plan_items": PlanItem,
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
            instance = model(**raw)
            if mode == "merge":
                session.merge(instance)
            else:
                session.add(instance)
    session.commit()
