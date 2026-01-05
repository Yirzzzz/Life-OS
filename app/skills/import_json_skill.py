from __future__ import annotations

import json
from typing import Dict

from pydantic import BaseModel
from sqlmodel import Session

from app.agent.base import Skill
from app.services.importer import import_json


class ImportJsonInput(BaseModel):
    content: str
    mode: str = "overwrite"


class ImportJsonOutput(BaseModel):
    status: str


class ImportJsonSkill(Skill):
    name = "import.json"
    description = "Import JSON data into database."
    input_schema = ImportJsonInput
    output_schema = ImportJsonOutput

    def run(self, data: ImportJsonInput, context: dict) -> ImportJsonOutput:
        session: Session = context["session"]
        payload: Dict[str, object] = json.loads(data.content)
        if not isinstance(payload, dict):
            raise ValueError("Invalid JSON payload")
        import_json(session, payload, data.mode)
        return ImportJsonOutput(status="ok")


def get_skill() -> Skill:
    return ImportJsonSkill()
