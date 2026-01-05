from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel
from sqlmodel import Session

from app.agent.base import Skill
from app.services.exporter import export_daily_markdown, export_monthly_markdown, export_yearly_markdown


class ExportMarkdownInput(BaseModel):
    scope: str
    date: Optional[date] = None
    year: Optional[int] = None
    month: Optional[int] = None


class ExportMarkdownOutput(BaseModel):
    content: str


class ExportMarkdownSkill(Skill):
    name = "export.markdown"
    description = "Export markdown for day, month, or year."
    input_schema = ExportMarkdownInput
    output_schema = ExportMarkdownOutput

    def run(self, data: ExportMarkdownInput, context: dict) -> ExportMarkdownOutput:
        session: Session = context["session"]
        if data.scope == "daily" and data.date:
            content = export_daily_markdown(session, data.date)
        elif data.scope == "monthly" and data.year and data.month:
            content = export_monthly_markdown(session, data.year, data.month)
        elif data.scope == "yearly" and data.year:
            content = export_yearly_markdown(session, data.year)
        else:
            raise ValueError("Invalid scope or missing parameters")
        return ExportMarkdownOutput(content=content)


def get_skill() -> Skill:
    return ExportMarkdownSkill()
