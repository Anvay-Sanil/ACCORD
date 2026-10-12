import secrets

import psycopg
import pytest
from psycopg.types.json import Jsonb

from accord.config import Settings
from accord.coordinator import Coordinator
from accord.db import DB, uid
from accord.extraction_worker import once
from accord.paypal import ProviderError
from accord import provision_db
from tests.test_operations import setup_authorized, get, payment, run
from tests.test_evidence import specimen
from tests.conftest import passing_decision
from accord.evidence import review_interpretation, evaluate
from accord.coordinator import Conflict
from accord.worker import Worker
from tests.fake_paypal import FakePayPal


def test_confirmed_capture_survives_failed_late_event_refresh(db,deal,clock,monkeypatch):
    c,w,p=setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"]); run(w.run_once())
    event={"id":uid(),"event_type":"PAYMENT.CAPTURE.PENDING","resource":p.capture}
    with db.tx() as conn:
        conn.execute("INSERT INTO webhook_inbox(event_id,raw_body,event) VALUES(%s,%s,%s)",
                     (event["id"],b"fixture",Jsonb(event)))
    async def unavailable(path):
        raise ProviderError()
    monkeypatch.setattr(p,"get",unavailable)
    w.wake_webhooks(); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"]=="CAPTURED"
    assert get(db,deal["id"],"CAPTURE")["state"]=="RECONCILING"


def test_preflight_failures_increase_backoff_without_post(db,deal,clock,monkeypatch):
    c,w,p=setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"])
    counts=[]
    monkeypatch.setattr("accord.worker.backoff",lambda n,retry_after: counts.append(n) or 1)
    async def unavailable(path):
        raise ProviderError(429,"RATE_LIMIT",retry_after=1)
    monkeypatch.setattr(p,"get",unavailable)
    for _ in range(3):
        run(w.run_once()); clock.advance()
    assert counts==[1,2,3]
    assert get(db,deal["id"],"CAPTURE")["attempts"]==0
    assert get(db,deal["id"],"CAPTURE")["failures"]==3


def test_runtime_database_role_cannot_destroy_or_rewrite_audit(db,deal,monkeypatch):
    password=secrets.token_urlsafe(24)
    monkeypatch.setenv("APP_DB_PASSWORD",password)
    provision_db.main(role="accord_test_runtime")
    import os
    runtime=DB(psycopg.conninfo.make_conninfo(os.environ["TEST_DATABASE_URL"],user="accord_test_runtime",password=password))
    # Exercise actual coordinator writes with the restricted credential.
    op=Coordinator(runtime).queue_create(deal["id"])
    assert op["kind"]=="CREATE"
    for statement in ("TRUNCATE audit", "UPDATE audit SET event='tampered'",
                      "DELETE FROM decisions", "CREATE TABLE intruder(id int)",
                      "UPDATE users SET role='buyer'", "DELETE FROM extraction_runs"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with runtime.tx() as conn:
                conn.execute(statement)


def test_failed_extraction_has_immutable_versioned_history(db,deal,monkeypatch):
    doc_id=Coordinator(db).add_document(deal["id"],deal["supplier_id"],"supplier","certificate",b"%PDF-test")
    def broken(body):
        raise ValueError("bad PDF")
    monkeypatch.setattr("accord.extraction_worker.parse_pdf",broken)
    config=Settings(database_url="postgresql://unused",app_origin="https://testserver")
    assert run(once(db,config))
    with db.tx() as conn:
        record=conn.execute("SELECT * FROM extraction_runs WHERE document_id=%s",(doc_id,)).fetchone()
        assert record["state"]=="REVIEW_REQUIRED"
        versions=record["extraction"]["versions"]
        assert all(versions[x] for x in ("parser","schema","prompt","prompt_sha256","model"))
    with pytest.raises(psycopg.errors.RaiseException):
        with db.tx() as conn:
            conn.execute("UPDATE extraction_runs SET state='READY' WHERE document_id=%s",(doc_id,))


def test_new_buyer_interpretation_blocks_old_passing_decision(db,deal,clock):
    c,w,p=setup_authorized(db,deal,clock)
    identity=c.add_document(deal["id"],deal["supplier_id"],"supplier","certificate",b"%PDF-fixture")
    doc,finding=specimen("PO: PO-001\nLot: LOT-001\nRequired grade: 316L\nDelivered grade: 304",value="304")
    with db.tx() as conn:
        version=conn.execute("SELECT version FROM documents WHERE id=%s",(identity,)).fetchone()["version"]
        conn.execute("UPDATE documents SET parsed=%s WHERE id=%s",(Jsonb(doc["parsed"]),identity))
    passing_decision(db,deal["id"])
    capture=c.queue_capture(deal["id"])
    finding=finding.model_copy(update={"document_id":identity,"document_version":version})
    review_interpretation(db,deal["id"],deal["buyer_id"],finding,"The delivered grade is explicitly 304.")
    with pytest.raises(Conflict,match="has not passed"):
        c.queue_capture(deal["id"])
    assert get(db,deal["id"],"CAPTURE")["id"]==capture["id"]
    assert get(db,deal["id"],"CAPTURE")["state"]=="BLOCKED"
    assert evaluate(db,deal["id"])["outcome"]=="REVIEW_REQUIRED"


def test_authorize_waits_for_approval_instead_of_permanent_manual_state(db,deal,clock):
    c=Coordinator(db,clock)
    p=FakePayPal(deal,clock)
    w=Worker(db,p,clock)
    c.queue_create(deal["id"]); run(w.run_once())
    p.order_value["status"]="CREATED"
    p.order_value.pop("payer")
    c.queue_authorize(deal["id"]); run(w.run_once())
    assert get(db,deal["id"],"AUTHORIZE")["state"]=="QUEUED"
    p.order_value.update(status="APPROVED",payer={"payer_id":"BUYER1"})
    clock.advance(); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"]=="AUTHORIZED"


def test_uncertain_capture_retry_budget_expires_without_new_key(db,deal,clock):
    c,w,p=setup_authorized(db,deal,clock)
    original=c.queue_capture(deal["id"])
    claim=w.claim(); w.prepare_dispatch(claim)
    # Dispatch intent was persisted, but the remote outcome is unknown.
    w.defer(claim,"RECONCILING","uncertain",1)
    clock.advance(360); run(w.run_once())
    current=get(db,deal["id"],"CAPTURE")
    assert current["state"]=="RECONCILING"
    assert current["request_id"]==original["request_id"]
    assert not any(x[0]=="POST" and x[1].endswith("/capture") for x in p.calls)
