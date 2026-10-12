import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from psycopg.types.json import Jsonb

from accord.coordinator import Coordinator,Conflict
from accord.db import uid
from accord.evidence import Finding,evaluate
from accord.worker import Worker
from tests.test_evidence import specimen
from tests.test_operations import setup_authorized,get,payment,run


def test_two_workers_only_one_claim_and_crash_recovery(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"])
    second = Worker(db,p,clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker:worker.claim(),[w,second]))
    assert len([x for x in claims if x])==1
    claim = next(x for x in claims if x)
    assert w.prepare_dispatch(claim)
    # Provider completes, then process dies without persisting the response.
    run(p.post(claim.op["endpoint"],claim.op["payload"],str(claim.op["request_id"])))
    clock.advance()
    run(second.run_once())
    assert payment(db,deal["id"])["payment_state"]=="CAPTURED"


def test_capture_and_void_queued_together_never_both_dispatch(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    def capture():
        try:
            c.queue_capture(deal["id"])
        except Conflict:
            pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(capture)
        b=pool.submit(c.request_void,deal["id"])
        a.result(); b.result()
    for _ in range(3):
        run(w.run_once())
    posts=[x[1] for x in p.calls if x[0]=="POST"]
    assert not (any(x.endswith("/capture") for x in posts) and any(x.endswith("/void") for x in posts))
    assert payment(db,deal["id"])["payment_state"]=="VOIDED"


def test_void_uncertain_does_not_blindly_retry(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    c.request_void(deal["id"])
    claim=w.claim(); w.prepare_dispatch(claim)
    # Lost request before provider received it is indistinguishable from an in-flight request.
    w.defer(claim,"RECONCILING","simulated uncertain void",1)
    clock.advance(); run(w.run_once())
    assert not any(x[0]=="POST" and x[1].endswith("/void") for x in p.calls)
    assert get(db,deal["id"],"VOID")["state"]=="RECONCILING"


def test_pending_capture_can_settle_after_48_hours_without_new_post(db,deal,clock):
    c,w,p=setup_authorized(db,deal,clock)
    p.capture_status="PENDING"
    c.queue_capture(deal["id"]); run(w.run_once())
    clock.advance(49*3600)
    p.capture["status"]="COMPLETED"
    run(w.run_once())
    assert payment(db,deal["id"])["payment_state"]=="CAPTURED"
    assert len([x for x in p.calls if x[0]=="POST" and x[1].endswith("/capture")])==1


def test_deadline_scheduler_voids_only_never_dispatched_capture(db,deal,clock):
    c,w,p=setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"])
    clock.advance(49*3600)
    w.expire_fulfillment(); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"]=="VOIDED"
    assert not any(x[0]=="POST" and x[1].endswith("/capture") for x in p.calls)


def test_delayed_pending_event_reconciles_canonical_completed_capture(db,deal,clock):
    c,w,p=setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"]); run(w.run_once())
    event={"id":uid(),"event_type":"PAYMENT.CAPTURE.PENDING","resource":{**p.capture,"status":"PENDING"}}
    with db.tx() as conn:
        conn.execute("INSERT INTO webhook_inbox(event_id,raw_body,event) VALUES(%s,%s,%s)",(event["id"],b"fixture",Jsonb(event)))
    w.wake_webhooks(); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"]=="CAPTURED"


@pytest.mark.parametrize("grade",["316L","304"])
def test_evidence_decision_controls_capture_eligibility(db,deal,clock,grade):
    c,w,p=setup_authorized(db,deal,clock)
    for kind,role,text,value,field in [
        ("certificate","supplier",f"PO: PO-001\nLot: LOT-001\nDelivered grade: {grade}",grade,"delivered_grade"),
        ("receipt","buyer","PO: PO-001\nLot: LOT-001\nReceived: YES","YES","delivery_received")]:
        identity=c.add_document(deal["id"],deal[f"{role}_id"],role,kind,b"%PDF-test-fixture")
        doc,finding=specimen(text,value,field)
        with db.tx() as conn:
            version=conn.execute("SELECT version FROM documents WHERE id=%s",(identity,)).fetchone()["version"]
            finding=finding.model_copy(update={"document_id":identity,"document_version":version})
            extraction={"proposal":{"status":"found","findings":[finding.model_dump()]},
                        "versions":{"parser":"fixture-parser","schema":"finding-v1","prompt":"extract-v1","model":"fixture-model"}}
            conn.execute("UPDATE documents SET parsed=%s,extraction=%s,parse_state='READY' WHERE id=%s",
                         (Jsonb(doc["parsed"]),Jsonb(extraction),identity))
    decision=evaluate(db,deal["id"])
    if grade=="316L":
        assert decision["outcome"]=="PASS"
        c.queue_capture(deal["id"]); run(w.run_once())
        assert payment(db,deal["id"])["payment_state"]=="CAPTURED"
    else:
        assert decision["outcome"]=="REVIEW_REQUIRED"
        with pytest.raises(Conflict):
            c.queue_capture(deal["id"])
        assert not any(x[0]=="POST" and x[1].endswith("/capture") for x in p.calls)

