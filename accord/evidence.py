"""Bounded semantics. Model labels are proposals; only this module admits evidence."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from .coordinator import Conflict, lock_deal
from .db import audit, uid

SCHEMA_VERSION = "finding-v1"
PROMPT_VERSION = "extract-v1"
RULE_VERSION = "bounded-layouts-v2"

CERTIFICATE_ASSOCIATIONS = (
    r"PO: ([A-Z0-9-]+)\nLot: ([A-Z0-9-]+)(?:\nRequired grade: [0-9]{3}[A-Z]?)?\nDelivered grade: ([0-9]{3}[A-Z]?)",
    r"([A-Z0-9-]+) \| ([A-Z0-9-]+) \| ([0-9]{3}[A-Z]?)",
    r"For purchase order ([A-Z0-9-]+), lot ([A-Z0-9-]+), the delivered material grade is ([0-9]{3}[A-Z]?)\.",
)
RECEIPT_ASSOCIATIONS = (r"PO: ([A-Z0-9-]+)\nLot: ([A-Z0-9-]+)\nReceived: (YES|NO)",)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    document_version: int
    field: Literal["delivered_grade", "delivery_received"]
    purchase_order: str
    lot: str
    value: str
    span_ids: list[str] = Field(min_length=1, max_length=20)
    quote: str


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["found", "missing", "conflicting", "ambiguous"]
    findings: list[Finding] = Field(max_length=20)


def cited_text(document, finding):
    if str(document["id"]) != finding.document_id or document["version"] != finding.document_version:
        raise Conflict("Document-version binding mismatch")
    spans = {s["id"]: s["text"] for s in document["parsed"]["spans"]}
    if len(set(finding.span_ids)) != len(finding.span_ids) or any(i not in spans for i in finding.span_ids):
        raise Conflict("Citation references invalid spans")
    text = "\n".join(spans[i] for i in finding.span_ids)
    if text != finding.quote:
        raise Conflict("Citation does not match parser output exactly")
    return text


def association_rows(document):
    text = "\n".join(s["text"] for s in document["parsed"]["spans"])
    patterns = CERTIFICATE_ASSOCIATIONS if document["kind"] == "certificate" else RECEIPT_ASSOCIATIONS
    return [(m.group(1), m.group(2), m.group(3), m.group(0))
            for pattern in patterns for m in re.finditer(pattern, text)]


def check_reviewed_value(document, finding):
    """Human review may resolve unknown prose, but cannot rewrite a known field."""
    for po, lot, value, _ in association_rows(document):
        if (po, lot) == (finding.purchase_order, finding.lot) and value != finding.value:
            raise Conflict("Review contradicts recognized delivered value; replace or supersede the evidence")


def admissible(document, finding, policy):
    """Three complete field/PO/lot associations; unknown semantics fail closed."""
    try:
        quote = cited_text(document,finding)
    except (Conflict,KeyError,TypeError):
        return False,"invalid citation binding"
    po,lot = policy["purchase_order"],policy["lot"]
    expected_field = "delivered_grade" if document["kind"] == "certificate" else "delivery_received"
    if finding.field != expected_field or finding.purchase_order != po or finding.lot != lot:
        return False,"field meaning or PO/lot mismatch"
    all_text = "\n".join(s["text"] for s in document["parsed"]["spans"])
    if re.search(r"\b(not|never|except|unless|isn't|isn’t|rejected|ignore|instructions?|system|assistant|override|capture)\b",all_text,re.I):
        return False,"negation, exception, or embedded instructions require buyer review"
    # A finding cannot cherry-pick a row: all supported associations are examined.
    rows = association_rows(document)
    if document["kind"] == "certificate":
        grades = set(re.findall(r"(?<![\w-])[0-9]{3}[A-Z]?(?![\w-])",all_text))
        if len(grades) != 1:
            return False,"competing or missing grade values"
    if len(rows) != 1:
        return False,"unresolved row association or unsupported layout"
    rpo,rlot,value,passage = rows[0]
    if (rpo,rlot,value) != (po,lot,finding.value) or passage not in quote:
        return False,"cited value does not describe delivery for the approved PO and lot"
    # The whole document must fit the supported grammar. Do not silently ignore a
    # contradictory paragraph simply because its language is outside our patterns.
    remaining = all_text.replace(passage,"",1)
    allowed = {"ACCORD MATERIAL CERTIFICATE v1","ACCORD DELIVERY RECEIPT v1",
               "PO | Lot | Delivered grade"}
    for line in remaining.splitlines():
        line = line.strip()
        if line and line not in allowed and not re.fullmatch(r"Required grade: [0-9]{3}[A-Z]?",line):
            return False,"unparsed prose requires buyer interpretation review"
    # Reject foreign identifier mentions, including rows not understood by the grammar.
    for identifier in re.findall(r"\b(?:PO|LOT)-[A-Z0-9-]+\b",all_text):
        if identifier not in (po,lot):
            return False,"mixed purchase orders or lots"
    return True,"bounded semantic association verified"


def evaluate(db,deal_id):
    with db.tx() as conn:
        d = lock_deal(conn,deal_id)
        docs = conn.execute("SELECT * FROM documents WHERE deal_id=%s AND active ORDER BY version",(deal_id,)).fetchall()
        reasons,accepted = [],[]
        if not (d["buyer_approved"] and d["supplier_approved"] and d["delivery_ack"]):
            reasons.append("approvals or buyer delivery acknowledgment missing")
        for kind in ("certificate","receipt"):
            selected = [x for x in docs if x["kind"] == kind]
            if len(selected) != 1:
                reasons.append(f"exactly one active {kind} required; supersession needs buyer review")
                continue
            doc = selected[0]
            # Reviews bind an immutable document version and frozen deal policy. A
            # second document's review must not invalidate the first interpretation.
            review = conn.execute("""SELECT * FROM reviews WHERE document_id=%s
                AND reviewer_id=%s ORDER BY created_at DESC LIMIT 1""",(doc["id"],d["buyer_id"])).fetchone()
            human = bool(review)
            try:
                if human:
                    finding = Finding.model_validate(review["interpretation"])
                    cited_text(doc,finding)
                    check_reviewed_value(doc, finding)
                    ok,reason = True,"buyer reviewed interpretation"
                else:
                    extraction = doc.get("extraction") or {}
                    versions = extraction.get("versions",{})
                    if not all(versions.get(k) for k in ("parser","schema","prompt","model")):
                        raise ValueError("extraction version metadata missing")
                    proposal = Proposal.model_validate(extraction["proposal"])
                    if len(proposal.findings) != 1:
                        raise ValueError("one unambiguous finding required")
                    finding = proposal.findings[0]
                    # The model's status is deliberately not used as an admission decision.
                    ok,reason = admissible(doc,finding,d["policy"])
                expected = d["policy"]["material_grade"] if kind == "certificate" else "YES"
                expected_field = "delivered_grade" if kind == "certificate" else "delivery_received"
                if (finding.value != expected or finding.field != expected_field or
                    finding.purchase_order != d["policy"]["purchase_order"] or finding.lot != d["policy"]["lot"]):
                    ok,reason = False,"finding contradicts frozen policy"
                if not ok:
                    reasons.append(reason)
                accepted.append({"document_id":str(doc["id"]),"version":doc["version"],"sha256":doc["sha256"],
                    "finding":finding.model_dump(),"admissible":ok,"reason":reason,
                    "review_id":str(review["id"]) if human else None,"extraction":doc.get("extraction")})
            except (ValueError,KeyError,Conflict,TypeError) as exc:
                reasons.append(str(exc))
        outcome = "REVIEW_REQUIRED" if reasons else "PASS"
        identity = uid()
        conn.execute("""INSERT INTO decisions(id,deal_id,evidence_generation,policy_hash,outcome,bundle)
            VALUES(%s,%s,%s,%s,%s,%s)""",(identity,deal_id,d["evidence_generation"],d["policy_hash"],outcome,
            Jsonb({"rule_version":RULE_VERSION,"findings":accepted,"reasons":reasons})))
        audit(conn,deal_id,"EVIDENCE_EVALUATED",{"decision_id":identity,"outcome":outcome})
        return {"id":identity,"outcome":outcome,"reasons":reasons}


def review_interpretation(db,deal_id,actor,finding,reason):
    if len(reason.strip()) < 10:
        raise Conflict("Explain the reviewed interpretation")
    with db.tx() as conn:
        d = lock_deal(conn,deal_id)
        if str(d["buyer_id"]) != str(actor):
            raise Conflict("Buyer required")
        doc = conn.execute("SELECT * FROM documents WHERE id=%s AND deal_id=%s AND active",
                           (finding.document_id,deal_id)).fetchone()
        if not doc or not doc["parsed"]:
            raise Conflict("Parsed active document required")
        quote = cited_text(doc,finding)
        check_reviewed_value(doc, finding)
        if any(v not in quote for v in (finding.purchase_order,finding.lot,finding.value)):
            raise Conflict("Reviewed quote must include PO, lot and proposed value")
        if finding.purchase_order != d["policy"]["purchase_order"] or finding.lot != d["policy"]["lot"]:
            raise Conflict("Review cannot change the approved PO/lot")
        from .coordinator import Coordinator
        Coordinator(db).invalidate_evidence(conn,d,actor,"buyer interpretation changed")
        conn.execute("""INSERT INTO reviews(id,deal_id,document_id,evidence_generation,reviewer_id,interpretation,reason)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",(uid(),deal_id,doc["id"],d["evidence_generation"]+1,actor,Jsonb(finding.model_dump()),reason))
        # Review is version-scoped, append-only, and requires another backend evaluation.
        conn.execute("UPDATE operations SET state='BLOCKED' WHERE deal_id=%s AND kind='CAPTURE' AND first_dispatch_at IS NULL",(deal_id,))
        audit(conn,deal_id,"BUYER_INTERPRETATION_REVIEWED",{"document_id":finding.document_id,"reason":reason},actor)
