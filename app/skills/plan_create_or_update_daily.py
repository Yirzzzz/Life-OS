from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.agent.base import Skill
from app.domain.models import DailyPlan, PlanItem


class PlanItemInput(BaseModel):
    title: str
    linked_goal_id: Optional[int] = None
    linked_habit_id: Optional[int] = None
    note: str = ""


class PlanCreateInput(BaseModel):
    date: date
    items: List[PlanItemInput] = Field(default_factory=list)


class PlanCreateOutput(BaseModel):
    plan_id: int
    items_created: int


class PlanCreateOrUpdateDailySkill(Skill):
    name = "plan.create_or_update_daily"
    description = "Create or update daily plan and append items."
    input_schema = PlanCreateInput
    output_schema = PlanCreateOutput

    def run(self, data: PlanCreateInput, context: dict) -> PlanCreateOutput:
        session: Session = context["session"]
        plan = session.exec(select(DailyPlan).where(DailyPlan.date == data.date)).first()
        if not plan:
            plan = DailyPlan(date=data.date)
            session.add(plan)
            session.commit()
            session.refresh(plan)

        created = 0
        for item in data.items:
            plan_item = PlanItem(
                daily_plan_id=plan.id,
                title=item.title,
                linked_goal_id=item.linked_goal_id,
                linked_habit_id=item.linked_habit_id,
                note=item.note,
            )
            session.add(plan_item)
            created += 1
        session.commit()
        return PlanCreateOutput(plan_id=plan.id, items_created=created)


def get_skill() -> Skill:
    return PlanCreateOrUpdateDailySkill()
