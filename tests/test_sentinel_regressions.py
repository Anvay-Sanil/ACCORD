"""Regression cases found by the Sentinel audit; provider behavior remains doubled."""
import copy

import pytest
from psycopg.types.json import Jsonb

from accord.coordinator import Conflict
from accord.db import uid
from accord.worker import Worker
from accord.paypal import event_resource_ids
from accord.evidence import review_interpretation
from tests.test_evidence import specimen
from tests.test_operations import get, payment, run, setup_authorized
from tests.fake_paypal import FakePayPal
from accord.coordinator import Coordinator


def inbox(db, event):
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO webhook_inbox(event_id,raw_body,event) VALUES(%s,%s,%s)",
            (event["id"], b"verified-test-fixture", Jsonb(event)),
        )


def test_webhook_arriving_during_claim_is_not_consumed_without_wakeup(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    c.queue_capture(deal["id"])
    run(w.run_once())
    old_snapshot = copy.deepcopy(p.capture)
    with db.tx() as conn:
        conn.execute(
            "UPDATE operations SET state='RECONCILING',next_attempt_at=%s WHERE deal_id=%s AND kind='CAPTURE'",
            (clock(), deal["id"]),
        )
    claim = w.claim()
    assert claim
    p.capture["status"] = "REFUNDED"
    event = {"id": uid(), "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {
        "id": "REFUND1", "supplementary_data": {"related_ids": {
            "order_id": "ORDER1", "capture_id": "CAPTURE1"}},
    }}
    inbox(db, event)
    w.wake_webhooks()
    with db.tx() as conn:
        assert conn.execute("SELECT processed_at FROM webhook_inbox WHERE event_id=%s", (event["id"],)).fetchone()["processed_at"] is None
    # The slow reader finishes with the snapshot it obtained before the refund.
    assert w.finish(claim, old_snapshot)
    clock.advance(2)
    w.wake_webhooks()
    run(w.run_once())
    assert payment(db, deal["id"])["payment_state"] == "REFUNDED"


def test_refund_with_capture_up_link_wakes_canonical_capture_read(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    c.queue_capture(deal["id"])
    run(w.run_once())
    p.capture["status"] = "PARTIALLY_REFUNDED"
    event = {"id": uid(), "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource_type": "refund", "resource": {
        "id": "REFUND1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "50.00"},
        "links": [{"rel": "up", "method": "GET", "href": "https://api.sandbox.paypal.com/v2/payments/captures/CAPTURE1"}],
    }}
    inbox(db, event)
    w.wake_webhooks()
    assert get(db, deal["id"], "CAPTURE")["state"] == "RECONCILING"
    run(w.run_once())
    assert payment(db, deal["id"])["payment_state"] == "PARTIALLY_REFUNDED"
    assert not any("refunds/REFUND1" in call[1] for call in p.calls)


def test_pending_authorization_keeps_polling_until_ready(db, deal, clock):
    c, p = Coordinator(db, clock), FakePayPal(deal, clock)
    w = Worker(db, p, clock)
    c.queue_create(deal["id"])
    run(w.run_once())
    original = p.get

    async def pending_auth(path):
        value = await original(path)
        if "/authorizations/" in path:
            value["status"] = "PENDING"
        return value

    p.get = pending_auth
    c.queue_authorize(deal["id"])
    run(w.run_once())
    assert get(db, deal["id"], "AUTHORIZE")["state"] == "PENDING"
    assert payment(db, deal["id"])["payment_state"] == "AUTHORIZATION_PENDING"
    with pytest.raises(Conflict):
        c.queue_capture(deal["id"])
    p.get = original
    clock.advance()
    run(w.run_once())
    assert payment(db, deal["id"])["payment_state"] == "AUTHORIZED"
    assert len([x for x in p.calls if x[0] == "POST" and x[1].endswith("/authorize")]) == 1


def test_confirmed_voided_authorization_cannot_queue_capture(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    p.auth["status"] = "VOIDED"
    event = {"id": uid(), "event_type": "PAYMENT.AUTHORIZATION.VOIDED", "resource": p.auth}
    inbox(db, event)
    w.wake_webhooks()
    run(w.run_once())
    assert payment(db, deal["id"])["payment_state"] == "VOIDED"
    with pytest.raises(Conflict):
        c.queue_capture(deal["id"])


def test_external_void_after_queue_is_observed_before_capture_post(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    c.queue_capture(deal["id"])
    # The merchant changed the provider resource outside ACCORD; no webhook yet.
    p.auth["status"] = "VOIDED"
    run(w.run_once())
    assert not any(x[0] == "POST" and x[1].endswith("/capture") for x in p.calls)
    assert get(db, deal["id"], "CAPTURE")["state"] == "BLOCKED"
    assert payment(db, deal["id"])["payment_state"] == "VOIDED"


def test_full_refund_does_not_regress_to_partial_refund(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    c.queue_capture(deal["id"])
    run(w.run_once())
    p.capture["status"] = "REFUNDED"
    for status, expected in (("REFUNDED", "REFUNDED"), ("PARTIALLY_REFUNDED", "REFUNDED")):
        p.capture["status"] = status
        event = {"id": uid(), "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {
            "id": "REFUND1", "supplementary_data": {"related_ids": {"capture_id": "CAPTURE1"}},
        }}
        inbox(db, event)
        w.wake_webhooks()
        run(w.run_once())
        assert payment(db, deal["id"])["payment_state"] == expected


def test_queued_capture_waits_for_authorization_pending_then_ready(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    c.queue_capture(deal["id"])
    p.auth["status"] = "PENDING"
    run(w.run_once())
    assert get(db, deal["id"], "CAPTURE")["state"] == "PENDING"
    assert payment(db, deal["id"])["payment_state"] == "AUTHORIZATION_PENDING"
    assert not any(x[0] == "POST" and x[1].endswith("/capture") for x in p.calls)
    p.auth["status"] = "CREATED"
    clock.advance()
    run(w.run_once())
    assert payment(db, deal["id"])["payment_state"] == "CAPTURED"


def test_consumed_authorization_with_invisible_capture_does_not_trigger_post_retry(db, deal, clock):
    c, w, p = setup_authorized(db, deal, clock)
    p.hide_order_capture = True
    p.drop.add("/v2/payments/authorizations/AUTH1/capture")
    c.queue_capture(deal["id"])
    run(w.run_once())
    p.auth["status"] = "CAPTURED"
    clock.advance()
    run(w.run_once())
    assert get(db, deal["id"], "CAPTURE")["state"] == "RECONCILING"
    assert payment(db, deal["id"])["capture_id"] is None
    assert len([x for x in p.calls if x[0] == "POST" and x[1].endswith("/capture")]) == 1


def test_denied_authorization_is_observed_and_cannot_capture(db, deal, clock):
    c, p = Coordinator(db, clock), FakePayPal(deal, clock)
    w = Worker(db, p, clock)
    c.queue_create(deal["id"])
    run(w.run_once())
    original = p.get

    async def denied(path):
        value = await original(path)
        if "/authorizations/" in path:
            value["status"] = "DENIED"
        return value

    p.get = denied
    c.queue_authorize(deal["id"])
    run(w.run_once())
    assert get(db, deal["id"], "AUTHORIZE")["state"] == "FAILED"
    assert payment(db, deal["id"])["payment_state"] == "AUTHORIZATION_DENIED"
    with pytest.raises(Conflict):
        c.queue_capture(deal["id"])


@pytest.mark.parametrize("href", [
    "http://api.sandbox.paypal.com/v2/payments/captures/CAPTURE1",
    "https://api-m.paypal.com/v2/payments/captures/CAPTURE1",
    "https://api.sandbox.paypal.com.evil.invalid/v2/payments/captures/CAPTURE1",
    "https://user@api.sandbox.paypal.com/v2/payments/captures/CAPTURE1",
    "https://api.sandbox.paypal.com:8443/v2/payments/captures/CAPTURE1",
    "https://api.sandbox.paypal.com/v2/payments/captures/CAPTURE1?redirect=elsewhere",
])
def test_refund_link_correlation_rejects_unsafe_or_wrong_environment_url(href):
    event = {"event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {
        "id": "REFUND1", "links": [{"rel": "up", "href": href}],
    }}
    with pytest.raises(ValueError):
        event_resource_ids(event)


def test_refund_link_conflicting_with_related_id_is_rejected():
    event = {"event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {
        "id": "REFUND1", "supplementary_data": {"related_ids": {"capture_id": "CAPTURE2"}},
        "links": [{"rel": "up", "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/CAPTURE1"}],
    }}
    with pytest.raises(ValueError, match="Conflicting"):
        event_resource_ids(event)


def test_buyer_review_cannot_relabel_recognized_wrong_grade(db, deal):
    c = Coordinator(db)
    identity = c.add_document(deal["id"], deal["supplier_id"], "supplier", "certificate", b"%PDF-fixture")
    doc, finding = specimen("PO: PO-001\nLot: LOT-001\nRequired grade: 316L\nDelivered grade: 304", value="316L")
    with db.tx() as conn:
        document = conn.execute("SELECT version FROM documents WHERE id=%s", (identity,)).fetchone()
        conn.execute("UPDATE documents SET parsed=%s WHERE id=%s", (Jsonb(doc["parsed"]), identity))
    finding = finding.model_copy(update={"document_id": identity, "document_version": document["version"]})
    with pytest.raises(Conflict, match="recognized delivered value"):
        review_interpretation(db, deal["id"], deal["buyer_id"], finding,
                              "I selected 316L from the requirement text.")
