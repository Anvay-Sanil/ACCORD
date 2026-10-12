import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from accord.coordinator import Coordinator,Conflict,lock_deal,operation
from accord.db import uid
from accord.worker import Worker
from tests.conftest import passing_decision
from tests.fake_paypal import FakePayPal


def run(coro):
    return asyncio.run(coro)


def setup_authorized(db,deal,clock):
    provider = FakePayPal(deal,clock)
    coordinator,worker = Coordinator(db,clock),Worker(db,provider,clock)
    coordinator.queue_create(deal["id"])
    run(worker.run_once())
    coordinator.queue_authorize(deal["id"])
    run(worker.run_once())
    passing_decision(db,deal["id"])
    return coordinator,worker,provider


def get(db,deal_id,kind):
    with db.tx() as conn:
        return operation(conn,deal_id,kind)


def payment(db,deal_id):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM deals WHERE id=%s",(deal_id,)).fetchone()


def test_repeated_and_ten_concurrent_submissions(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(lambda _:str(c.queue_capture(deal["id"])["id"]),range(10)))
    assert len(set(ids)) == 1
    run(w.run_once())
    assert c.queue_capture(deal["id"])["id"] == get(db,deal["id"],"CAPTURE")["id"]
    assert payment(db,deal["id"])["payment_state"] == "CAPTURED"
    assert len([x for x in p.calls if x[0]=="POST" and x[1].endswith("/capture")]) == 1


def test_lost_create_response_reuses_original_payload_and_key(db,deal,clock):
    p = FakePayPal(deal,clock)
    p.drop.add("/v2/checkout/orders")
    c,w = Coordinator(db,clock),Worker(db,p,clock)
    c.queue_create(deal["id"])
    run(w.run_once())
    assert get(db,deal["id"],"CREATE")["state"] == "RECONCILING"
    clock.advance()
    run(w.run_once())
    posts = [x for x in p.calls if x[0]=="POST"]
    assert posts[0][2:] == posts[1][2:]
    assert payment(db,deal["id"])["order_id"] == "ORDER1"


def test_lost_authorization_recovers_known_order_and_provider_timestamp(db,deal,clock):
    p = FakePayPal(deal,clock)
    c,w = Coordinator(db,clock),Worker(db,p,clock)
    c.queue_create(deal["id"]); run(w.run_once())
    c.queue_authorize(deal["id"])
    p.drop.add("/v2/checkout/orders/ORDER1/authorize")
    run(w.run_once())
    created = clock()
    clock.advance(3600)
    run(w.run_once())
    d = payment(db,deal["id"])
    assert d["authorized_at"] == created
    assert d["capture_deadline"] == created+timedelta(hours=48)
    assert len([x for x in p.calls if x[0]=="POST" and x[1].endswith("/authorize")]) == 1


def test_authorization_post_stops_before_six_hours(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    # Simulate a request sent with no observable provider result, preserving the original key.
    with db.tx() as conn:
        conn.execute("UPDATE operations SET state='RECONCILING',next_attempt_at=%s WHERE deal_id=%s AND kind='AUTHORIZE'",(clock(),deal["id"]))
        conn.execute("UPDATE deals SET authorization_id=NULL,authorized_at=NULL,capture_deadline=NULL WHERE id=%s",(deal["id"],))
    p.auth = None
    before = len([x for x in p.calls if x[0]=="POST"])
    key = get(db,deal["id"],"AUTHORIZE")["request_id"]
    clock.advance(6*3600)
    run(w.run_once())
    assert len([x for x in p.calls if x[0]=="POST"]) == before
    assert get(db,deal["id"],"AUTHORIZE")["request_id"] == key


def test_capture_timeout_recovers_missing_id_through_order(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    p.drop.add("/v2/payments/authorizations/AUTH1/capture")
    c.queue_capture(deal["id"]); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"] == "RECONCILING"
    clock.advance(); run(w.run_once())
    assert payment(db,deal["id"])["capture_id"] == "CAPTURE1"
    assert payment(db,deal["id"])["payment_state"] == "CAPTURED"


def test_missing_capture_id_recovers_from_verified_event(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    p.drop.add("/v2/payments/authorizations/AUTH1/capture")
    c.queue_capture(deal["id"]); run(w.run_once())
    p.hide_order_capture = True
    event = {"id":uid(),"event_type":"PAYMENT.CAPTURE.COMPLETED","resource":p.capture}
    with db.tx() as conn:
        conn.execute("INSERT INTO webhook_inbox(event_id,raw_body,event) VALUES(%s,%s,%s)",(event["id"],b"fixture",Jsonb(event)))
    clock.advance(); run(w.run_once())
    assert payment(db,deal["id"])["capture_id"] == "CAPTURE1"
    assert len([x for x in p.calls if x[0]=="POST" and x[1].endswith("/capture")]) == 1


def test_capture_same_key_retry_when_no_resource_visible(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    p.drop.add("/v2/payments/authorizations/AUTH1/capture")
    p.hide_order_capture = True
    c.queue_capture(deal["id"]); run(w.run_once())
    clock.advance(); run(w.run_once())
    posts = [x for x in p.calls if x[0]=="POST" and x[1].endswith("/capture")]
    assert len(posts)==2 and posts[0][2:]==posts[1][2:]
    assert len([v for v in p.cache.values() if v.get("id")=="CAPTURE1"])==1


def test_stale_worker_write_fenced_and_void_cannot_conflict(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"])
    stale = w.claim()
    assert w.prepare_dispatch(stale)
    clock.advance(120)
    c.request_void(deal["id"])
    replacement = w.claim()
    assert replacement.op["kind"] == "CAPTURE"
    fake_result = {"id":"CAPTURE1","status":"COMPLETED","amount":{"value":"750.00","currency_code":"USD"}}
    assert w.finish(stale,fake_result) is False
    run(w.execute(replacement))
    # A CREATED authorization read is not proof the paused worker cannot send later.
    run(w.run_once())
    assert get(db,deal["id"],"VOID")["state"] == "MANUAL_REVIEW"
    assert not any(x[0]=="POST" and x[1].endswith("/void") for x in p.calls)
    assert not payment(db,deal["id"])["capture_id"]
    # Demonstrate the actual fencing limit: paused code may still send HTTP.
    run(p.post(stale.op["endpoint"],stale.op["payload"],str(stale.op["request_id"])))
    assert w.finish(stale,p.capture) is False
    clock.advance(); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"] == "CAPTURED"


def test_invalidated_capture_rebinds_only_before_first_dispatch(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    first = c.queue_capture(deal["id"])
    with db.tx() as conn:
        d = lock_deal(conn,deal["id"])
        c.invalidate_evidence(conn,d,None,"new evidence")
    assert get(db,deal["id"],"CAPTURE")["state"] == "BLOCKED"
    decision = passing_decision(db,deal["id"])
    second = c.queue_capture(deal["id"])
    assert first["id"]==second["id"] and first["request_id"]==second["request_id"]
    assert str(second["decision_id"]) == decision
    run(w.run_once())
    with pytest.raises(psycopg.errors.RaiseException):
        with db.tx() as conn:
            conn.execute("UPDATE operations SET decision_id=NULL WHERE id=%s",(second["id"],))


def test_void_lost_response_read_only_recovery(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    p.drop.add("/v2/payments/authorizations/AUTH1/void")
    c.request_void(deal["id"]); run(w.run_once())
    clock.advance(); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"] == "VOIDED"
    assert len([x for x in p.calls if x[0]=="POST" and x[1].endswith("/void")])==1


@pytest.mark.parametrize("status,expected",[("PENDING","CAPTURE_PENDING"),("DECLINED","CAPTURE_FAILED")])
def test_pending_or_declined_is_not_success(db,deal,clock,status,expected):
    c,w,p = setup_authorized(db,deal,clock)
    p.capture_status = status
    c.queue_capture(deal["id"]); run(w.run_once())
    assert payment(db,deal["id"])["payment_state"] == expected


def test_no_capture_after_fulfillment_deadline(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    c.queue_capture(deal["id"])
    clock.advance(48*3600)
    run(w.run_once())
    assert not any(x[0]=="POST" and x[1].endswith("/capture") for x in p.calls)


def test_database_freezes_payment_payload(db,deal,clock):
    c,w,p = setup_authorized(db,deal,clock)
    op = c.queue_capture(deal["id"])
    with pytest.raises(psycopg.errors.RaiseException):
        with db.tx() as conn:
            conn.execute("UPDATE operations SET payload='{}' WHERE id=%s",(op["id"],))
