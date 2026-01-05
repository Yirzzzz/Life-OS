from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.domain.models import DailyPlan, HabitTemplate, PlanItem, ShortTermObjective


def _week_start(target_date: date) -> date:
    return target_date - timedelta(days=target_date.weekday())


class PlanToggleInput(BaseModel):
    plan_item_id: int
    completed: bool
    note: Optional[str] = None


class PlanToggleOutput(BaseModel):
    plan_item_id: int
    status: str
    completed_at: Optional[datetime]


class PlanToggleCompleteSkill(Skill):
    name = "plan.toggle_complete"
    description = "Toggle plan item completion."
    input_schema = PlanToggleInput
    output_schema = PlanToggleOutput

    def run(self, data: PlanToggleInput, context: dict) -> PlanToggleOutput:
        session: Session = context["session"]
        item = session.exec(select(PlanItem).where(PlanItem.id == data.plan_item_id)).first()
        if not item:
            raise ValueError("Plan item not found")
        if data.completed:
            item.status = "completed"
            item.completed_at = datetime.utcnow()
        else:
            item.status = "pending"
            item.completed_at = None
        if data.note is not None:
            item.note = data.note
        session.add(item)
        if item.linked_objective_id:
            objective = session.exec(
                select(ShortTermObjective).where(ShortTermObjective.id == item.linked_objective_id)
            ).first()
            if objective:
                objective.status = "completed" if data.completed else "pending"
                session.add(objective)
        if item.linked_habit_id:
            template = session.exec(
                select(HabitTemplate).where(HabitTemplate.id == item.linked_habit_id)
            ).first()
            if template and template.frequency == "weekly":
                plan = session.exec(
                    select(DailyPlan).where(DailyPlan.id == item.daily_plan_id)
                ).first()
                if plan:
                    week_start = _week_start(plan.date)
                    week_end = week_start + timedelta(days=6)
                    week_items = session.exec(
                        select(PlanItem)
                        .join(DailyPlan, PlanItem.daily_plan_id == DailyPlan.id)
                        .where(
                            PlanItem.linked_habit_id == template.id,
                            DailyPlan.date >= week_start,
                            DailyPlan.date <= week_end,
                        )
                    ).all()
                    if data.completed:
                        completed_at = item.completed_at or datetime.utcnow()
                        for week_item in week_items:
                            week_item.status = "completed"
                            week_item.completed_at = completed_at
                            session.add(week_item)
                    else:
                        remaining = any(
                            week_item.completed_at
                            for week_item in week_items
                            if week_item.id != item.id
                        )
                        if not remaining:
                            for week_item in week_items:
                                week_item.status = "pending"
                                week_item.completed_at = None
                                session.add(week_item)
        session.commit()
        return PlanToggleOutput(
            plan_item_id=item.id,
            status=item.status,
            completed_at=item.completed_at,
        )


def get_skill() -> Skill:
    return PlanToggleCompleteSkill()
