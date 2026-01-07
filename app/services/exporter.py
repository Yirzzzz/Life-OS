from __future__ import annotations

import json
from datetime import date
from typing import Dict, List

from sqlmodel import Session, select

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
from app.services.review import generate_monthly_review, generate_yearly_review


def export_json(session: Session) -> Dict[str, object]:
    return {
        "goals": session.exec(select(Goal)).all(),
        "milestones": session.exec(select(Milestone)).all(),
        "habits": session.exec(select(HabitTemplate)).all(),
        "objectives": session.exec(select(ShortTermObjective)).all(),
        "daily_plans": session.exec(select(DailyPlan)).all(),
        "plan_items": session.exec(select(PlanItem)).all(),
        "plan_item_suppressions": session.exec(select(PlanItemSuppression)).all(),
        "day_logs": session.exec(select(DayLog)).all(),
        "suggestions": session.exec(select(Suggestion)).all(),
        "suggestion_decisions": session.exec(select(SuggestionDecision)).all(),
        "settings": session.exec(select(Settings)).all(),
    }


def export_json_text(session: Session) -> str:
    payload = export_json(session)
    serializable: Dict[str, List[Dict[str, object]]] = {}
    for key, value in payload.items():
        items = [item.dict() for item in value]
        if key == "settings":
            for item in items:
                item["llm_api_key"] = ""
        serializable[key] = items
    return json.dumps(serializable, ensure_ascii=False, indent=2, default=str)


def export_daily_markdown(session: Session, target_date: date) -> str:
    plan = session.exec(select(DailyPlan).where(DailyPlan.date == target_date)).first()
    items: List[PlanItem] = []
    if plan:
        items = session.exec(select(PlanItem).where(PlanItem.daily_plan_id == plan.id)).all()
    log = session.exec(select(DayLog).where(DayLog.date == target_date)).first()

    lines = [f"# {target_date} 日志"]
    lines.append("")
    lines.append("## 计划")
    if items:
        for item in items:
            status = "x" if item.completed_at else " "
            lines.append(f"- [{status}] {item.title}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 时段记录")
    if log and log.period_entries:
        for entry in log.period_entries:
            period = entry.get("period", "unknown")
            text = entry.get("text", "")
            lines.append(f"- **{period}** {text}")
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 日记")
    if log and log.journal_md:
        lines.append(log.journal_md)
    else:
        lines.append("无")
    return "\n".join(lines)


def export_monthly_markdown(session: Session, year: int, month: int) -> str:
    review = generate_monthly_review(session, year, month)
    lines = [f"# {review['title']} 月度复盘", ""]
    lines.append(review["narrative"])
    lines.append("")
    lines.append("## 统计")
    lines.append(f"- 完成率: {review['completion_rate']:.0%}")
    lines.append(f"- 完成数: {review['completed_items']}")
    lines.append(f"- 计划总数: {review['total_items']}")
    lines.append(f"- 活跃天数: {review['active_days']}")
    lines.append("")
    lines.append("## TOP 习惯")
    for name, count in review["top_habits"]:
        lines.append(f"- {name}: {count} 次")
    if not review["top_habits"]:
        lines.append("- 无")
    lines.append("")
    lines.append("## 时段分布")
    for period, count in review["period_counts"].items():
        lines.append(f"- {period}: {count}")
    if not review["period_counts"]:
        lines.append("- 无")
    return "\n".join(lines)


def export_yearly_markdown(session: Session, year: int) -> str:
    review = generate_yearly_review(session, year)
    lines = [f"# {review['title']} 年度复盘", ""]
    lines.append(review["narrative"])
    lines.append("")
    lines.append("## 统计")
    lines.append(f"- 完成率: {review['completion_rate']:.0%}")
    lines.append(f"- 完成数: {review['completed_items']}")
    lines.append(f"- 计划总数: {review['total_items']}")
    lines.append(f"- 活跃天数: {review['active_days']}")
    lines.append("")
    lines.append("## TOP 习惯")
    for name, count in review["top_habits"]:
        lines.append(f"- {name}: {count} 次")
    if not review["top_habits"]:
        lines.append("- 无")
    lines.append("")
    lines.append("## 时段分布")
    for period, count in review["period_counts"].items():
        lines.append(f"- {period}: {count}")
    if not review["period_counts"]:
        lines.append("- 无")
    return "\n".join(lines)
