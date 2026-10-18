from copy import deepcopy
from datetime import timedelta

import pytest

from acceptance.paypal_proof import validate_resources,operation_checks
from accord.coordinator import Conflict
from tests.test_operations import setup_authorized,run,payment


def capture_fixture(db,deal,clock):
    c,w,p=setup_authorized(db,deal,clock);c.queue_capture(deal["id"]);run(w.run_once())
    return payment(db,deal["id"]),p


def test_real_record_resource_validation_rejects_other_amount_or_capture(db,deal,clock):
    d,p=capture_fixture(db,deal,clock)
    validate_resources(d,p.order(),p.auth,p.capture)
    wrong=deepcopy(p.capture);wrong["amount"]["value"]="7.50"
    with pytest.raises(Conflict):validate_resources(d,p.order(),p.auth,wrong)
    wrong=deepcopy(p.order());wrong["purchase_units"][0]["payments"]["captures"].append({"id":"SECOND"})
    with pytest.raises(ValueError,match="singular"):validate_resources(d,wrong,p.auth,p.capture)


def test_real_record_rejects_locally_extended_authorization_deadline(db,deal,clock):
    d,p=capture_fixture(db,deal,clock);d["capture_deadline"]+=timedelta(seconds=1)
    with pytest.raises(ValueError,match="provider creation time"):validate_resources(d,p.order(),p.auth,p.capture)


def test_confirmed_capture_alone_cannot_certify_timeout_or_duplicate_recovery(db,deal,clock):
    d,p=capture_fixture(db,deal,clock)
    with db.tx() as conn:
        operations=conn.execute("SELECT * FROM operations WHERE deal_id=%s",(deal["id"],)).fetchall()
        dispatches=conn.execute("SELECT * FROM dispatches").fetchall()
        events=conn.execute("SELECT * FROM audit WHERE deal_id=%s",(deal["id"],)).fetchall()
    checks=operation_checks(operations,dispatches,events,d)
    assert checks["single_capture_operation"] and checks["frozen_key_and_payload"] and checks["recovery_confirmed"]
    assert not checks["uncertain_response_recorded"]
    assert not checks["duplicate_submissions_audited"]
