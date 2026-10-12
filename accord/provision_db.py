"""Run once with schema-owner credentials. Do not give this credential to web/workers."""
import os

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql


def main(role="accord_app"):
    command.upgrade(Config("alembic.ini"),"head")
    password = os.environ["APP_DB_PASSWORD"]
    if len(password)<16:
        raise SystemExit("Use an application database password of at least 16 characters")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        identifier=sql.Identifier(role)
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s",(role,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(identifier,sql.Literal(password)))
        else:
            conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(identifier,sql.Literal(password)))
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        for statement in (
            "GRANT USAGE ON SCHEMA public TO {}",
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}",
            "GRANT INSERT,UPDATE ON deals,documents,operations,dispatches,webhook_inbox,login_limits TO {}",
            "GRANT INSERT ON audit,decisions,reviews,extraction_runs TO {}",
            "GRANT INSERT,DELETE ON sessions TO {}",
            "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}",
        ):
            conn.execute(sql.SQL(statement).format(identifier))
    print("Migrated and granted a non-owner runtime role. Seed users using operator credentials.")


if __name__ == "__main__":
    main()
