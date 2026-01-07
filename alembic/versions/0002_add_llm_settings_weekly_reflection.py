from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_add_llm_settings_weekly_reflection"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("suggestion") as batch_op:
        batch_op.alter_column("habit_id", existing_type=sa.Integer(), nullable=True)

    with op.batch_alter_table("settings") as batch_op:
        batch_op.add_column(
            sa.Column("llm_api_key", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("llm_model", sa.String(), nullable=False, server_default="deepseek")
        )


def downgrade() -> None:
    with op.batch_alter_table("settings") as batch_op:
        batch_op.drop_column("llm_model")
        batch_op.drop_column("llm_api_key")

    with op.batch_alter_table("suggestion") as batch_op:
        batch_op.alter_column("habit_id", existing_type=sa.Integer(), nullable=False)
