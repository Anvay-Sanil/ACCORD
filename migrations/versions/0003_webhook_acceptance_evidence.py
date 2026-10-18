"""Retain the verification input required to recheck real-transaction signatures."""
from alembic import op
revision="0003"
down_revision="0002"


def upgrade():
    op.execute("""ALTER TABLE webhook_inbox ADD COLUMN verification_headers jsonb;
      ALTER TABLE webhook_inbox ADD COLUMN verification_method text NOT NULL DEFAULT 'unrecorded';""")


def downgrade():
    raise RuntimeError("Restore a database backup; destructive rollback is disabled")
