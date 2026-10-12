"""Payment-only worker. PDF parsing and model calls run in a separate process/service."""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from .coordinator import Coordinator, Conflict, insert_operation, lock_deal, operation
from .db import DB, audit, digest, uid, utcnow
from .paypal import PayPal, ProviderError, backoff, resource_path, event_resource_ids

WORK_STATES = ("QUEUED", "IN_FLIGHT", "RECONCILING", "PENDING")
KNOWN_PAYMENT_STATES = ("CAPTURED", "REFUNDED", "PARTIALLY_REFUNDED", "REVERSED", "VOIDED", "EXPIRED")


@dataclass
class Claim:
    deal: dict
    op: dict
    generation: int


@dataclass
class UnavailableAuthorization:
    resource: dict


def parse_time(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise Conflict("Provider timestamp has no timezone")
    return date


def check_amount(resource):
    from decimal import Decimal, InvalidOperation
    try:
        amount = resource["amount"]
        if amount["currency_code"] != "USD" or Decimal(amount["value"]) != Decimal("750.00"):
            raise Conflict("Provider amount mismatch")
    except (KeyError, InvalidOperation, TypeError):
        raise Conflict("Provider amount missing or invalid")


def validate_order(order, deal, require_payer=False):
    units = order.get("purchase_units", [])
    if len(units) != 1 or order.get("intent") != "AUTHORIZE":
        raise Conflict("Unexpected order structure")
    unit = units[0]
    check_amount(unit)
    if (unit.get("payee", {}).get("merchant_id") != deal["merchant_id"] or
            unit.get("custom_id") != str(deal["id"])):
        raise Conflict("Provider merchant/deal binding mismatch")
    if require_payer:
        payer = order.get("payer", {}).get("payer_id") or order.get("payment_source", {}).get("paypal", {}).get("account_id")
        if payer != deal["payer_id"]:
            raise Conflict("Unexpected PayPal buyer")
    return unit


class Worker:
    def __init__(self, db, paypal, clock=utcnow):
        self.db, self.paypal, self.clock = db, paypal, clock

    def claim(self):
        now = self.clock()
        with self.db.tx() as conn:
            candidates = conn.execute("""SELECT DISTINCT deal_id FROM operations
                WHERE state = ANY(%s) AND next_attempt_at <= %s LIMIT 100""", (list(WORK_STATES), now)).fetchall()
            for candidate in candidates:
                d = conn.execute("SELECT * FROM deals WHERE id=%s FOR UPDATE SKIP LOCKED", (candidate["deal_id"],)).fetchone()
                if not d or (d["lease_until"] and d["lease_until"] > now):
                    continue
                # Lease expiry always recovers the old operation before another kind can run.
                old = None
                if d["active_operation"]:
                    old = conn.execute("SELECT * FROM operations WHERE id=%s", (d["active_operation"],)).fetchone()
                if old and old["state"] in WORK_STATES:
                    chosen = old
                    conn.execute("UPDATE operations SET state=%s WHERE id=%s",
                                 ("RECONCILING" if old["first_dispatch_at"] else "QUEUED", old["id"]))
                    audit(conn, d["id"], "LEASE_EXPIRED_RECONCILIATION", {"operation": str(old["id"])})
                else:
                    chosen = conn.execute("""SELECT * FROM operations WHERE deal_id=%s AND state=ANY(%s)
                       AND next_attempt_at<=%s ORDER BY CASE kind WHEN 'CREATE' THEN 1 WHEN 'AUTHORIZE'
                       THEN 2 WHEN 'CAPTURE' THEN 3 ELSE 4 END LIMIT 1""", (d["id"],list(WORK_STATES),now)).fetchone()
                if not chosen:
                    continue
                generation = d["lease_generation"] + 1
                conn.execute("UPDATE deals SET lease_generation=%s,lease_until=%s,active_operation=%s WHERE id=%s",
                             (generation, now + timedelta(seconds=90), chosen["id"], d["id"]))
                return Claim(d, chosen, generation)
        return None

    def fenced(self, conn, claim):
        d = lock_deal(conn, claim.deal["id"])
        if d["lease_generation"] != claim.generation or d["active_operation"] != claim.op["id"]:
            return None
        if not d["lease_until"] or d["lease_until"] <= self.clock():
            return None
        return d

    def release(self, conn, claim):
        conn.execute("UPDATE deals SET active_operation=NULL,lease_until=NULL WHERE id=%s", (claim.deal["id"],))

    def defer(self, claim, state, reason, delay=30):
        with self.db.tx() as conn:
            d = self.fenced(conn, claim)
            if not d:
                return False
            current = operation(conn,d["id"],claim.op["kind"])
            if current["state"] == "BLOCKED" and current["first_dispatch_at"] is None:
                state = "BLOCKED"
            conn.execute("UPDATE operations SET state=%s,last_error=%s,next_attempt_at=%s,failures=failures+1 WHERE id=%s",
                         (state, reason, self.clock()+timedelta(seconds=delay),claim.op["id"]))
            if (claim.op["first_dispatch_at"] or state == "RECONCILING") and d["payment_state"] not in KNOWN_PAYMENT_STATES:
                conn.execute("UPDATE deals SET payment_state='RECONCILING' WHERE id=%s", (d["id"],))
            audit(conn,d["id"],"OPERATION_DEFERRED",{"kind":claim.op["kind"],"state":state,"reason":reason})
            self.release(conn,claim)
            return True

    def prepare_dispatch(self, claim):
        """Persist before HTTP. The returned lease cannot fence a remote HTTP side effect."""
        now = self.clock()
        with self.db.tx() as conn:
            d = self.fenced(conn,claim)
            if not d:
                return None
            o = operation(conn,d["id"],claim.op["kind"])
            reason, state = None, "BLOCKED"
            if digest(o["payload"]) != o["payload_hash"]:
                reason = "Request integrity mismatch"
            elif o["state"] == "BLOCKED":
                reason = "Queued decision invalidated"
            elif d["cancel_requested"] and o["kind"] != "VOID":
                reason = "Cancellation requires read-only reconciliation" if o["first_dispatch_at"] else "Cancelled before dispatch"
                state = "RECONCILING" if o["first_dispatch_at"] else "CANCELLED"
            elif o["kind"] == "CAPTURE":
                decision = conn.execute("SELECT * FROM decisions WHERE id=%s",(o["decision_id"],)).fetchone()
                if (not o["first_dispatch_at"] and
                    d["payment_state"] not in ("AUTHORIZED", "CAPTURE_QUEUED")):
                    reason = "Authorization is not confirmed available for capture"
                elif d["payment_state"] in ("VOIDED", "EXPIRED", "AUTHORIZATION_DENIED"):
                    reason, state = "Authorization cannot be captured; read-only reconciliation", "RECONCILING"
                elif (not decision or decision["outcome"] != "PASS" or
                    decision["evidence_generation"] != d["evidence_generation"] or
                    decision["policy_hash"] != d["policy_hash"] or not d["delivery_ack"] or
                    not d["buyer_approved"] or not d["supplier_approved"]):
                    reason = "Evidence is no longer eligible"
                    state = "RECONCILING" if o["first_dispatch_at"] else "BLOCKED"
                elif not d["capture_deadline"] or now + timedelta(seconds=30) >= d["capture_deadline"]:
                    reason = "Capture initiation deadline reached"
                    state = "RECONCILING" if o["first_dispatch_at"] else "BLOCKED"
            elif o["kind"] == "VOID":
                cap = operation(conn,d["id"],"CAPTURE")
                if cap and cap["first_dispatch_at"]:
                    reason, state = "A capture has been dispatched; automatic conflicting void is prohibited", "MANUAL_REVIEW"
                elif o["first_dispatch_at"]:
                    # The reference advertises a key, but no retention window. Read-only recovery.
                    reason, state = "Void outcome unknown; no blind POST retry", "RECONCILING"
            deadline = o["retry_deadline"]
            if deadline and now + timedelta(seconds=30) >= deadline:
                reason = "POST retry deadline reached; read-only reconciliation"
                state = "RECONCILING" if o["first_dispatch_at"] else "BLOCKED"
            if reason:
                conn.execute("UPDATE operations SET state=%s,last_error=%s,next_attempt_at=%s WHERE id=%s",
                             (state,reason,now+timedelta(seconds=60),o["id"]))
                self.release(conn,claim)
                return None
            if not o["first_dispatch_at"]:
                deadline = now + timedelta(hours=5,minutes=55) if o["kind"] in ("CREATE","AUTHORIZE") else (
                    min(d["capture_deadline"],now+timedelta(minutes=5)) if o["kind"] == "CAPTURE" else now + timedelta(minutes=5))
                conn.execute("UPDATE operations SET first_dispatch_at=%s,retry_deadline=%s WHERE id=%s",(now,deadline,o["id"]))
            attempt_id = uid()
            conn.execute("""INSERT INTO dispatches(id,operation_id,lease_generation,request_id,payload_hash,started_at)
                VALUES(%s,%s,%s,%s,%s,%s)""",(attempt_id,o["id"],claim.generation,o["request_id"],o["payload_hash"],now))
            conn.execute("UPDATE operations SET state='IN_FLIGHT',attempts=attempts+1 WHERE id=%s",(o["id"],))
            state = {"CREATE":"CREATING_ORDER","AUTHORIZE":"AUTHORIZING","CAPTURE":"CAPTURING","VOID":"VOID_PENDING"}[o["kind"]]
            conn.execute("UPDATE deals SET payment_state=%s WHERE id=%s",(state,d["id"]))
            claim.op = operation(conn,d["id"],o["kind"])
            return attempt_id

    async def order(self, d, require_payer=True):
        value = await self.paypal.get(resource_path("order",d["order_id"]))
        if value.get("id") != d["order_id"]:
            raise Conflict("Order ID mismatch")
        validate_order(value,d,require_payer=require_payer)
        return value

    async def recover(self, claim):
        d,o = claim.deal,claim.op
        if o["kind"] == "CREATE":
            if d["order_id"]:
                value = await self.paypal.get(resource_path("order",d["order_id"]))
                if value.get("id") != d["order_id"]:
                    raise Conflict("Order ID mismatch")
                validate_order(value,d)
                return value
            return None  # Only the original CREATE key can recover an unknown order ID.
        if o["kind"] == "AUTHORIZE":
            order = await self.order(d,require_payer=False)
            authorizations = order["purchase_units"][0].get("payments",{}).get("authorizations",[])
            if len(authorizations) > 1:
                raise Conflict("More than one authorization observed")
            if authorizations:
                validate_order(order,d,require_payer=True)
                auth = await self.paypal.get(resource_path("authorization",authorizations[0]["id"]))
                if auth.get("id") != authorizations[0]["id"]:
                    raise Conflict("Authorization resource ID mismatch")
                return auth
            if order["status"] != "APPROVED":
                raise ProviderError(409,"BUYER_APPROVAL_OR_RECONCILIATION_REQUIRED")
            validate_order(order,d,require_payer=True)
            return None
        if o["kind"] == "VOID":
            auth = await self.paypal.get(resource_path("authorization",d["authorization_id"]))
            check_amount(auth)
            if auth.get("id") != d["authorization_id"]:
                raise Conflict("Authorization ID mismatch")
            if auth["status"] in ("VOIDED","EXPIRED"):
                return auth
            if auth["status"] != "CREATED":
                raise Conflict("Authorization is not safely voidable")
            return None
        capture_id = d["capture_id"]
        if not capture_id:
            order = await self.order(d)
            captures = order["purchase_units"][0].get("payments",{}).get("captures",[])
            if len(captures) > 1:
                raise Conflict("Unexpected multiple captures")
            if captures:
                capture_id = captures[0]["id"]
            else:
                # Events have already passed PayPal signature verification. Never fetch event URLs.
                with self.db.tx() as conn:
                    events = conn.execute("""SELECT event FROM webhook_inbox WHERE
                      event->'resource'->'supplementary_data'->'related_ids'->>'order_id'=%s OR
                      event->'resource'->'supplementary_data'->'related_ids'->>'authorization_id'=%s""",
                      (d["order_id"],d["authorization_id"])).fetchall()
                ids = set()
                for e in events:
                    event = e["event"]
                    if not event.get("event_type","").startswith("PAYMENT.CAPTURE."):
                        continue
                    candidate = event_resource_ids(event)["capture"]
                    if candidate:
                        ids.add(candidate)
                if len(ids) > 1:
                    raise Conflict("Multiple capture IDs in verified events")
                capture_id = next(iter(ids),None)
        if capture_id:
            capture = await self.paypal.get(resource_path("capture",capture_id))
            if capture.get("id") != capture_id:
                raise Conflict("Capture ID mismatch")
            related = capture.get("supplementary_data",{}).get("related_ids",{})
            if (related.get("order_id",d["order_id"]) != d["order_id"] or
                related.get("authorization_id",d["authorization_id"]) != d["authorization_id"]):
                raise Conflict("Capture relationship mismatch")
            return capture
        # A merchant can void an authorization outside ACCORD before its webhook
        # arrives. Inspect the canonical authorization before sending any capture
        # POST; a consumed authorization is not evidence that a retry is safe.
        auth = await self.paypal.get(resource_path("authorization", d["authorization_id"]))
        check_amount(auth)
        if auth.get("id") != d["authorization_id"]:
            raise Conflict("Authorization ID mismatch")
        if auth.get("payee", {}).get("merchant_id", d["merchant_id"]) != d["merchant_id"]:
            raise Conflict("Authorization merchant mismatch")
        status = auth.get("status")
        if status not in ("CREATED", "PENDING", "DENIED", "VOIDED", "EXPIRED", "CAPTURED", "PARTIALLY_CAPTURED"):
            raise Conflict("Unknown authorization status")
        if status != "CREATED":
            return UnavailableAuthorization(auth)
        # A pending authorization may become available between polling attempts.
        # Only recover the availability label; never erase known financial facts.
        with self.db.tx() as conn:
            current = self.fenced(conn, claim)
            if current and current["payment_state"] == "AUTHORIZATION_PENDING":
                conn.execute("UPDATE deals SET payment_state='AUTHORIZED' WHERE id=%s", (d["id"],))
        return None

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

    def finish(self,claim,value,attempt_id=None):
        o = claim.op
        now = self.clock()
        with self.db.tx() as conn:
            d = self.fenced(conn,claim)
            if not d:
                return False
            provider_id = value.get("id")
            if not provider_id:
                raise Conflict("Provider resource has no ID")
            known_id = d[{"CREATE":"order_id","AUTHORIZE":"authorization_id","CAPTURE":"capture_id","VOID":"authorization_id"}[o["kind"]]]
            if known_id and known_id != provider_id:
                raise Conflict("Provider resource ID changed")
            state, payment = "SUCCEEDED", None
            if o["kind"] == "CREATE":
                validate_order(value,d)
                conn.execute("UPDATE deals SET order_id=%s WHERE id=%s",(provider_id,d["id"]))
                payment = "AWAITING_BUYER"
            elif o["kind"] == "AUTHORIZE":
                check_amount(value)
                created = parse_time(value["create_time"])
                if created > now + timedelta(seconds=60):
                    raise Conflict("Authorization creation time is in the future")
                if d["authorized_at"] and d["authorized_at"] != created:
                    raise Conflict("Authorization timestamp changed")
                status = value["status"]
                if status not in ("CREATED", "PENDING", "DENIED", "VOIDED", "EXPIRED", "CAPTURED", "PARTIALLY_CAPTURED"):
                    raise Conflict("Unknown authorization status")
                conn.execute("UPDATE deals SET authorization_id=%s,authorized_at=%s,capture_deadline=%s WHERE id=%s",
                             (provider_id,created,created+timedelta(hours=48),d["id"]))
                payment = {"CREATED": "AUTHORIZED", "PENDING": "AUTHORIZATION_PENDING",
                           "DENIED": "AUTHORIZATION_DENIED", "VOIDED": "VOIDED", "EXPIRED": "EXPIRED"}.get(status, "RECONCILING")
                if status == "PENDING":
                    state = "PENDING"
                elif status == "DENIED":
                    state = "FAILED"
                elif status in ("CAPTURED", "PARTIALLY_CAPTURED"):
                    state = "MANUAL_REVIEW"
                if d["payment_state"] in ("CAPTURED","REFUNDED","PARTIALLY_REFUNDED","REVERSED"):
                    payment = d["payment_state"]
                    state = "SUCCEEDED"
                if d["cancel_requested"] and value["status"]=="CREATED":
                    insert_operation(conn,d,"VOID",resource_path("authorization",provider_id)+"/void",{})
            elif o["kind"] == "CAPTURE":
                check_amount(value)
                if value.get("payee",{}).get("merchant_id",d["merchant_id"]) != d["merchant_id"]:
                    raise Conflict("Capture merchant mismatch")
                status = value["status"]
                if status == "COMPLETED":
                    payment = "CAPTURED"
                elif status == "PENDING":
                    payment,state = "CAPTURE_PENDING","PENDING"
                elif status in ("DECLINED","FAILED"):
                    payment,state = "CAPTURE_FAILED","FAILED"
                elif status in ("REFUNDED","PARTIALLY_REFUNDED","REVERSED"):
                    payment = status
                else:
                    raise Conflict("Unknown capture state")
                # Never regress known completion because of an older pending/failed observation.
                if d["capture_id"] and d["capture_id"] != provider_id:
                    raise Conflict("Second capture ID observed")
                if d["payment_state"] in ("CAPTURED","REFUNDED","PARTIALLY_REFUNDED","REVERSED") and status in ("PENDING","DECLINED","FAILED"):
                    payment,state = d["payment_state"],"SUCCEEDED"
                if d["payment_state"] in ("REFUNDED","PARTIALLY_REFUNDED","REVERSED") and status == "COMPLETED":
                    payment = d["payment_state"]
                if d["payment_state"] in ("REFUNDED", "REVERSED") and status == "PARTIALLY_REFUNDED":
                    payment = d["payment_state"]
                conn.execute("UPDATE deals SET capture_id=%s WHERE id=%s",(provider_id,d["id"]))
            else:
                if value.get("status") not in ("VOIDED","EXPIRED"):
                    raise Conflict("Void is not confirmed")
                payment = value["status"]
            conn.execute("""UPDATE operations SET state=%s,provider_id=%s,last_error=NULL,failures=0,next_attempt_at=%s WHERE id=%s""",
                         (state,provider_id,now+timedelta(seconds=60),o["id"]))
            conn.execute("UPDATE deals SET payment_state=%s WHERE id=%s",(payment,d["id"]))
            if attempt_id:
                conn.execute("UPDATE dispatches SET finished_at=%s,outcome=%s WHERE id=%s",(now,state,attempt_id))
            audit(conn,d["id"],"PROVIDER_OBSERVED",{"kind":o["kind"],"provider_id":provider_id,"state":payment})
            self.release(conn,claim)
            return True

    async def execute(self,claim):
        try:
            observed = await self.recover(claim)
            if isinstance(observed, UnavailableAuthorization):
                self.authorization_unavailable(claim, observed.resource)
                return
            if observed:
                self.finish(claim,observed)
                return
            attempt = self.prepare_dispatch(claim)
            if not attempt:
                return
            # Last local check cannot prevent suspension after this point; never assume it can.
            with self.db.tx() as conn:
                if not self.fenced(conn,claim):
                    return
            o = claim.op
            response = await self.paypal.post(o["endpoint"],o["payload"],str(o["request_id"]),deadline=o["retry_deadline"])
            if o["kind"] == "AUTHORIZE":
                if response.get("id") != claim.deal["order_id"]:
                    raise Conflict("Authorization response order ID mismatch")
                validate_order(response,claim.deal,require_payer=True)
                auths = response["purchase_units"][0].get("payments",{}).get("authorizations",[])
                if len(auths) != 1:
                    raise Conflict("Missing or multiple authorizations")
                response = await self.paypal.get(resource_path("authorization",auths[0]["id"]))
                if response.get("id") != auths[0]["id"]:
                    raise Conflict("Authorization resource ID mismatch")
            elif o["kind"] == "VOID":
                response = await self.paypal.get(resource_path("authorization",claim.deal["authorization_id"]))
            self.finish(claim,response,attempt)
        except ProviderError as exc:
            # Even 4xx may report already-captured/conflicting requests; reconcile before conclusions.
            state = "RECONCILING" if claim.op["first_dispatch_at"] else "QUEUED"
            if exc.status in (400,403,404,422) and not claim.op["first_dispatch_at"]:
                state = "MANUAL_REVIEW"
            self.defer(claim,state,exc.name,backoff(claim.op["failures"]+1,exc.retry_after))
        except (Conflict,KeyError,ValueError,TypeError) as exc:
            self.defer(claim,"MANUAL_REVIEW",str(exc))

    async def run_once(self):
        claim = self.claim()
        if not claim:
            return False
        await self.execute(claim)
        return True

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

    def expire_fulfillment(self):
        with self.db.tx() as conn:
            ids = conn.execute("""SELECT id FROM deals WHERE capture_deadline<=%s
                AND NOT cancel_requested AND capture_id IS NULL LIMIT 20""",(self.clock(),)).fetchall()
        for d in ids:
            Coordinator(self.db,self.clock).request_void(d["id"])


async def main():
    from .config import settings
    config = settings()
    db, paypal = DB(config.database_url), PayPal(config)
    worker = Worker(db,paypal)
    try:
        while True:
            try:
                worker.wake_webhooks()
                worker.expire_fulfillment()
            except Exception as exc:
                print(f"maintenance failed: {type(exc).__name__}",flush=True)
            try:
                worked = await worker.run_once()
            except Exception as exc:
                # A crash leaves the durable lease for reconciliation. Never log credentials/bodies.
                print(f"payment iteration failed: {type(exc).__name__}",flush=True)
                worked = False
            if not worked:
                await asyncio.sleep(1)
    finally:
        await paypal.close()


if __name__ == "__main__":
    asyncio.run(main())
