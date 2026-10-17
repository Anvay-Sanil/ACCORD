"""Read actual Sandbox resources and reverify retained real webhook signatures.

This command never creates, authorizes, captures, voids, or refunds a payment.
It cannot certify the gate from database state, simulator events, or test doubles.
"""
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path

from accord.config import Settings
from accord.db import DB,canonical,digest,utcnow
from accord.paypal import PayPal,ProviderError,event_resource_ids,resource_path
from accord.worker import validate_order,check_amount,parse_time


def validate_resources(deal,order,authorization,capture):
    unit=validate_order(order,deal,require_payer=True)
    for kind,resource in (("order",order),("authorization",authorization),("capture",capture)):
        if resource["id"]!=deal[kind+"_id"]:raise ValueError("Resource ID mismatch")
    for resource in (authorization,capture):check_amount(resource)
    payments=unit.get("payments",{})
    if {x["id"] for x in payments.get("authorizations",[])}!={authorization["id"]}:
        raise ValueError("Order authorization association is not singular")
    if {x["id"] for x in payments.get("captures",[])}!={capture["id"]}:
        raise ValueError("Order capture association is not singular")
    if capture["status"]!="COMPLETED":raise ValueError("Capture is not currently completed")
    if deal["authorized_at"]!=parse_time(authorization["create_time"]):
        raise ValueError("Local authorization timestamp differs from provider creation time")
    if deal["capture_deadline"]!=parse_time(authorization["create_time"])+timedelta(hours=48):
        raise ValueError("Capture deadline is not based on provider creation time")


def operation_checks(operations,dispatches,events,deal):
    captures=[o for o in operations if o["kind"]=="CAPTURE"]
    if len(captures)!=1:return {"single_capture_operation":False}
    op=captures[0];attempts=[d for d in dispatches if d["operation_id"]==op["id"]]
    return {
        "single_capture_operation":True,
        "frozen_key_and_payload":bool(attempts) and all(
            d["request_id"]==op["request_id"] and d["payload_hash"]==op["payload_hash"] for d in attempts),
        "capture_within_deadline":bool(op["first_dispatch_at"] and op["first_dispatch_at"]<=deal["capture_deadline"]),
        "duplicate_submissions_audited":sum(e["event"]=="CAPTURE_REPEAT_SUBMISSION" for e in events)>=2,
        "uncertain_response_recorded":any(e["event"]=="OPERATION_DEFERRED" and e["data"].get("kind")=="CAPTURE"
            and e["data"].get("state")=="RECONCILING" and e["data"].get("reason") in
            ("LOCAL_FAULT_RESPONSE_DROPPED","TRANSPORT_UNCERTAIN") for e in events),
        "recovery_confirmed":op["state"]=="SUCCEEDED" and op["provider_id"]==deal["capture_id"],
    }


async def main():
    parser=argparse.ArgumentParser();parser.add_argument("--deal-id");parser.add_argument("--output",default="acceptance-record/paypal")
    args=parser.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    report={"gate":"real_paypal_sandbox","timestamp":utcnow().isoformat(),"status":"INCOMPLETE","checks":{}}
    config=Settings();db=DB(config.database_url);client=PayPal(config)
    try:
        bootstrap=Path("acceptance-record/bootstrap.json")
        identity=args.deal_id or (json.loads(bootstrap.read_text())["deal_id"] if bootstrap.exists() else None)
        if not identity:
            report.update(status="BLOCKED",reason="No bootstrapped acceptance deal; complete buyer configuration and bootstrap first.")
            return 2
        with db.tx() as conn:
            deal=conn.execute("SELECT * FROM deals WHERE id=%s",(identity,)).fetchone()
            if not deal:raise ValueError("Acceptance deal does not exist")
            operations=conn.execute("SELECT * FROM operations WHERE deal_id=%s ORDER BY created_at",(identity,)).fetchall()
            dispatches=conn.execute("SELECT d.* FROM dispatches d JOIN operations o ON o.id=d.operation_id WHERE o.deal_id=%s",(identity,)).fetchall()
            events=conn.execute("SELECT event,data,created_at FROM audit WHERE deal_id=%s ORDER BY created_at",(identity,)).fetchall()
            hooks=conn.execute("SELECT * FROM webhook_inbox WHERE verification_method='paypal_postback' ORDER BY verified_at").fetchall()
        report.update(deal_id=identity,payment_state=deal["payment_state"])
        report["checks"]["both_application_approvals"]=bool(deal["buyer_approved"] and deal["supplier_approved"])
        report["checks"]["supplier_merchant_matches_configuration"]=deal["merchant_id"]==config.paypal_merchant_id
        if not all(deal[k+"_id"] for k in ("order","authorization","capture")):
            report["reason"]="Real buyer approval, authorization and capture are not yet all recorded."
            return 2
        resources={}
        for kind in ("order","authorization","capture"):
            resources[kind]=await client.get(resource_path(kind,deal[kind+"_id"]))
        validate_resources(deal,resources["order"],resources["authorization"],resources["capture"])
        report["checks"]["real_resources_validated"]=True
        report["resources"]={kind:{"id":r["id"],"status":r["status"],"create_time":r.get("create_time"),
            "amount":r.get("amount"),"response_sha256":digest(canonical(r))} for kind,r in resources.items()}
        report["checks"].update(operation_checks(operations,dispatches,events,deal))
        verified=[]
        for hook in hooks:
            ids=event_resource_ids(hook["event"])
            if hook["event"]["event_type"]!="PAYMENT.CAPTURE.COMPLETED" or ids["capture"]!=deal["capture_id"]:continue
            valid=await client.verify(bytes(hook["raw_body"]),hook["verification_headers"] or {})
            verified.append({"event_id":hook["event_id"],"type":hook["event"]["event_type"],"raw_sha256":digest(bytes(hook["raw_body"])),
                "verified_at":hook["verified_at"],"processed_at":hook["processed_at"],"postback_reverification":valid})
        report["webhooks"]=verified
        report["checks"]["real_completed_webhook_verified_and_processed"]=any(v["postback_reverification"] and v["processed_at"] for v in verified)
        duplicate_file=out/"duplicate-result.json"
        duplicate=json.loads(duplicate_file.read_text()) if duplicate_file.exists() else {}
        report["checks"]["concurrent_authenticated_submissions_recorded"]=(duplicate.get("status")=="PASS" and duplicate.get("deal_id")==identity
            and duplicate.get("operation_id")==str(next(o["id"] for o in operations if o["kind"]=="CAPTURE")))
        report["operations"]=[{k:o[k] for k in ("id","kind","state","request_id","payload_hash","provider_id","first_dispatch_at","retry_deadline","decision_id")} for o in operations]
        report["dispatches"]=dispatches;report["audit"]=events
        report["status"]="PASS" if all(report["checks"].values()) else "INCOMPLETE"
        return 0 if report["status"]=="PASS" else 2
    except ProviderError as exc:
        report.update(status="BLOCKED",provider_status=exc.status,provider_error=exc.name);return 2
    except Exception as exc:
        report.update(status="FAIL",error=type(exc).__name__);return 1
    finally:
        await client.close()
        (out/"paypal-result.json").write_text(json.dumps(report,indent=2,default=str),encoding="utf-8")
        print(json.dumps({"status":report["status"],"checks":report["checks"],"record":str(out/"paypal-result.json")}))


if __name__=="__main__":raise SystemExit(asyncio.run(main()))
