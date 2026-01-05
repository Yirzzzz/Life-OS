from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.domain.models import PlanItem


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
        session.commit()
        return PlanToggleOutput(
            plan_item_id=item.id,
            status=item.status,
            completed_at=item.completed_at,
        )


def get_skill() -> Skill:
    return PlanToggleCompleteSkill()
