"""All application mutations acquire the deal row first; no locks span HTTP calls."""
from datetime import timedelta

from psycopg.types.json import Jsonb

from .db import audit, canonical, digest, uid, utcnow
from .paypal import resource_path
from .contract import TERMS_VERSION, MANDATE


class Conflict(Exception):
    pass


def lock_deal(conn, identity):
    row = conn.execute("SELECT * FROM deals WHERE id=%s FOR UPDATE", (identity,)).fetchone()
    if not row:
        raise Conflict("Unknown deal")
    return row


def operation(conn, deal_id, kind):
    return conn.execute("SELECT * FROM operations WHERE deal_id=%s AND kind=%s", (deal_id, kind)).fetchone()


def insert_operation(conn, deal, kind, endpoint, payload, decision_id=None):
    existing = operation(conn, deal["id"], kind)
    if existing:
        return existing
    text = canonical(payload)
    row = conn.execute("""INSERT INTO operations
        (id,deal_id,kind,endpoint,payload,payload_hash,request_id,decision_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (uid(), deal["id"], kind, endpoint, text, digest(text), uid(), decision_id)).fetchone()
    audit(conn, deal["id"], "OPERATION_CREATED", {"kind": kind, "operation_id": str(row["id"])})
    return row


class Coordinator:
    def __init__(self, db, clock=utcnow):
        self.db, self.clock = db, clock

    def approve(self, deal_id, actor, role, policy_hash):
        with self.db.tx() as conn:
            deal = lock_deal(conn, deal_id)
            if role not in ("buyer", "supplier") or str(deal[f"{role}_id"]) != str(actor):
                raise Conflict("Actor cannot approve this deal")
            if policy_hash != deal["policy_hash"]:
                raise Conflict("Requirement version changed")
            if deal["policy"].get("terms_version") != TERMS_VERSION or deal["policy"].get("mandate") != MANDATE:
                raise Conflict("Unsupported capture mandate")
            column = "buyer_approved" if role == "buyer" else "supplier_approved"
            conn.execute(f"UPDATE deals SET {column}=true WHERE id=%s", (deal_id,))
            audit(conn, deal_id, "POLICY_APPROVED", {"role": role, "policy_hash": policy_hash,
                "terms_version": deal["policy"]["terms_version"]}, actor)

    def queue_create(self, deal_id):
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            old = operation(conn, deal_id, "CREATE")
            if old:
                return old
            if not (d["buyer_approved"] and d["supplier_approved"]) or d["cancel_requested"]:
                raise Conflict("Both approvals are required")
            return insert_operation(conn, d, "CREATE", "/v2/checkout/orders", {
                "intent": "AUTHORIZE", "purchase_units": [{"reference_id": str(d["id"]),
                "custom_id": str(d["id"]), "amount": {"currency_code": "USD", "value": "750.00"},
                "payee": {"merchant_id": d["merchant_id"]}}]})

    def queue_authorize(self, deal_id):
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            old = operation(conn, deal_id, "AUTHORIZE")
            if old:
                return old
            if not d["order_id"] or d["cancel_requested"]:
                raise Conflict("Order not available for authorization")
            return insert_operation(conn, d, "AUTHORIZE", resource_path("order", d["order_id"]) + "/authorize", {})

    def queue_capture(self, deal_id):
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            old = operation(conn, deal_id, "CAPTURE")
            if old and old["first_dispatch_at"]:
                return old
            if (not d["authorization_id"] or d["cancel_requested"] or
                    d["payment_state"] not in ("AUTHORIZED", "CAPTURE_QUEUED") or
                    not d["capture_deadline"] or self.clock() >= d["capture_deadline"]):
                raise Conflict("Capture is not eligible")
            decision = conn.execute("""SELECT * FROM decisions WHERE deal_id=%s AND
                evidence_generation=%s AND policy_hash=%s ORDER BY created_at DESC,id DESC LIMIT 1""",
                (deal_id, d["evidence_generation"], d["policy_hash"])).fetchone()
            if not decision or decision["outcome"] != "PASS":
                raise Conflict("Current evidence has not passed")
            if old:
                conn.execute("UPDATE operations SET decision_id=%s,state='QUEUED',next_attempt_at=%s WHERE id=%s",
                             (decision["id"], self.clock(), old["id"]))
                audit(conn, deal_id, "CAPTURE_DECISION_REBOUND", {"old": str(old["decision_id"]),
                    "new": str(decision["id"]), "operation_id": str(old["id"])})
                return operation(conn, deal_id, "CAPTURE")
            return insert_operation(conn, d, "CAPTURE", resource_path("authorization", d["authorization_id"]) + "/capture",
                {"amount": {"currency_code": "USD", "value": "750.00"}, "final_capture": True}, decision["id"])

    def request_void(self, deal_id):
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            conn.execute("UPDATE deals SET cancel_requested=true WHERE id=%s", (deal_id,))
            cap = operation(conn, deal_id, "CAPTURE")
            if cap and cap["first_dispatch_at"] is None:
                conn.execute("UPDATE operations SET state='BLOCKED' WHERE id=%s", (cap["id"],))
            audit(conn, deal_id, "CANCELLATION_REQUESTED", {})
            if not d["authorization_id"]:
                # An unknown authorization must first be recovered through its known order.
                return None
            return insert_operation(conn, d, "VOID", resource_path("authorization", d["authorization_id"]) + "/void", {})

    def invalidate_evidence(self, conn, d, actor, reason):
        conn.execute("UPDATE deals SET evidence_generation=evidence_generation+1 WHERE id=%s", (d["id"],))
        conn.execute("""UPDATE operations SET state='BLOCKED' WHERE deal_id=%s AND kind='CAPTURE'
                        AND first_dispatch_at IS NULL""", (d["id"],))
        audit(conn, d["id"], "EVIDENCE_INVALIDATED", {"reason": reason}, actor)

    def add_document(self, deal_id, actor, role, kind, body):
        if len(body) > 5_242_880 or not body.startswith(b"%PDF-"):
            raise Conflict("Expected a PDF of at most 5 MB")
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            expected = "supplier" if kind == "certificate" else "buyer"
            if role != expected or str(d[f"{expected}_id"]) != str(actor):
                raise Conflict("Wrong document uploader")
            if conn.execute("SELECT count(*) AS n FROM documents WHERE deal_id=%s",(deal_id,)).fetchone()["n"] >= 32:
                raise Conflict("The bounded MVP allows at most 32 documents per deal")
            version = conn.execute("SELECT coalesce(max(version),0)+1 AS v FROM documents WHERE deal_id=%s", (deal_id,)).fetchone()["v"]
            identity = uid()
            conn.execute("""INSERT INTO documents(id,deal_id,version,kind,uploader_id,sha256,body)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""", (identity,deal_id,version,kind,actor,digest(body),body))
            self.invalidate_evidence(conn, d, actor, "document uploaded")
            return identity

    def acknowledge_delivery(self, deal_id, actor):
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            if str(d["buyer_id"]) != str(actor):
                raise Conflict("Buyer required")
            conn.execute("UPDATE deals SET delivery_ack=true WHERE id=%s", (deal_id,))
            self.invalidate_evidence(conn, d, actor, "buyer acknowledged delivery")

    def supersede(self, deal_id, actor, old_id, new_id):
        with self.db.tx() as conn:
            d = lock_deal(conn, deal_id)
            if str(d["buyer_id"]) != str(actor):
                raise Conflict("Buyer review required")
            rows = conn.execute("SELECT * FROM documents WHERE deal_id=%s AND id IN (%s,%s)",
                                (deal_id, old_id, new_id)).fetchall()
            if len(rows) != 2 or rows[0]["kind"] != rows[1]["kind"]:
                raise Conflict("Replacement must be a document of the same kind in this deal")
            new = next(r for r in rows if str(r["id"]) == new_id)
            if not new["active"]:
                raise Conflict("Replacement is inactive")
            conn.execute("UPDATE documents SET active=false WHERE id=%s", (old_id,))
            self.invalidate_evidence(conn, d, actor, "buyer reviewed certificate replacement")
            audit(conn, deal_id, "DOCUMENT_SUPERSEDED", {"old": old_id, "new": new_id}, actor)
