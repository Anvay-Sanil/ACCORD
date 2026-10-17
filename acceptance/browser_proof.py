"""Real Chromium + real PostgreSQL + real PDF parser. Explicit provider/model doubles.

This script is a test harness only; no fake mode or fault routes enter the public app.
It never resets a database: each run creates distinct users and one disposable deal.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import threading
import time

from argon2 import PasswordHasher
from psycopg.types.json import Jsonb
from playwright.sync_api import sync_playwright,expect
import uvicorn
import psycopg
from psycopg import sql
from alembic import command
from alembic.config import Config

from accord.api import create_app
from accord.config import Settings
from accord.contract import policy
from accord.db import DB,canonical,digest,uid,utcnow
from accord.evidence import association_rows
from accord import extraction_worker
from accord.worker import Worker
from tests.fake_paypal import FakePayPal
from acceptance.fixtures import write_fixtures


async def fixture_propose(config,document,parsed):
    doc={**document,"parsed":parsed};rows=association_rows(doc)
    findings=[]
    if len(rows)==1:
        po,lot,value,_=rows[0]
        findings=[{"document_id":str(doc["id"]),"document_version":doc["version"],"field":"delivered_grade" if doc["kind"]=="certificate" else "delivery_received",
                   "purchase_order":po,"lot":lot,"value":value,"span_ids":[s["id"] for s in parsed["spans"]],"quote":"\n".join(s["text"] for s in parsed["spans"])}]
    return {"proposal":{"status":"found" if findings else "ambiguous","findings":findings},
            "versions":{"parser":parsed["parser"],"schema":"finding-v1","prompt":"TEST-DOUBLE","model":"TEST-DOUBLE"},"response_id":"TEST-DOUBLE"}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",default="acceptance-record/browser");parser.add_argument("--port",type=int,default=8765)
    args=parser.parse_args();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    url=os.environ["TEST_DATABASE_URL"]
    if not psycopg.conninfo.conninfo_to_dict(url).get("dbname","").startswith("accord_test"):
        raise SystemExit("A disposable accord_test database is required")
    identity,buyer,supplier=uid(),uid(),uid();suffix=identity[:8];password=secrets.token_urlsafe(18)
    # Never let an acceptance worker pick up another run's unfinished operations.
    database_name="accord_test_browser_"+suffix
    with psycopg.connect(url,autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    parts=psycopg.conninfo.conninfo_to_dict(url);parts["dbname"]=database_name
    url=psycopg.conninfo.make_conninfo(**parts)
    # Alembic expects a URL. Keep credentials inside the child process environment.
    from sqlalchemy import URL
    migration_url=URL.create("postgresql+psycopg",username=parts.get("user"),password=parts.get("password"),
        host=parts.get("host"),port=int(parts.get("port",5432)),database=database_name).render_as_string(hide_password=False)
    os.environ["DATABASE_URL"]=migration_url
    command.upgrade(Config("alembic.ini"),"head")
    db=DB(url)
    p=policy();users={"buyer":"buyer-"+suffix,"supplier":"supplier-"+suffix}
    with db.tx() as conn:
        for user_id,role in ((buyer,"buyer"),(supplier,"supplier")):
            conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,%s,%s,%s)",(user_id,users[role],PasswordHasher().hash(password),role))
        conn.execute("""INSERT INTO deals(id,buyer_id,supplier_id,merchant_id,payer_id,policy,policy_hash)
            VALUES(%s,%s,%s,'MERCHANT1','BUYER1',%s,%s)""",(identity,buyer,supplier,Jsonb(p),digest(canonical(p))))
        deal=conn.execute("SELECT * FROM deals WHERE id=%s",(identity,)).fetchone()
    origin=f"http://127.0.0.1:{args.port}"
    config=Settings(_env_file=None,database_url=url,app_origin=origin,session_secure=False,public_deployment=False,
                    fault_drop_capture_response=False,openai_api_key="",paypal_webhook_id="TEST-DOUBLE",paypal_client_id="TEST-DOUBLE",
                    paypal_client_secret="TEST-DOUBLE",paypal_merchant_id="MERCHANT1",provider_label="LOCAL TEST DOUBLES")
    provider=FakePayPal(deal,utcnow,namespace=suffix);worker=Worker(db,provider)
    app=create_app(config,db,provider);stop=threading.Event();recovery_hold=threading.Event();errors=[]
    real_lifespan=app.router.lifespan_context
    extraction_worker.propose=fixture_propose
    async def payments():
        while not stop.is_set():
            try:
                if not recovery_hold.is_set():
                    worker.wake_webhooks();await worker.run_once()
            except Exception as exc:errors.append(type(exc).__name__+": "+str(exc))
            await asyncio.sleep(.1)
    async def extraction():
        while not stop.is_set():
            try:await extraction_worker.once(db,config)
            except Exception as exc:errors.append(type(exc).__name__+": "+str(exc))
            await asyncio.sleep(.1)
    @asynccontextmanager
    async def lifespan(application):
        async with real_lifespan(application):
            tasks=[asyncio.create_task(payments()),asyncio.create_task(extraction())]
            yield
            stop.set()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    app.router.lifespan_context=lifespan
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=args.port,log_level="error"))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.1)
    assert server.started,"Browser server failed to start"
    fixtures=write_fixtures(out/"fixtures")
    sdk='''window.paypal={createInstance:async()=>({findEligibleMethods:async()=>({isEligible:()=>true}),createPayPalOneTimePaymentSession:opts=>({start:async(options,promise)=>{const data=await promise;await opts.onApprove({orderId:data.orderId,payerId:"BUYER1"});}})})};customElements.define("paypal-button",class extends HTMLElement{connectedCallback(){this.setAttribute("role","button");this.setAttribute("tabindex","0");this.textContent="Approve with PayPal (test double)";}});'''
    report={"gate":"browser","provider":"test_double","model":"test_double","database":"PostgreSQL","database_name":database_name,"timestamp":utcnow().isoformat(),"deal_id":identity,"assertions":[]}
    def record(value):report["assertions"].append(value)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            contexts=[browser.new_context(viewport={"width":1440,"height":1100},record_video_dir=str(out/"video")) for _ in range(2)]
            for context in contexts:
                context.route("https://www.sandbox.paypal.com/web-sdk/v6/core",lambda route:route.fulfill(content_type="application/javascript",body=sdk))
            bp,sp=[c.new_page() for c in contexts]
            for page,role in ((bp,"buyer"),(sp,"supplier")):
                page.on("pageerror",lambda exc:errors.append(str(exc)))
                page.goto(origin);page.locator("#username").fill(users[role]);page.locator("#password").fill(password);page.get_by_role("button",name="Sign in",exact=True).click()
                expect(page.locator("#workspace")).to_be_visible();page.locator("#agree-check").check();page.locator("#approve").click()
                expect(page.locator("#approval-note")).to_have_text("Your approval is recorded.")
            bp.locator("#refresh").click();bp.locator("#prepare-checkout").click();expect(bp.locator("#paypal-area")).to_be_visible(timeout=30000)
            bp.locator("#paypal-button").click();expect(bp.locator("#payment-state")).to_have_text("Authorized · not captured",timeout=30000)
            record("Both browser roles approved; SDK callback requested backend authorization of the existing order.")
            sp.locator("#document-file").set_input_files(str(fixtures/"wrong-grade.pdf"));sp.get_by_role("button",name="Upload PDF",exact=True).click()
            bp.locator("#document-file").set_input_files(str(fixtures/"receipt.pdf"));bp.get_by_role("button",name="Upload PDF",exact=True).click()
            bp.locator("#received-check").check();bp.locator("#acknowledge").click()
            expect(bp.locator("#documents")).to_contain_text("delivery_received: YES",timeout=45000)
            expect(bp.locator("#acknowledge")).to_be_disabled()
            ready=bp.evaluate("async()=>api(route('workspace'))")
            assert ready["deal"]["delivery_ack"] and ready["deal"]["buyer_approved"] and ready["deal"]["supplier_approved"]
            assert ready["deal"]["authorization_id"]
            assert {d["kind"] for d in ready["documents"] if d["active"]}=={"certificate","receipt"}
            assert all(d["parse_state"]=="READY" for d in ready["documents"] if d["active"])
            sp.locator("#refresh").click()
            expect(sp.locator("#documents")).to_contain_text("delivered_grade: 304",timeout=45000)
            sp.locator("#evaluate").click();expect(sp.locator("#decision-state")).to_have_text("Review required")
            expect(sp.locator("#capture")).to_be_disabled()
            assert not any(c[0]=="POST" and c[1].endswith("/capture") for c in provider.calls)
            sp.locator(".document-row").filter(has_text="certificate").first.click()
            expect(sp.locator("#passage")).to_contain_text("316L")
            expect(sp.locator("#passage")).to_contain_text("304")
            expect(sp.locator("#finding-summary")).to_contain_text("delivered_grade: 304")
            sp.screenshot(path=str(out/"01-wrong-evidence-blocked.png"),full_page=True)
            record("Both documents are READY, delivery is acknowledged and both mandates approved; cited required 316L / delivered 304 is visible, capture is disabled and zero capture POSTs occurred.")
            bp.locator("#refresh").click();bp.locator(".document-row").filter(has_text="certificate").first.click()
            bp.locator("#review-value").fill("316L");bp.locator("#review-spans").select_option([o.get_attribute("value") for o in bp.locator("#review-spans option").all()])
            bp.locator("#review-reason").fill("Attempted relabeling of the required value for this acceptance test.");bp.get_by_role("button",name="Record interpretation",exact=True).click()
            expect(bp.locator("#notice")).to_contain_text("recognized delivered value")
            record("Browser review cannot relabel recognized delivered 304 as 316L.")
            provider.drop.add(f"/v2/payments/authorizations/{provider.auth_id}/capture")
            async def hold_after_capture(path):
                if path.endswith("/capture"):recovery_hold.set()
            provider.before_post=hold_after_capture
            sp.locator("#document-file").set_input_files(str(fixtures/"valid-table.pdf"));sp.get_by_role("button",name="Upload PDF",exact=True).click()
            expect(sp.locator("#documents")).to_contain_text("delivered_grade: 316L",timeout=45000)
            bp.locator("#refresh").click();bp.locator(".document-row").filter(has_text="delivered_grade: 316L").click()
            expect(bp.locator("#passage")).to_contain_text("PO-001 | LOT-001 | 316L")
            latest=bp.evaluate("state.documentId")
            latest_title=bp.locator("#document-title").inner_text()
            previous=bp.evaluate("state.data.documents.find(d=>d.kind==='certificate'&&d.id!==state.documentId).id")
            held=[];old_findings=f"{origin}/api/deals/{identity}/documents/{previous}/findings"
            bp.route(old_findings,lambda route:held.append(route))
            bp.locator(f'[data-document-id="{previous}"]').click()
            for _ in range(50):
                if held:break
                bp.wait_for_timeout(100)
            assert held,"The controlled old-document request was not intercepted"
            with bp.expect_response(f"{origin}/api/deals/{identity}/documents/{latest}/findings") as newer:
                bp.locator(f'[data-document-id="{latest}"]').click()
            newer.value.body();bp.wait_for_timeout(100)
            expect(bp.locator("#passage")).to_contain_text("PO-001 | LOT-001 | 316L")
            with bp.expect_response(old_findings) as released:
                held[0].continue_()
            released.value.body();bp.wait_for_timeout(250)
            expect(bp.locator("#document-title")).to_have_text(latest_title)
            expect(bp.locator("#passage")).to_contain_text("PO-001 | LOT-001 | 316L")
            assert bp.evaluate("state.documentId")==latest
            bp.unroute(old_findings)
            record("A deliberately delayed older-document response cannot replace the buyer's latest selected certificate or passages.")
            expect(bp.locator("#replace")).to_be_disabled();bp.locator("#replacement-check").check();bp.locator("#replace").click()
            expect(bp.locator("#decision-state")).to_have_text("Requirements passed",timeout=30000)
            expect(bp.locator("#payment-state")).to_have_text("Reconciling",timeout=30000)
            bp.screenshot(path=str(out/"02-capture-outcome-uncertain.png"),full_page=True)
            record("A provider-double lost capture response displayed Reconciling; the browser did not claim payment completion.")
            recovery_hold.clear()
            expect(bp.locator("#payment-state")).to_have_text("Capture confirmed",timeout=30000)
            bp.screenshot(path=str(out/"02-reviewed-replacement-captured.png"),full_page=True)
            record("Buyer inspected and accepted the table-layout replacement; supersession and evaluation permitted one final capture.")
            sp.locator("#refresh").click();expect(sp.locator("#capture")).to_be_enabled();sp.locator("#capture").click();sp.locator("#capture").click()
            results=sp.evaluate("async()=>Promise.all([api(route('capture'),'POST'),api(route('capture'),'POST')])")
            assert results[0]["id"]==results[1]["id"]
            assert len([c for c in provider.calls if c[0]=="POST" and c[1].endswith("/capture")])==1
            record("Repeated button clicks and concurrent browser requests reused one capture operation; one provider-double capture POST.")
            bp.set_viewport_size({"width":390,"height":844});bp.screenshot(path=str(out/"03-mobile.png"),full_page=True)
            assert bp.evaluate("document.documentElement.scrollWidth<=window.innerWidth"),"Mobile layout overflow"
            record("390px viewport has no horizontal overflow.")
            assert not errors,errors
            for context in contexts:context.close()
            report["videos"]={role:str(Path(page.video.path()).relative_to(out)) for role,page in (("buyer",bp),("supplier",sp))}
            browser.close()
        report["status"]="PASS_WITH_TEST_DOUBLES"
    except Exception as exc:
        report.update(status="FAIL",error=type(exc).__name__+": "+str(exc),worker_errors=errors)
        raise
    finally:
        server.should_exit=True;stop.set();thread.join(10)
        with db.tx() as conn:
            report["capture_operations"]=conn.execute("SELECT count(*) n FROM operations WHERE deal_id=%s AND kind='CAPTURE'",(identity,)).fetchone()["n"]
        report["provider_calls"]=[{"method":c[0],"path":c[1],"request_id":c[3]} for c in provider.calls]
        (out/"browser-result.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({"status":report["status"],"assertions":len(report["assertions"]),"report":str(out/"browser-result.json")}))


if __name__=="__main__":main()
