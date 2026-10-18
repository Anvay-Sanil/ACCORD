# ACCORD Phase 3 — Sentinel audit

Disposition: the corrected payment foundation clears local review. Phase 3 acceptance remains conditional on real-provider and deployment evidence.

The original package's 54 tests were independently reproduced. Seven additional failure scenarios were reproduced before their fixes. The corrected full suite passes 71 tests on actual PostgreSQL. The source ZIP and original source folder were preserved.

Original ZIP SHA-256: `50352e2c7dba90703d9839911ecb1261b3af1b060a23821468d379887c518067`.

## [P1] Review can relabel delivered 304 as required 316L

Original location: `accord/evidence.py:161`.

The quote only needed to contain the proposed value somewhere. A certificate containing required 316L and delivered 304 could be reviewed as delivered 316L; human evaluation bypassed semantic admission.

Correction: Reject reviews that contradict recognized delivery associations, and apply that check again to stored reviews during evaluation. Unknown prose still requires explicit buyer interpretation.

Regression: `test_buyer_review_cannot_relabel_recognized_wrong_grade`.

## [P1] Verified wakeup lost while a worker is active

Original location: `accord/worker.py:385`.

The handler skipped the operation update for a busy deal but still marked the inbox event processed. A slow reader could finish with an older completed snapshot and leave a refund permanently unobserved.

Correction: Retain the inbox event until matching deals are idle; queue the canonical refresh before marking it processed.

Regression: `test_webhook_arriving_during_claim_is_not_consumed_without_wakeup`.

## [P1] Link-only refund cannot find its capture

Original location: `accord/worker.py:379`.

Correlation required supplementary related IDs or a matching resource ID. A refund resource's own ID is a refund ID; its capture can instead be represented by an up link. That event never woke the completed capture operation.

Correction: Parse one consistent Sandbox capture relationship from related IDs or a strictly validated up link. Construct canonical requests from the extracted ID; never fetch event URLs.

Regression: `test_refund_with_capture_up_link_wakes_canonical_capture_read`.

## [P1] Pending authorization stops automatic recovery

Original location: `accord/worker.py:279`.

PENDING is a documented authorization state. The original implementation raised Conflict and moved it to MANUAL_REVIEW, which the worker does not poll. The buyer could remain stuck even after authorization became ready.

Correction: Persist AUTHORIZATION_PENDING/PENDING and poll reads until it resolves. Record DENIED as a failed authorization and prohibit capture.

Regression: `test_pending_authorization_keeps_polling_until_ready`.

## [P1] Locally confirmed void is still capture-eligible

Original location: `accord/coordinator.py:86`.

Eligibility checked an authorization ID and deadline but ignored a confirmed VOIDED financial state. A passing evidence decision still created a capture operation.

Correction: Require available authorization state at queueing and again before first dispatch.

Regression: `test_confirmed_voided_authorization_cannot_queue_capture`.

## [P1] External void between queueing and dispatch is missed

Original location: `accord/worker.py:214`.

Recovery inspected captures but did not read authorization availability before issuing a capture. The supplier can change its merchant resource outside ACCORD; no webhook is required to arrive first.

Correction: Read canonical authorization state before an unobserved capture POST. Block an unsent voided/denied authorization, wait on pending, and reconcile consumed authorizations without blind replay. A subsequent provider change can still race the final POST; PayPal remains the authority for acceptance.

Regression: `test_external_void_after_queue_is_observed_before_capture_post`.

## [P2] Full refund regresses to partial refund

Original location: `accord/worker.py:299`.

The stale-observation guards protected completion from pending/failed reads and refunds from completed reads, but allowed REFUNDED to become PARTIALLY_REFUNDED.

Correction: Preserve confirmed full refund/reversal facts when an older partial-refund snapshot arrives.

Regression: `test_full_refund_does_not_regress_to_partial_refund`.

## Corrected code blocks

These excerpts are taken from the delivered files. Complete implementations and context are in PHASE3_REVIEWED_SOURCE.md and the ZIP.

### accord/evidence.py — check_reviewed_value

```python
def check_reviewed_value(document, finding):
    """Human review may resolve unknown prose, but cannot rewrite a known field."""
    for po, lot, value, _ in association_rows(document):
        if (po, lot) == (finding.purchase_order, finding.lot) and value != finding.value:
            raise Conflict("Review contradicts recognized delivered value; replace or supersede the evidence")
```

### accord/worker.py — authorization_unavailable

```python
    def authorization_unavailable(self, claim, resource):
        """Observe an unavailable authorization without treating it as a capture."""
        status = resource["status"]
        with self.db.tx() as conn:
            d = self.fenced(conn, claim)
            if not d:
                return False
            o = operation(conn, d["id"], "CAPTURE")
            payment = {"PENDING": "AUTHORIZATION_PENDING", "DENIED": "AUTHORIZATION_DENIED",
                       "VOIDED": "VOIDED", "EXPIRED": "EXPIRED"}.get(status, "RECONCILING")
            state = "PENDING" if status == "PENDING" else (
                "RECONCILING" if o["first_dispatch_at"] or status in ("CAPTURED", "PARTIALLY_CAPTURED") else "BLOCKED")
            if d["payment_state"] not in KNOWN_PAYMENT_STATES:
                conn.execute("UPDATE deals SET payment_state=%s WHERE id=%s", (payment, d["id"]))
            conn.execute("""UPDATE operations SET state=%s,last_error=%s,next_attempt_at=%s WHERE id=%s""",
                         (state, "Authorization status: " + status,
                          self.clock() + timedelta(seconds=60), o["id"]))
            audit(conn, d["id"], "CAPTURE_AUTHORIZATION_UNAVAILABLE", {"status": status, "state": state})
            self.release(conn, claim)
            return True
```

### accord/worker.py — wake_webhooks

```python
    def wake_webhooks(self):
        with self.db.tx() as conn:
            rows = conn.execute("""SELECT * FROM webhook_inbox WHERE processed_at IS NULL AND next_attempt_at<=%s
                 ORDER BY verified_at LIMIT 20 FOR UPDATE SKIP LOCKED""",(self.clock(),)).fetchall()
            for row in rows:
                event = row["event"]
                ids = event_resource_ids(event)
                deals = conn.execute("""SELECT * FROM deals WHERE order_id=%s OR authorization_id=%s
                    OR capture_id=%s FOR UPDATE""",
                    (ids["order"], ids["authorization"], ids["capture"])).fetchall()
                if not deals:
                    conn.execute("UPDATE webhook_inbox SET attempts=attempts+1,next_attempt_at=%s WHERE event_id=%s",
                                 (self.clock()+timedelta(seconds=60),row["event_id"]))
                    continue  # May arrive before the operation response has been persisted.
                # A worker may already hold an older provider snapshot. Consuming
                # this event now would lose the only wakeup after it commits that
                # snapshot. Retain the inbox event until all matching deals are idle.
                if any(d["active_operation"] for d in deals):
                    conn.execute("UPDATE webhook_inbox SET next_attempt_at=%s WHERE event_id=%s",
                                 (self.clock() + timedelta(seconds=1), row["event_id"]))
                    continue
                for d in deals:
                    kind = "CAPTURE" if event.get("event_type","").startswith("PAYMENT.CAPTURE.") else "AUTHORIZE"
                    conn.execute("""UPDATE operations SET state='RECONCILING',next_attempt_at=%s
                      WHERE deal_id=%s AND kind=%s AND first_dispatch_at IS NOT NULL
                      AND (kind='CAPTURE' OR state<>'SUCCEEDED' OR %s='PAYMENT.AUTHORIZATION.VOIDED')""",
                      (self.clock(),d["id"],kind,event.get("event_type","")))
                conn.execute("UPDATE webhook_inbox SET processed_at=%s WHERE event_id=%s",(self.clock(),row["event_id"]))
```

## File-by-file review scope

- `api.py`, `config.py`: session, Origin/CSRF, membership, role, body-limit and webhook ingress paths. Refund correlation validation was added to webhook ingress.
- `coordinator.py`, `worker.py`, `paypal.py`: mutations, eligibility, immutable requests, leases, uncertain outcomes, provider resource bindings, retry deadlines, HTTP boundaries, and event recovery. Corrections are in the diff; no new payment key is created during uncertainty.
- `evidence.py`, `extraction_worker.py`: supported-layout admission, human review, decision generations, model request/output handling, and extraction provenance. The human review bypass was closed; actual model utility remains unverified.
- `parser.py`, `pdf_child.py`: resource-limited parsing subprocess. Existing Windows parse/memory/wall tests were rerun; Linux behavior remains unverified.
- `migrations/`, `provision_db.py`, `seed.py`, `db.py`, `contract.py`: append-only records, immutable operation fields, runtime grants, bootstrap approvals, transaction/timeout handling, and the frozen mandate. No migration changes were required.
- `compose.yaml`, `Dockerfile`, configuration examples and README: static inspection. Containers, service isolation and public hosting were not exercised by this audit.
- `tests/`: original 54-test baseline, failing reproductions, and final 71-test suite. Provider and model doubles are not Sandbox or model acceptance evidence.

## Acceptance still outstanding

Record actual supplier/buyer Sandbox provisioning, Checkout approval, authorization/capture, signature-verified real-transaction webhooks, uncertain-response recovery, and provider idempotency behavior. Execute real extraction on varied/adversarial PDFs. Build/run Docker on Linux and exercise the browser checkout/evidence journey. Preserve the existing distinction between evidence passed, payment pending, and confirmed capture. Do not label the package production-ready from test-double results.

The source's exact grammar is a material product limitation: unsupported wording/layouts require review. Measure real model proposals separately from backend admissibility before claiming useful general document interpretation. Submission licensing, hosted judge access, README polish and the under-three-minute video belong to the remaining submission work.

## Official references checked

- PayPal documents PENDING, DENIED, CREATED, CAPTURED, PARTIALLY_CAPTURED and VOIDED authorization states: [Authorization status](https://developer.paypal.com/sdk/payment/v2/definitions/authorization_status).
- PayPal's refund resource example links upward to a capture: [Show refund details](https://developer.paypal.com/api/payments/v2/refunds-get/). The correction accepts only Sandbox capture relationships and reconstructs the fixed-base request.
- The pinned model supports Structured Outputs and Responses: [GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1), [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). The request configuration was reviewed against official OpenAI documentation; no real model run was performed.

The original temporary PostgreSQL instance on localhost:55439 remains running. Automatic approval review rejected cleanup of the isolated review database and generated caches because sandbox approval is disabled. The audit's separate database remains present; no stop of the original service is claimed. The ZIP excludes generated caches.
