"""Kill a real worker process after simulated provider completion, then restart it.

Real PostgreSQL and elapsed 90-second lease; the provider is explicitly a double.
Never run against the application database. No real provider credentials are loaded.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy import URL

from accord.coordinator import Coordinator
from accord.contract import policy
from accord.db import DB,canonical,digest,uid,utcnow
from accord.worker import Worker
from tests.fake_paypal import FakePayPal
from tests.conftest import passing_decision


def child(mode,path):
    state=json.loads(path.read_text());db=DB(os.environ["RECOVERY_DATABASE_URL"])
    with db.tx() as conn:deal=conn.execute("SELECT * FROM deals WHERE id=%s",(state["deal_id"],)).fetchone()
    provider=FakePayPal(deal,utcnow)
    for key in ("order_value","auth","capture"):setattr(provider,key,state[key])
    worker=Worker(db,provider)
    if mode=="crash":
        claim=worker.claim();assert claim and claim.op["kind"]=="CAPTURE"
        assert worker.prepare_dispatch(claim)
        asyncio.run(provider.post(claim.op["endpoint"],claim.op["payload"],str(claim.op["request_id"])))
        state.update(capture=provider.capture,calls=provider.calls)
        path.write_text(json.dumps(state),encoding="utf-8")
        os._exit(23)  # No finish(), release(), finally blocks, or connection cache shared with parent.
    asyncio.run(worker.run_once())
    state["restarted_calls"]=provider.calls;path.write_text(json.dumps(state),encoding="utf-8")


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="acceptance-record/process-recovery")
    parser.add_argument("--child",choices=("crash","recover"));parser.add_argument("--snapshot")
    args=parser.parse_args()
    if args.child:child(args.child,Path(args.snapshot));return 0
    base=os.environ["TEST_DATABASE_URL"];parts=psycopg.conninfo.conninfo_to_dict(base)
    if not parts.get("dbname","").startswith("accord_test"):raise SystemExit("Disposable accord_test database required")
    name="accord_test_recovery_"+uid()[:8]
    with psycopg.connect(base,autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts["dbname"]=name;url=psycopg.conninfo.make_conninfo(**parts)
    os.environ["DATABASE_URL"]=URL.create("postgresql+psycopg",username=parts.get("user"),password=parts.get("password"),
        host=parts.get("host"),port=int(parts.get("port",5432)),database=name).render_as_string(hide_password=False)
    command.upgrade(Config("alembic.ini"),"head")
    db=DB(url);identity,buyer,supplier=uid(),uid(),uid();p=policy()
    with db.tx() as conn:
        for key,role in ((buyer,"buyer"),(supplier,"supplier")):
            conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,%s,'LOGIN-DISABLED',%s)",(key,role,role))
        conn.execute("""INSERT INTO deals(id,buyer_id,supplier_id,merchant_id,payer_id,policy,policy_hash,
            buyer_approved,supplier_approved,delivery_ack) VALUES(%s,%s,%s,'MERCHANT1','BUYER1',%s,%s,true,true,true)""",
            (identity,buyer,supplier,Jsonb(p),digest(canonical(p))))
        deal=conn.execute("SELECT * FROM deals WHERE id=%s",(identity,)).fetchone()
    c=Coordinator(db);provider=FakePayPal(deal,utcnow);worker=Worker(db,provider)
    c.queue_create(identity);asyncio.run(worker.run_once());c.queue_authorize(identity);asyncio.run(worker.run_once())
    passing_decision(db,identity);op=c.queue_capture(identity)
    out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True);snapshot=out/"provider-double.json"
    snapshot.write_text(json.dumps({"deal_id":identity,"order_value":provider.order_value,"auth":provider.auth,"capture":None}),encoding="utf-8")
    report={"timestamp":utcnow().isoformat(),"platform":platform.platform(),"database_name":name,"provider":"test_double","status":"RUNNING"}
    child_env={k:v for k,v in os.environ.items() if k in ("SYSTEMROOT","WINDIR","PATH","TEMP","TMP","HOME")}
    child_env["RECOVERY_DATABASE_URL"]=url
    try:
        crashed=subprocess.run([sys.executable,"-m","acceptance.process_recovery","--child","crash","--snapshot",str(snapshot)],env=child_env,capture_output=True,timeout=30)
        assert crashed.returncode==23,"Crash probe failed before deliberate exit"
        with db.tx() as conn:
            d=conn.execute("SELECT * FROM deals WHERE id=%s",(identity,)).fetchone()
        assert d["capture_id"] is None and d["active_operation"]==op["id"]
        assert not worker.claim(),"Another worker stole a live lease"
        wait=max(0,(d["lease_until"]-utcnow()).total_seconds())+1
        print(json.dumps({"state":"WAITING_FOR_REAL_LEASE_EXPIRY","seconds":round(wait,1)}),flush=True)
        started=time.monotonic()
        while utcnow()<=d["lease_until"]:time.sleep(.5)
        recovered=subprocess.run([sys.executable,"-m","acceptance.process_recovery","--child","recover","--snapshot",str(snapshot)],env=child_env,capture_output=True,timeout=30)
        assert recovered.returncode==0,"Replacement worker failed"
        with db.tx() as conn:
            d=conn.execute("SELECT * FROM deals WHERE id=%s",(identity,)).fetchone()
            current=conn.execute("SELECT * FROM operations WHERE id=%s",(op["id"],)).fetchone()
        observed=json.loads(snapshot.read_text())
        assert d["payment_state"]=="CAPTURED" and d["capture_id"]==observed["capture"]["id"]
        assert current["request_id"]==op["request_id"] and current["payload_hash"]==op["payload_hash"]
        assert not any(call[0]=="POST" for call in observed["restarted_calls"])
        report.update(status="PASS_WITH_TEST_DOUBLE",crash_exit_code=crashed.returncode,recovery_exit_code=recovered.returncode,
            lease_wait_seconds=round(time.monotonic()-started,2),operation_id=str(op["id"]),request_id=str(op["request_id"]),
            payment_state=d["payment_state"],capture_id=d["capture_id"],replacement_worker_posts=0)
    except Exception as exc:report.update(status="FAIL",error=type(exc).__name__+": "+str(exc))
    (out/"process-result.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report));return 0 if report["status"]=="PASS_WITH_TEST_DOUBLE" else 1


if __name__=="__main__":raise SystemExit(main())
