from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel


class Goal(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str
    title: str
    start_date: date
    end_date: date
    description_md: str = Field(default="", sa_column=Column(Text))
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Milestone(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    goal_id: int = Field(foreign_key="goal.id")
    title: str
    due_date: date
    status: str = "pending"


class HabitTemplate(SQLModel, table=True):
    __tablename__ = "habit"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    frequency: str
    preferred_period: str = "unknown"
    target_per_week: int = 7
    start_date: date = Field(default_factory=date.today)
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DailyPlan(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: date
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ShortTermObjective(SQLModel, table=True):
    __tablename__ = "short_term_objective"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    linked_goal_id: int = Field(foreign_key="goal.id")
    due_date: date
    status: str = "pending"
    note: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PlanItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    daily_plan_id: int = Field(foreign_key="dailyplan.id")
    title: str
    linked_goal_id: Optional[int] = Field(default=None, foreign_key="goal.id")
    linked_habit_id: Optional[int] = Field(default=None, foreign_key="habit.id")
    linked_objective_id: Optional[int] = Field(
        default=None, foreign_key="short_term_objective.id"
    )
    status: str = "pending"
    completed_at: Optional[datetime] = None
    note: str = ""


class PlanItemSuppression(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: date
    linked_habit_id: Optional[int] = Field(default=None, foreign_key="habit.id")
    linked_objective_id: Optional[int] = Field(
        default=None, foreign_key="short_term_objective.id"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DayLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: date
    period_entries: List[Dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    journal_md: str = Field(default="", sa_column=Column(Text))
    tags: List[str] = Field(default_factory=list, sa_column=Column(JSON))


class Suggestion(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    habit_id: Optional[int] = Field(default=None, foreign_key="habit.id")
    type: str
    reason: str
    metrics_json: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "open"


class SuggestionDecision(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    suggestion_id: int = Field(foreign_key="suggestion.id")
    decision: str
    note: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NextStepFeedback(SQLModel, table=True):
    __tablename__ = "next_step_feedback"
    id: Optional[int] = Field(default=None, primary_key=True)
    goal_id: int = Field(foreign_key="goal.id")
    suggestion_id: int = Field(foreign_key="suggestion.id")
    step_key: str
    step_text_snapshot: str = Field(default="", sa_column=Column(Text))
    action: str
    reason: str
    reason_detail: str = Field(default="", sa_column=Column(Text))
    snooze_until: Optional[datetime] = None
    user_due_date: Optional[date] = None
    created_short_term_objective_id: Optional[int] = Field(
        default=None, foreign_key="short_term_objective.id"
    )
    created_plan_item_id: Optional[int] = Field(default=None, foreign_key="planitem.id")
    completion_note: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AgentRunLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    skill_name: str
    input_json: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_json: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str
    error: str = ""
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Settings(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    periods_json: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    llm_api_key: str = Field(default="", sa_column=Column(Text))
    llm_model: str = "Qwen/Qwen2.5-Coder-32B-Instruct"
