from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from accord.api import create_app
from accord.config import Settings
from accord.coordinator import Coordinator
from accord.db import uid
from tests.fake_paypal import FakePayPal
from tests.test_ingress import client_for,login


def test_browser_configuration_only_exposes_public_client_id(db,deal,clock):
    config=Settings(database_url=db.url,app_origin="https://testserver",paypal_client_id="public-id",
        paypal_client_secret="PRIVATE-SECRET",paypal_merchant_id="MERCHANT1",openai_api_key="PRIVATE-MODEL-KEY")
    client=TestClient(create_app(config,db,FakePayPal(deal,clock)),base_url="https://testserver")
    response=client.get("/api/browser-config")
    assert response.json()=={"environment":"sandbox","label":"PayPal Sandbox","client_id":"public-id","checkout_configured":True}
    assert "PRIVATE" not in response.text
    assert client.get("/").status_code==200
    assert client.get("/assets/app.js").status_code==200
    assert "script-src 'self'" in client.get("/").headers["content-security-policy"]


def test_workspace_and_list_are_membership_scoped(db,deal,clock):
    client,_=client_for(db,deal,clock)
    assert client.get("/api/deals").status_code==401
    assert client.get(f"/api/deals/{deal['id']}/workspace").status_code==401
    login(client,deal)
    assert [d["id"] for d in client.get("/api/deals").json()["deals"]]==[str(deal["id"])]
    content=client.get(f"/api/deals/{deal['id']}/workspace").json()
    assert content["deal"]["policy_hash"]==deal["policy_hash"]
    assert "lease_generation" not in content["deal"]
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,'journey-outsider',%s,'buyer')",
            (uid(),PasswordHasher().hash("test-password-only")))
    client.post("/api/login",headers={"Origin":"https://testserver"},json={"username":"journey-outsider","password":"test-password-only"})
    assert client.get("/api/deals").json()=={"deals":[]}
    assert client.get(f"/api/deals/{deal['id']}/workspace").status_code==404


def test_original_pdf_keeps_sandbox_header_and_does_not_enter_workspace_json(db,deal,clock):
    body=b"%PDF-immutable-original"
    identity=Coordinator(db).add_document(deal["id"],deal["supplier_id"],"supplier","certificate",body)
    client,_=client_for(db,deal,clock);login(client,deal)
    response=client.get(f"/api/deals/{deal['id']}/documents/{identity}")
    assert response.content==body
    assert response.headers["content-security-policy"]=="sandbox"
    assert response.headers["content-disposition"].startswith("attachment;")
    workspace=client.get(f"/api/deals/{deal['id']}/workspace").json()
    assert "body" not in workspace["documents"][0]


def test_verified_event_retains_verification_inputs_once_without_mislabeling_a_double(db,deal,clock):
    client,_=client_for(db,deal,clock)
    headers={name:"TEST-ONLY" for name in ("paypal-auth-algo","paypal-cert-url","paypal-transmission-id","paypal-transmission-sig","paypal-transmission-time")}
    raw=b'{ "id":"WH-JOURNEY", "event_type":"PAYMENT.CAPTURE.COMPLETED", "resource":{"id":"CAP1"} }'
    assert client.post("/api/webhooks/paypal",content=raw,headers=headers).status_code==200
    assert client.post("/api/webhooks/paypal",content=raw,headers=headers).status_code==200
    with db.tx() as conn:
        records=conn.execute("SELECT * FROM webhook_inbox WHERE event_id='WH-JOURNEY'").fetchall()
    assert len(records)==1
    assert bytes(records[0]["raw_body"])==raw
    assert records[0]["verification_headers"]==headers
    assert records[0]["verification_method"]=="test_double"
