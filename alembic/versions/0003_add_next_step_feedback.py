"""add next_step_feedback

Revision ID: 0003_add_next_step_feedback
Revises: 0002_add_llm_settings_weekly_reflection
Create Date: 2026-01-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0003_add_next_step_feedback"
down_revision = "0002_add_llm_settings_weekly_reflection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "next_step_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("goal_id", sa.Integer(), sa.ForeignKey("goal.id"), nullable=False),
        sa.Column("suggestion_id", sa.Integer(), sa.ForeignKey("suggestion.id"), nullable=False),
        sa.Column("step_key", sa.String(), nullable=False),
        sa.Column("step_text_snapshot", sa.Text(), nullable=False, server_default=""),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("reason_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_next_step_feedback_goal_step_created",
        "next_step_feedback",
        ["goal_id", "step_key", "created_at"],
    )
    with op.batch_alter_table("next_step_feedback") as batch_op:
        batch_op.create_unique_constraint(
            "uq_next_step_feedback_dedupe",
            ["goal_id", "step_key", "action", "reason", "suggestion_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("next_step_feedback") as batch_op:
        batch_op.drop_constraint("uq_next_step_feedback_dedupe", type_="unique")
    op.drop_index("ix_next_step_feedback_goal_step_created", table_name="next_step_feedback")
    op.drop_table("next_step_feedback")
