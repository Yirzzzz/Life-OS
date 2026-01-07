from __future__ import annotations

from fastapi import FastAPI

from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.registry import SkillRegistry
from app.config import load_env
from app.db import get_session, init_db
from app.seed import seed_db
from app.skills import register_skills
from app.web.routes import router


def create_app() -> FastAPI:
    load_env()
    app = FastAPI(title="Life OS v1")
    registry = SkillRegistry()
    register_skills(registry)

    planner = Planner()
    planner.register_route("plan_create", "plan.create_or_update_daily")
    planner.register_route("plan_toggle", "plan.toggle_complete")
    planner.register_route("habit_upsert", "habit.create_or_update")
    planner.register_route("log_upsert", "log.upsert_day_log")
    planner.register_route("review_monthly", "review.generate_monthly")
    planner.register_route("review_yearly", "review.generate_yearly")
    planner.register_route("agent_suggestions", "agent.generate_suggestions")
    planner.register_route("export_json", "export.json")
    planner.register_route("import_json", "import.json")
    planner.register_route("export_markdown", "export.markdown")

    executor = Executor(registry)

    app.state.registry = registry
    app.state.planner = planner
    app.state.executor = executor
    app.state.session = get_session

    app.include_router(router)

    @app.on_event("startup")
    def _startup() -> None:
        init_db()

    return app


app = create_app()
