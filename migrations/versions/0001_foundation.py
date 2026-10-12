"""Durable operations, deal serialization, evidence lineage and ingress."""
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("""
    CREATE TABLE users (
      id uuid PRIMARY KEY, username text UNIQUE NOT NULL,
      password_hash text NOT NULL, role text NOT NULL CHECK(role IN ('buyer','supplier'))
    );
    CREATE TABLE sessions (
      token_hash text PRIMARY KEY, csrf_hash text NOT NULL,
      user_id uuid NOT NULL REFERENCES users, expires_at timestamptz NOT NULL
    );
    CREATE TABLE login_limits (
      bucket text PRIMARY KEY, attempts integer NOT NULL, resets_at timestamptz NOT NULL
    );
    CREATE TABLE deals (
      id uuid PRIMARY KEY, buyer_id uuid NOT NULL REFERENCES users,
      supplier_id uuid NOT NULL REFERENCES users, merchant_id text NOT NULL,
      payer_id text NOT NULL, policy jsonb NOT NULL, policy_hash text NOT NULL,
      buyer_approved boolean NOT NULL DEFAULT false,
      supplier_approved boolean NOT NULL DEFAULT false,
      delivery_ack boolean NOT NULL DEFAULT false,
      evidence_generation integer NOT NULL DEFAULT 0,
      payment_state text NOT NULL DEFAULT 'AWAITING_BUYER',
      cancel_requested boolean NOT NULL DEFAULT false,
      order_id text UNIQUE, authorization_id text UNIQUE, capture_id text UNIQUE,
      authorized_at timestamptz, capture_deadline timestamptz,
      lease_generation bigint NOT NULL DEFAULT 0,
      lease_until timestamptz, active_operation uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CHECK ((policy->>'amount') = '750.00' AND (policy->>'currency') = 'USD')
    );
    CREATE TABLE documents (
      id uuid PRIMARY KEY, deal_id uuid NOT NULL REFERENCES deals,
      version integer NOT NULL, kind text NOT NULL CHECK(kind IN ('certificate','receipt')),
      uploader_id uuid NOT NULL REFERENCES users, sha256 text NOT NULL,
      body bytea NOT NULL CHECK(octet_length(body) <= 5242880),
      active boolean NOT NULL DEFAULT true,
      parse_state text NOT NULL DEFAULT 'QUEUED', parsed jsonb, extraction jsonb,
      parse_generation bigint NOT NULL DEFAULT 0, parse_lease_until timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(deal_id,version)
    );
    CREATE TABLE reviews (
      id uuid PRIMARY KEY, deal_id uuid NOT NULL REFERENCES deals,
      document_id uuid NOT NULL REFERENCES documents,
      evidence_generation integer NOT NULL, reviewer_id uuid NOT NULL REFERENCES users,
      interpretation jsonb NOT NULL, reason text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE decisions (
      id uuid PRIMARY KEY, deal_id uuid NOT NULL REFERENCES deals,
      evidence_generation integer NOT NULL, policy_hash text NOT NULL,
      outcome text NOT NULL CHECK(outcome IN ('PASS','REVIEW_REQUIRED')),
      bundle jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE operations (
      id uuid PRIMARY KEY, deal_id uuid NOT NULL REFERENCES deals,
      kind text NOT NULL CHECK(kind IN ('CREATE','AUTHORIZE','CAPTURE','VOID')),
      state text NOT NULL DEFAULT 'QUEUED', endpoint text NOT NULL,
      payload text NOT NULL, payload_hash text NOT NULL,
      request_id uuid NOT NULL UNIQUE, provider_id text,
      decision_id uuid REFERENCES decisions,
      first_dispatch_at timestamptz, retry_deadline timestamptz,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      attempts integer NOT NULL DEFAULT 0, last_error text,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(deal_id,kind)
    );
    ALTER TABLE deals ADD CONSTRAINT active_operation_fk
      FOREIGN KEY(active_operation) REFERENCES operations(id);
    CREATE INDEX work_due ON operations(next_attempt_at)
      WHERE state IN ('QUEUED','IN_FLIGHT','RECONCILING','PENDING');
    CREATE TABLE dispatches (
      id uuid PRIMARY KEY, operation_id uuid NOT NULL REFERENCES operations,
      lease_generation bigint NOT NULL, request_id uuid NOT NULL,
      payload_hash text NOT NULL, started_at timestamptz NOT NULL,
      finished_at timestamptz, outcome text, provider_status integer
    );
    CREATE TABLE webhook_inbox (
      event_id text PRIMARY KEY, raw_body bytea NOT NULL,
      event jsonb NOT NULL, verified_at timestamptz NOT NULL DEFAULT now(),
      processed_at timestamptz, next_attempt_at timestamptz NOT NULL DEFAULT now(),
      attempts integer NOT NULL DEFAULT 0
    );
    CREATE TABLE audit (
      id uuid PRIMARY KEY, deal_id uuid REFERENCES deals, event text NOT NULL,
      data jsonb NOT NULL, actor_id uuid REFERENCES users,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'append-only record'; END $$;
    CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit
      FOR EACH ROW EXECUTE FUNCTION forbid_change();
    CREATE TRIGGER decision_append_only BEFORE UPDATE OR DELETE ON decisions
      FOR EACH ROW EXECUTE FUNCTION forbid_change();
    CREATE TRIGGER review_append_only BEFORE UPDATE OR DELETE ON reviews
      FOR EACH ROW EXECUTE FUNCTION forbid_change();
    CREATE FUNCTION freeze_operation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.kind <> OLD.kind OR NEW.deal_id <> OLD.deal_id OR
         NEW.endpoint <> OLD.endpoint OR NEW.payload <> OLD.payload OR
         NEW.payload_hash <> OLD.payload_hash OR NEW.request_id <> OLD.request_id THEN
        RAISE EXCEPTION 'operation request is frozen';
      END IF;
      IF OLD.first_dispatch_at IS NOT NULL AND
        (NEW.first_dispatch_at IS DISTINCT FROM OLD.first_dispatch_at OR
         NEW.retry_deadline IS DISTINCT FROM OLD.retry_deadline OR
         NEW.decision_id IS DISTINCT FROM OLD.decision_id) THEN
        RAISE EXCEPTION 'dispatched decision and retry window are frozen';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER operation_immutable BEFORE UPDATE ON operations
      FOR EACH ROW EXECUTE FUNCTION freeze_operation();
    CREATE FUNCTION freeze_deal() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.policy <> OLD.policy OR NEW.policy_hash <> OLD.policy_hash OR
         NEW.buyer_id <> OLD.buyer_id OR NEW.supplier_id <> OLD.supplier_id OR
         NEW.merchant_id <> OLD.merchant_id OR NEW.payer_id <> OLD.payer_id THEN
         RAISE EXCEPTION 'deal contract is frozen';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER deal_immutable BEFORE UPDATE ON deals
      FOR EACH ROW EXECUTE FUNCTION freeze_deal();
    CREATE FUNCTION freeze_document() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.body <> OLD.body OR NEW.sha256 <> OLD.sha256 OR NEW.version <> OLD.version OR
         NEW.deal_id <> OLD.deal_id OR NEW.uploader_id <> OLD.uploader_id OR NEW.kind <> OLD.kind THEN
        RAISE EXCEPTION 'original document is immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER document_immutable BEFORE UPDATE ON documents
      FOR EACH ROW EXECUTE FUNCTION freeze_document();
    """)


def downgrade():
    raise RuntimeError("Destructive rollback intentionally unsupported; restore a database backup")
