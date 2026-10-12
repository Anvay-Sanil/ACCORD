import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from accord.api import create_app
from accord.config import Settings
from accord.coordinator import Coordinator
from accord.db import uid
from accord.paypal import ProviderError,PayPal,backoff
from tests.fake_paypal import FakePayPal
from tests.test_operations import run


def client_for(db,deal,clock):
    settings = Settings(database_url=db.url,app_origin="https://testserver",session_secure=True)
    fake = FakePayPal(deal,clock)
    return TestClient(create_app(settings,db,fake),base_url="https://testserver"),fake


def login(client,deal,role="buyer"):
    response = client.post("/api/login",headers={"Origin":"https://testserver"},json={
        "username":deal[f"{role}_username"],"password":"test-password-only"})
    assert response.status_code==200
    return response,{"Origin":"https://testserver","X-CSRF-Token":response.json()["csrf_token"]}


def test_cookie_flags_csrf_origin_and_role(db,deal,clock):
    client,_ = client_for(db,deal,clock)
    response,headers = login(client,deal)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    route = f"/api/deals/{deal['id']}/orders"
    assert client.post(route).status_code==403
    assert client.post(route,headers={"Origin":"https://evil.invalid","X-CSRF-Token":headers["X-CSRF-Token"]}).status_code==403
    assert client.post(route,headers=headers).status_code==202
    assert client.post(f"/api/deals/{deal['id']}/capture",headers=headers).status_code==403


def test_login_csrf_and_rate_limit(db,deal,clock):
    client,_ = client_for(db,deal,clock)
    payload = {"username":deal["buyer_username"],"password":"incorrect"}
    assert client.post("/api/login",json=payload).status_code==403
    for _ in range(10):
        assert client.post("/api/login",json=payload,headers={"Origin":"https://testserver"}).status_code==401
    assert client.post("/api/login",json=payload,headers={"Origin":"https://testserver"}).status_code==429


def test_deal_document_payment_access_and_oversized_ingress(db,deal,clock):
    client,_ = client_for(db,deal,clock)
    _,headers = login(client,deal)
    unknown = uid()
    for route in (f"/api/deals/{unknown}",f"/api/deals/{unknown}/documents/{uid()}"):
        assert client.get(route).status_code==404
    assert client.post(f"/api/deals/{unknown}/orders",headers=headers).status_code==404
    path = f"/api/deals/{deal['id']}/documents?kind=certificate"
    assert client.post(path,headers=headers,content=b"%PDF-invalid").status_code==409
    assert client.post(path,headers=headers,content=b"x"*5_242_881).status_code==413


def test_webhook_verification_duplicate_and_outage(db,deal,clock):
    client,fake = client_for(db,deal,clock)
    event = {"id":"WH-1","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"CAP1"}}
    fake.verify_result = False
    assert client.post("/api/webhooks/paypal",json=event).status_code==401
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) n FROM webhook_inbox").fetchone()["n"]==0
    fake.verify_result = ProviderError(503,"UNAVAILABLE")
    assert client.post("/api/webhooks/paypal",json=event).status_code==503
    fake.verify_result = True
    assert client.post("/api/webhooks/paypal",json=event).status_code==200
    assert client.post("/api/webhooks/paypal",json=event).status_code==200
    with db.tx() as conn:
        assert conn.execute("SELECT count(*) n FROM webhook_inbox").fetchone()["n"]==1


def test_known_deal_and_document_are_hidden_from_non_member(db,deal,clock):
    from argon2 import PasswordHasher
    identity=Coordinator(db).add_document(deal["id"],deal["supplier_id"],"supplier","certificate",b"%PDF-fixture")
    with db.tx() as conn:
        conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,'outsider',%s,'buyer')",
                     (uid(),PasswordHasher().hash("test-password-only")))
    client,_=client_for(db,deal,clock)
    response=client.post("/api/login",headers={"Origin":"https://testserver"},json={"username":"outsider","password":"test-password-only"})
    headers={"Origin":"https://testserver","X-CSRF-Token":response.json()["csrf_token"]}
    assert client.get(f"/api/deals/{deal['id']}").status_code==404
    assert client.get(f"/api/deals/{deal['id']}/documents/{identity}").status_code==404
    assert client.post(f"/api/deals/{deal['id']}/capture",headers=headers).status_code==404


def test_malformed_webhook_cannot_enter_work_queue(db,deal,clock):
    client,_=client_for(db,deal,clock)
    event={"id":"WH-BROKEN","event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"supplementary_data":{"related_ids":[]}}}
    assert client.post("/api/webhooks/paypal",json=event).status_code==400


def test_public_configuration_rejects_fault_injection_and_insecure_cookie(db):
    with pytest.raises(ValueError):
        Settings(database_url=db.url,public_deployment=True,fault_drop_capture_response=True)
    with pytest.raises(ValueError):
        Settings(database_url=db.url,public_deployment=True,session_secure=False)
    with pytest.raises(ValueError):
        Settings(database_url=db.url,paypal_base_url="https://api-m.paypal.com")


def test_oauth_reuse_and_429_no_hidden_post_retry(db):
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path=="/v1/oauth2/token":
            return httpx.Response(200,json={"access_token":"FAKE_TOKEN","expires_in":3600})
        if request.method=="GET":
            return httpx.Response(200,json={"id":"ORDER1"})
        return httpx.Response(429,headers={"Retry-After":"120"},json={"name":"RATE_LIMIT_REACHED"})
    async def scenario():
        client = PayPal(Settings(database_url=db.url),httpx.MockTransport(handler))
        await client.get("/v2/checkout/orders/ORDER1")
        await client.get("/v2/checkout/orders/ORDER1")
        with pytest.raises(ProviderError) as failure:
            await client.post("/v2/payments/authorizations/AUTH1/capture","{}","one-key")
        assert failure.value.retry_after==120
        with pytest.raises(ProviderError):
            await client.get("/v2/checkout/orders/ORDER1")
        await client.close()
    run(scenario())
    assert calls.count("/v1/oauth2/token")==1
    assert len(calls)==4
    assert backoff(1,120)>=120


def test_verification_forwards_exact_event_and_checks_success_field(db):
    raw = b'{ "id" : "WH-TEST", "amount": 750.00 }'
    requests=[]
    def handler(request):
        if request.url.path=="/v1/oauth2/token":
            return httpx.Response(200,json={"access_token":"FAKE","expires_in":3600})
        requests.append(request)
        return httpx.Response(200,json={"verification_status":"FAILURE"})
    async def scenario():
        client = PayPal(Settings(database_url=db.url,paypal_webhook_id="REGISTERED-ID"),httpx.MockTransport(handler))
        headers={name:"sample" for name in ("paypal-auth-algo","paypal-cert-url","paypal-transmission-id","paypal-transmission-sig","paypal-transmission-time")}
        assert not await client.verify(raw,headers)
        assert raw in requests[0].content
        assert json.loads(requests[0].content)["webhook_id"]=="REGISTERED-ID"
        await client.close()
    run(scenario())
