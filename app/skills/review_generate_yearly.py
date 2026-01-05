from __future__ import annotations

from pydantic import BaseModel
from sqlmodel import Session

from app.agent.base import Skill
from app.services.review import generate_yearly_review


class ReviewYearlyInput(BaseModel):
    year: int


class ReviewYearlyOutput(BaseModel):
    review: dict


class ReviewGenerateYearlySkill(Skill):
    name = "review.generate_yearly"
    description = "Generate yearly review."
    input_schema = ReviewYearlyInput
    output_schema = ReviewYearlyOutput

    def run(self, data: ReviewYearlyInput, context: dict) -> ReviewYearlyOutput:
        session: Session = context["session"]
        review = generate_yearly_review(session, data.year)
        return ReviewYearlyOutput(review=review)


def get_skill() -> Skill:
    return ReviewGenerateYearlySkill()
