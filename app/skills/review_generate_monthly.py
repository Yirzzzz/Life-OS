from __future__ import annotations

from pydantic import BaseModel
from sqlmodel import Session

from app.agent.base import Skill
from app.services.review import generate_monthly_review


class ReviewMonthlyInput(BaseModel):
    year: int
    month: int


class ReviewMonthlyOutput(BaseModel):
    review: dict


class ReviewGenerateMonthlySkill(Skill):
    name = "review.generate_monthly"
    description = "Generate monthly review."
    input_schema = ReviewMonthlyInput
    output_schema = ReviewMonthlyOutput

    def run(self, data: ReviewMonthlyInput, context: dict) -> ReviewMonthlyOutput:
        session: Session = context["session"]
        review = generate_monthly_review(session, data.year, data.month)
        return ReviewMonthlyOutput(review=review)


def get_skill() -> Skill:
    return ReviewGenerateMonthlySkill()
