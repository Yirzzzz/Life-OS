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
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.create_table(
            "_next_step_feedback_new",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("goal_id", sa.Integer(), nullable=False),
            sa.Column("suggestion_id", sa.Integer(), nullable=False),
            sa.Column("step_key", sa.String(), nullable=False),
            sa.Column("step_text_snapshot", sa.Text(), nullable=False, server_default=""),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("reason", sa.String(), nullable=False),
            sa.Column("reason_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("snooze_until", sa.DateTime(), nullable=True),
            sa.Column("user_due_date", sa.Date(), nullable=True),
            sa.Column(
                "created_short_term_objective_id", sa.Integer(), nullable=True
            ),
            sa.Column("created_plan_item_id", sa.Integer(), nullable=True),
            sa.Column("completion_note", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["goal_id"], ["goal.id"]),
            sa.ForeignKeyConstraint(["suggestion_id"], ["suggestion.id"]),
            sa.ForeignKeyConstraint(
                ["created_short_term_objective_id"], ["short_term_objective.id"]
            ),
            sa.ForeignKeyConstraint(
                ["created_plan_item_id"], ["planitem.id"]
            ),
            sa.UniqueConstraint(
                "goal_id",
                "step_key",
                "action",
                "reason",
                "suggestion_id",
                name="uq_next_step_feedback_dedupe",
            ),
        )
        op.execute(
            """
            INSERT INTO _next_step_feedback_new (
                id,
                goal_id,
                suggestion_id,
                step_key,
                step_text_snapshot,
                action,
                reason,
                reason_detail,
                snooze_until,
                user_due_date,
                created_short_term_objective_id,
                created_plan_item_id,
                completion_note,
                created_at
            )
            SELECT
                id,
                goal_id,
                suggestion_id,
                step_key,
                step_text_snapshot,
                action,
                reason,
                reason_detail,
                NULL,
                NULL,
                NULL,
                NULL,
                '',
                created_at
            FROM next_step_feedback
            """
        )
        op.drop_table("next_step_feedback")
        op.rename_table("_next_step_feedback_new", "next_step_feedback")
        op.create_index(
            "ix_next_step_feedback_goal_step_created",
            "next_step_feedback",
            ["goal_id", "step_key", "created_at"],
        )
    else:
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
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.create_table(
            "_next_step_feedback_old",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("goal_id", sa.Integer(), nullable=False),
            sa.Column("suggestion_id", sa.Integer(), nullable=False),
            sa.Column("step_key", sa.String(), nullable=False),
            sa.Column("step_text_snapshot", sa.Text(), nullable=False, server_default=""),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("reason", sa.String(), nullable=False),
            sa.Column("reason_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["goal_id"], ["goal.id"]),
            sa.ForeignKeyConstraint(["suggestion_id"], ["suggestion.id"]),
            sa.UniqueConstraint(
                "goal_id",
                "step_key",
                "action",
                "reason",
                "suggestion_id",
                name="uq_next_step_feedback_dedupe",
            ),
        )
        op.execute(
            """
            INSERT INTO _next_step_feedback_old (
                id,
                goal_id,
                suggestion_id,
                step_key,
                step_text_snapshot,
                action,
                reason,
                reason_detail,
                created_at
            )
            SELECT
                id,
                goal_id,
                suggestion_id,
                step_key,
                step_text_snapshot,
                action,
                reason,
                reason_detail,
                created_at
            FROM next_step_feedback
            """
        )
        op.drop_table("next_step_feedback")
        op.rename_table("_next_step_feedback_old", "next_step_feedback")
        op.create_index(
            "ix_next_step_feedback_goal_step_created",
            "next_step_feedback",
            ["goal_id", "step_key", "created_at"],
        )
    else:
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
