"""Submit repeats through a real authenticated supplier session after confirmed capture."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
import httpx
from accord.config import Settings
from accord.db import DB,utcnow


async def main():
    parser=argparse.ArgumentParser();parser.add_argument("--deal-id",required=True)
    args=parser.parse_args();load_dotenv();config=Settings();db=DB(config.database_url)
    out=Path("acceptance-record/paypal");out.mkdir(parents=True,exist_ok=True)
    report={"timestamp":utcnow().isoformat(),"deal_id":args.deal_id,"status":"INCOMPLETE"}
    try:
        with db.tx() as conn:
            op=conn.execute("""SELECT o.id FROM operations o JOIN deals d ON d.id=o.deal_id
                WHERE d.id=%s AND d.payment_state='CAPTURED' AND o.kind='CAPTURE' AND o.state='SUCCEEDED'
                AND o.provider_id=d.capture_id""",(args.deal_id,)).fetchone()
        if not op:raise ValueError("Run only after one capture is confirmed")
        async with httpx.AsyncClient(base_url=config.app_origin,timeout=30,follow_redirects=False) as client:
            result=await client.post("/api/login",headers={"Origin":config.app_origin},json={
                "username":os.environ.get("ACCORD_SUPPLIER_USERNAME","supplier"),"password":os.environ["ACCORD_SUPPLIER_PASSWORD"]})
            result.raise_for_status();headers={"Origin":config.app_origin,"X-CSRF-Token":result.json()["csrf_token"]}
            if result.json()["role"]!="supplier":raise ValueError("Supplier session required")
            path=f"/api/deals/{args.deal_id}/capture"
            sequential=[await client.post(path,headers=headers) for _ in range(2)]
            concurrent=await asyncio.gather(*(client.post(path,headers=headers) for _ in range(4)))
            responses=sequential+concurrent
            for response in responses:
                response.raise_for_status()
                if response.json()["id"]!=str(op["id"]):raise ValueError("Operation ID changed")
            report.update(status="PASS",operation_id=str(op["id"]),sequential_count=2,concurrent_count=4,
                responses=[{"http_status":r.status_code,"operation":r.json()} for r in responses])
            await client.post("/api/logout",headers=headers)
    except Exception as exc:report.update(status="BLOCKED",error=type(exc).__name__)
    (out/"duplicate-result.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"status":report["status"],"record":str(out/"duplicate-result.json")}))
    return 0 if report["status"]=="PASS" else 2


if __name__=="__main__":raise SystemExit(asyncio.run(main()))
