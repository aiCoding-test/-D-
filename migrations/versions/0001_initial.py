"""Initial versioned P0 schema.

Revision ID: 0001
"""
from alembic import op
from pathlib import Path

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Frozen revision; future model edits must not silently rewrite migration 0001.
    sql = Path(__file__).with_name("0001.sql").read_text(encoding="utf-8")
    for statement in sql.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    for name in ["command_receipts", "task_events", "cache_entries", "steps", "artifacts", "attempts", "resource_slots", "worker_sessions", "tasks"]:
        op.execute(f'DROP TABLE "{name}" CASCADE')
