from __future__ import annotations

from typing import Optional

from pydantic import BaseModel
from sqlmodel import Session, select

from app.agent.base import Skill
from app.domain.models import Habit


class HabitInput(BaseModel):
    id: Optional[int] = None
    title: str
    frequency: str
    target_per_week: int = 7
    preferred_period: str = "morning"
    active: bool = True


class HabitOutput(BaseModel):
    id: int
    title: str


class HabitCreateOrUpdateSkill(Skill):
    name = "habit.create_or_update"
    description = "Create or update habit."
    input_schema = HabitInput
    output_schema = HabitOutput

    def run(self, data: HabitInput, context: dict) -> HabitOutput:
        session: Session = context["session"]
        if data.id:
            habit = session.exec(select(Habit).where(Habit.id == data.id)).first()
            if not habit:
                raise ValueError("Habit not found")
        else:
            habit = Habit(title=data.title, frequency=data.frequency)
        habit.title = data.title
        habit.frequency = data.frequency
        habit.target_per_week = data.target_per_week
        habit.preferred_period = data.preferred_period
        habit.active = data.active
        session.add(habit)
        session.commit()
        session.refresh(habit)
        return HabitOutput(id=habit.id, title=habit.title)


def get_skill() -> Skill:
    return HabitCreateOrUpdateSkill()
