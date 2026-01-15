from __future__ import annotations

from app.agent.registry import SkillRegistry
from app.skills.agent_generate_suggestions import get_skill as agent_generate_suggestions
from app.skills.export_json_skill import get_skill as export_json
from app.skills.export_markdown_skill import get_skill as export_markdown
from app.skills.habit_create_or_update import get_skill as habit_create_or_update
from app.skills.import_json_skill import get_skill as import_json
from app.skills.log_upsert_day_log import get_skill as log_upsert_day_log
from app.skills.plan_create_or_update_daily import get_skill as plan_create_or_update_daily
from app.skills.plan_toggle_complete import get_skill as plan_toggle_complete
from app.skills.review_generate_monthly import get_skill as review_generate_monthly
from app.skills.review_generate_yearly import get_skill as review_generate_yearly
from app.skills.goal_analysis import get_skill as review_goal_analysis
from app.skills.review_weekly_reflection import get_skill as review_weekly_reflection


def register_skills(registry: SkillRegistry) -> None:
    registry.register(plan_create_or_update_daily())
    registry.register(plan_toggle_complete())
    registry.register(habit_create_or_update())
    registry.register(log_upsert_day_log())
    registry.register(review_generate_monthly())
    registry.register(review_generate_yearly())
    registry.register(review_goal_analysis())
    registry.register(review_weekly_reflection())
    registry.register(agent_generate_suggestions())
    registry.register(export_json())
    registry.register(import_json())
    registry.register(export_markdown())
