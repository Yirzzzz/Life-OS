from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app.db import engine
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


def seed_db() -> None:
    session = Session(engine)
    existing = session.exec(select(Goal)).first()
    if existing:
        return

    settings = Settings(id=1, periods_json=["morning", "afternoon", "evening"])
    session.add(settings)

    year_goal = Goal(
        type="year",
        title="2026 年健康与技能升级",
        start_date=date(date.today().year, 1, 1),
        end_date=date(date.today().year, 12, 31),
        description_md="提升健康体能与工程效率。",
        tags=["health", "career"],
    )
    phase_goal = Goal(
        type="phase",
        title="90 天产品迭代",
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=60),
        description_md="完成 Life OS v1 并验证使用习惯。",
        tags=["product"],
    )
    session.add(year_goal)
    session.add(phase_goal)
    session.commit()
    session.refresh(year_goal)
    session.refresh(phase_goal)

    milestones = [
        Milestone(goal_id=year_goal.id, title="完成年度体检", due_date=date.today() + timedelta(days=45)),
        Milestone(goal_id=phase_goal.id, title="上线 MVP", due_date=date.today() + timedelta(days=7)),
    ]
    session.add_all(milestones)

    habits = [
        Habit(title="晨跑 20 分钟", frequency="daily", target_per_week=5, preferred_period="morning"),
        Habit(title="阅读 30 分钟", frequency="daily", target_per_week=5, preferred_period="evening"),
        Habit(title="周复盘", frequency="weekly", target_per_week=1, preferred_period="afternoon"),
    ]
    session.add_all(habits)
    session.commit()

    today = date.today()
    for offset in range(5):
        target = today - timedelta(days=offset)
        plan = DailyPlan(date=target)
        session.add(plan)
        session.commit()
        session.refresh(plan)
        items = [
            PlanItem(
                daily_plan_id=plan.id,
                title="推进 Life OS 页面",
                linked_goal_id=phase_goal.id,
                status="completed" if offset % 2 == 0 else "pending",
                completed_at=datetime.utcnow() if offset % 2 == 0 else None,
            ),
            PlanItem(
                daily_plan_id=plan.id,
                title="晨跑 20 分钟",
                linked_habit_id=habits[0].id,
                status="completed" if offset % 3 == 0 else "pending",
                completed_at=datetime.utcnow() if offset % 3 == 0 else None,
            ),
            PlanItem(
                daily_plan_id=plan.id,
                title="阅读 30 分钟",
                linked_habit_id=habits[1].id,
                status="completed" if offset % 2 == 1 else "pending",
                completed_at=datetime.utcnow() if offset % 2 == 1 else None,
            ),
        ]
        session.add_all(items)
        log = DayLog(
            date=target,
            period_entries=[
                {"period": "morning", "text": "晨跑与早餐准备", "tags": ["health"]},
                {"period": "afternoon", "text": "推进接口与模板", "tags": ["product"]},
                {"period": "evening", "text": "阅读 30 分钟", "tags": ["learning"]},
            ],
            journal_md="今天节奏不错，准备继续优化页面结构。",
            tags=["health", "product"],
        )
        session.add(log)

    session.commit()

    suggestions = [
        Suggestion(
            habit_id=habits[2].id,
            type="reduce_or_shift",
            reason="近 30 天复盘执行率偏低，建议调整时段。",
            metrics_json={"completion_rate_30": 0.2, "streak": 0},
            created_at=datetime.utcnow() - timedelta(days=2),
        ),
        Suggestion(
            habit_id=habits[1].id,
            type="upgrade_or_bind",
            reason="阅读完成率高，可绑定年度目标。",
            metrics_json={"completion_rate_30": 0.9, "streak": 14},
            created_at=datetime.utcnow() - timedelta(days=1),
        ),
        Suggestion(
            habit_id=habits[0].id,
            type="delete_or_replace",
            reason="连续 14 天未完成，建议拆分为更小版本。",
            metrics_json={"completion_rate_30": 0.05, "streak": 0},
        ),
    ]
    session.add_all(suggestions)
    session.commit()
    accepted = SuggestionDecision(
        suggestion_id=suggestions[1].id, decision="accept", note="绑定到年度目标"
    )
    session.add(accepted)
    suggestions[1].status = "accept"
    session.add(suggestions[1])
    session.commit()
