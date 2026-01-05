from __future__ import annotations

import time
from typing import Any, Dict

from pydantic import ValidationError
from sqlmodel import Session

from app.agent.registry import SkillRegistry
from app.domain.models import AgentRunLog


class Executor:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry

    def execute(self, session: Session, skill_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        start = time.time()
        status = "success"
        error = ""
        output_payload: Dict[str, Any] = {}

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
            log = AgentRunLog(
                skill_name=skill_name,
                input_json=payload,
                output_json=output_payload,
                status=status,
                error=error,
                duration_ms=duration_ms,
            )
            session.add(log)
            session.commit()

        if status == "error":
            raise RuntimeError(error)

        return output_payload
