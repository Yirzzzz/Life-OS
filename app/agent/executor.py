from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Dict, Iterable

from pydantic import ValidationError
from sqlmodel import Session

from app.agent.registry import SkillRegistry
from app.domain.models import AgentRunLog


class Executor:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def _make_json_safe(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: self._make_json_safe(val) for key, val in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._make_json_safe(val) for val in value]
        return value

    def execute(self, session: Session, skill_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()
        status = "success"
        error = ""
        output_payload: Dict[str, Any] = {}

        skill = None
        try:
            skill = self.registry.get(skill_name)
            data = skill.input_schema(**payload)
            output = skill.run(data, {"session": session})
            output_payload = output.dict()
        except (ValidationError, KeyError, Exception) as exc:  # noqa: BLE001
            status = "error"
            error = str(exc)
            output_payload = {"error": error}
        finally:
            duration_ms = int((time.time() - start) * 1000)
            log_input = getattr(skill, "log_input", None) if skill else None
            if log_input is None:
                log_input = payload
            log = AgentRunLog(
                skill_name=skill_name,
                input_json=self._make_json_safe(log_input),
                output_json=self._make_json_safe(output_payload),
                status=status,
                error=error,
                duration_ms=duration_ms,
            )
            session.add(log)
            session.commit()

        if status == "error":
            raise RuntimeError(error)

        return output_payload
