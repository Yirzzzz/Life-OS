from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goal",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("description_md", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "habit",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("frequency", sa.String(), nullable=False),
        sa.Column("preferred_period", sa.String(), nullable=False),
        sa.Column("target_per_week", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "dailyplan",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "short_term_objective",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("linked_goal_id", sa.Integer(), sa.ForeignKey("goal.id"), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "milestone",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("goal_id", sa.Integer(), sa.ForeignKey("goal.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_table(
        "planitem",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("daily_plan_id", sa.Integer(), sa.ForeignKey("dailyplan.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("linked_goal_id", sa.Integer(), sa.ForeignKey("goal.id"), nullable=True),
        sa.Column("linked_habit_id", sa.Integer(), sa.ForeignKey("habit.id"), nullable=True),
        sa.Column(
            "linked_objective_id",
            sa.Integer(),
            sa.ForeignKey("short_term_objective.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.String(), nullable=False),
    )
    op.create_table(
        "planitemsuppression",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("linked_habit_id", sa.Integer(), sa.ForeignKey("habit.id"), nullable=True),
        sa.Column(
            "linked_objective_id",
            sa.Integer(),
            sa.ForeignKey("short_term_objective.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "daylog",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("period_entries", sa.JSON(), nullable=False),
        sa.Column("journal_md", sa.Text(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
    )
    op.create_table(
        "suggestion",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("habit_id", sa.Integer(), sa.ForeignKey("habit.id"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
    )
    op.create_table(
        "suggestiondecision",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "suggestion_id",
            sa.Integer(),
            sa.ForeignKey("suggestion.id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "agentrunlog",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("skill_name", sa.String(), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("periods_json", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("agentrunlog")
    op.drop_table("suggestiondecision")
    op.drop_table("suggestion")
    op.drop_table("daylog")
    op.drop_table("planitemsuppression")
    op.drop_table("planitem")
    op.drop_table("milestone")
    op.drop_table("short_term_objective")
    op.drop_table("dailyplan")
    op.drop_table("habit")
    op.drop_table("goal")
