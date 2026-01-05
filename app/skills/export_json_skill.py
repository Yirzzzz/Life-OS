from __future__ import annotations

from pydantic import BaseModel
from sqlmodel import Session

from app.agent.base import Skill
from app.services.exporter import export_json_text


class ExportJsonInput(BaseModel):
    pass


class ExportJsonOutput(BaseModel):
    content: str


class ExportJsonSkill(Skill):
    name = "export.json"
    description = "Export full database as JSON."
    input_schema = ExportJsonInput
    output_schema = ExportJsonOutput

    def run(self, data: ExportJsonInput, context: dict) -> ExportJsonOutput:
        session: Session = context["session"]
        return ExportJsonOutput(content=export_json_text(session))


def get_skill() -> Skill:
    return ExportJsonSkill()
