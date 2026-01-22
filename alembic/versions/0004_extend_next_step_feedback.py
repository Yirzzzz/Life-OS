"""extend next_step_feedback

Revision ID: 0004_extend_next_step_feedback
Revises: 0003_add_next_step_feedback
Create Date: 2026-01-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0004_extend_next_step_feedback"
down_revision = "0003_add_next_step_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "next_step_feedback", sa.Column("snooze_until", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "next_step_feedback", sa.Column("user_due_date", sa.Date(), nullable=True)
    )
    op.add_column(
        "next_step_feedback",
        sa.Column("created_short_term_objective_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "next_step_feedback",
        sa.Column("created_plan_item_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "next_step_feedback",
        sa.Column("completion_note", sa.Text(), nullable=False, server_default=""),
    )
    op.create_foreign_key(
        "fk_next_step_feedback_objective",
        "next_step_feedback",
        "short_term_objective",
        ["created_short_term_objective_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_next_step_feedback_plan_item",
        "next_step_feedback",
        "planitem",
        ["created_plan_item_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_next_step_feedback_plan_item", "next_step_feedback", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_next_step_feedback_objective", "next_step_feedback", type_="foreignkey"
    )
    op.drop_column("next_step_feedback", "completion_note")
    op.drop_column("next_step_feedback", "created_plan_item_id")
    op.drop_column("next_step_feedback", "created_short_term_objective_id")
    op.drop_column("next_step_feedback", "user_due_date")
    op.drop_column("next_step_feedback", "snooze_until")
