"""Optional physical GPU identity and immutable attempt execution configuration."""
from alembic import op
revision='0003'
down_revision='0002'
branch_labels=None
depends_on=None

def upgrade():
    op.execute("ALTER TABLE resource_slots ALTER COLUMN key TYPE VARCHAR(80)")
    op.execute("ALTER TABLE resource_slots ADD COLUMN device JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE attempts ADD COLUMN execution JSONB")

def downgrade():
    op.execute("ALTER TABLE attempts DROP COLUMN execution")
    op.execute("ALTER TABLE resource_slots DROP COLUMN device")
    # Keep the wider key to avoid truncating real UUID history.
