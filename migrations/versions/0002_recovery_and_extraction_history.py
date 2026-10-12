"""Count read failures independently and preserve extraction history."""
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.execute("""
      ALTER TABLE operations ADD COLUMN failures integer NOT NULL DEFAULT 0;
      CREATE TABLE extraction_runs (
        id uuid PRIMARY KEY, document_id uuid NOT NULL REFERENCES documents,
        parse_generation bigint NOT NULL, state text NOT NULL,
        extraction jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(document_id,parse_generation)
      );
      CREATE TRIGGER extraction_append_only BEFORE UPDATE OR DELETE ON extraction_runs
        FOR EACH ROW EXECUTE FUNCTION forbid_change();
    """)


def downgrade():
    raise RuntimeError("Restore a database backup; destructive rollback is disabled")
