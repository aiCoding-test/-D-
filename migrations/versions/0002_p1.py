"""Checkpoint lineage and bounded resource telemetry history."""
from pathlib import Path
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    for statement in Path(__file__).with_name("0002.sql").read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    op.drop_table("resource_samples")
    op.drop_table("checkpoint_uses")
    op.drop_table("checkpoints")
    op.execute("ALTER TABLE attempts DROP COLUMN IF EXISTS queue_seconds")
