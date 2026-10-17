import os
from datetime import datetime,timezone,timedelta
from uuid import uuid4

import pytest
import psycopg
from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from psycopg.types.json import Jsonb

from accord.db import DB,canonical,digest,uid
from accord.contract import policy as make_policy
from accord.config import Settings,settings


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch,db):
    """A developer's real provider credentials/configuration must never enter tests."""
    monkeypatch.setitem(Settings.model_config,"env_file",None)
    for name in Settings.model_fields:
        if name!="database_url":
            monkeypatch.delenv(name.upper(),raising=False)
    settings.cache_clear()
    yield
    settings.cache_clear()


class Clock:
    def __init__(self):
        # Keep PostgreSQL DEFAULT now() work timestamps behind the controlled test clock.
        self.value = datetime.now(timezone.utc)+timedelta(minutes=1)
    def __call__(self):
        return self.value
    def advance(self,seconds=120):
        self.value += timedelta(seconds=seconds)


@pytest.fixture(scope="session")
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required: tests must exercise real PostgreSQL")
    if "accord_test" not in psycopg.conninfo.conninfo_to_dict(url).get("dbname",""):
        pytest.fail("Refusing destructive tests unless database name contains accord_test")
    with psycopg.connect(url,autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    os.environ["DATABASE_URL"] = url
    command.upgrade(Config("alembic.ini"),"head")
    return DB(url)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def deal(db):
    buyer,supplier,identity = uid(),uid(),uid()
    unique = str(uuid4())[:8]
    passwords = PasswordHasher()
    policy = make_policy()
    with db.tx() as conn:
        conn.execute("TRUNCATE users CASCADE")
        conn.execute("TRUNCATE webhook_inbox,login_limits")
        for user_id,role in ((buyer,"buyer"),(supplier,"supplier")):
            conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,%s,%s,%s)",
                         (user_id,role+unique,passwords.hash("test-password-only"),role))
        conn.execute("""INSERT INTO deals(id,buyer_id,supplier_id,merchant_id,payer_id,policy,policy_hash,
            buyer_approved,supplier_approved,delivery_ack) VALUES(%s,%s,%s,'MERCHANT1','BUYER1',%s,%s,true,true,true)""",
            (identity,buyer,supplier,Jsonb(policy),digest(canonical(policy))))
        row = conn.execute("SELECT * FROM deals WHERE id=%s",(identity,)).fetchone()
    row["buyer_username"],row["supplier_username"] = "buyer"+unique,"supplier"+unique
    yield row
    # No cross-test jobs, including intentionally unresolved operations.
    with db.tx() as conn:
        conn.execute("UPDATE deals SET active_operation=NULL,lease_until=NULL WHERE id=%s",(identity,))
        conn.execute("UPDATE operations SET state='MANUAL_REVIEW' WHERE deal_id=%s",(identity,))


def passing_decision(db,deal_id):
    with db.tx() as conn:
        d = conn.execute("SELECT * FROM deals WHERE id=%s",(deal_id,)).fetchone()
        identity = uid()
        conn.execute("""INSERT INTO decisions(id,deal_id,evidence_generation,policy_hash,outcome,bundle)
            VALUES(%s,%s,%s,%s,'PASS',%s)""",(identity,deal_id,d["evidence_generation"],d["policy_hash"],Jsonb({"test_fixture":True})))
    return identity
