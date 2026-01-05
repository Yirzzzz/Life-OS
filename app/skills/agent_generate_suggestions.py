from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.domain.models import HabitTemplate, Suggestion
from app.services.metrics import habit_metrics, habit_preferred_period


class AgentSuggestionInput(BaseModel):
    as_of: date


class AgentSuggestionOutput(BaseModel):
    created: int
    suggestion_ids: List[int]


class AgentGenerateSuggestionsSkill(Skill):
    name = "agent.generate_suggestions"
    description = "Generate habit suggestions based on last 30 days."
    input_schema = AgentSuggestionInput
    output_schema = AgentSuggestionOutput

    def run(self, data: AgentSuggestionInput, context: dict) -> AgentSuggestionOutput:
        session: Session = context["session"]
        habits = session.exec(
            select(HabitTemplate).where(HabitTemplate.active == True)  # noqa: E712
        ).all()
        created = 0
        suggestion_ids: List[int] = []
        for habit in habits:
            metrics = habit_metrics(session, habit, data.as_of)
            preferred_period, preferred_count = habit_preferred_period(session, habit, data.as_of)
            completion_rate = (
                metrics["completed_30"] / metrics["total_30"] if metrics["total_30"] else 0
            )
            suggestion_type = ""
            reason = ""
            if completion_rate < 0.1 and metrics["completed_14"] == 0:
                suggestion_type = "delete_or_replace"
                reason = "30天完成率低于10%且连续14天未完成，建议删除或拆成更小习惯。"
            elif 0.1 <= completion_rate < 0.3:
                suggestion_type = "reduce_or_shift"
                reason = "30天完成率在10%-30%，建议降频或调整执行时段。"
            elif completion_rate > 0.85 and metrics["streak"] >= 14:
                suggestion_type = "upgrade_or_bind"
                reason = "30天完成率高于85%且连续14天完成，建议升级或绑定目标。"
            else:
                continue

            last_week = datetime.utcnow() - timedelta(days=7)
            existing = session.exec(
                select(Suggestion).where(
                    Suggestion.habit_id == habit.id,
                    Suggestion.type == suggestion_type,
                    Suggestion.created_at >= last_week,
                )
            ).first()
            if existing:
                continue

            suggestion = Suggestion(
                habit_id=habit.id,
                type=suggestion_type,
                reason=reason,
                metrics_json={
                    **metrics,
                    "completion_rate_30": completion_rate,
                    "preferred_period": preferred_period,
                    "preferred_count": preferred_count,
                },
            )
            session.add(suggestion)
            session.commit()
            session.refresh(suggestion)
            created += 1
            suggestion_ids.append(suggestion.id)

        return AgentSuggestionOutput(created=created, suggestion_ids=suggestion_ids)


def get_skill() -> Skill:
    return AgentGenerateSuggestionsSkill()
