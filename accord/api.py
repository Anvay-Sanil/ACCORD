import asyncio
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, settings
from .coordinator import Coordinator, Conflict
from .db import DB, canonical, digest, uid, utcnow
from .evidence import Finding, evaluate, review_interpretation
from .paypal import PayPal, ProviderError, event_resource_ids


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=1,max_length=100)
    password: str = Field(min_length=1,max_length=200)


class Approval(Input):
    policy_hash: str = Field(min_length=64,max_length=64)


class Review(Input):
    finding: Finding
    reason: str = Field(min_length=10,max_length=2000)


class Replacement(Input):
    old_id: UUID
    new_id: UUID


class BodyLimit:
    def __init__(self,app):
        self.app = app

    async def __call__(self,scope,receive,send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        path = scope["path"]
        cap = 5_242_880 if path.endswith("/documents") else 262144
        chunks,total = [],0
        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body = message.get("body",b"")
                    total += len(body)
                    if total > cap:
                        return await JSONResponse({"detail":"Request too large"},413)(scope,receive,send)
                    chunks.append(body)
                    if not message.get("more_body",False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail":"Request timeout"},408)(scope,receive,send)
        sent = False
        async def replay():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type":"http.request","body":b"".join(chunks),"more_body":False}
        await self.app(scope,replay,send)


def create_app(config: Settings | None=None,db=None,paypal=None):
    config = config or settings()
    db = db or DB(config.database_url)
    paypal = paypal or PayPal(config)
    coordinator = Coordinator(db)
    hasher = PasswordHasher()
    dummy_hash = hasher.hash(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await paypal.close()

    app = FastAPI(title="ACCORD payment foundation",lifespan=lifespan)
    web = Path(__file__).with_name("web")
    app.mount("/assets",StaticFiles(directory=web),name="assets")

    @app.get("/",include_in_schema=False)
    def home():
        return FileResponse(web/"index.html")

    @app.get("/api/browser-config")
    def browser_config():
        return {"environment":"sandbox","label":config.provider_label,
                "client_id":config.paypal_client_id,
                "checkout_configured":bool(config.paypal_client_id and config.paypal_client_secret and config.paypal_merchant_id)}
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[urlparse(config.app_origin).hostname])

    @app.exception_handler(Conflict)
    async def conflict_handler(request,exc):
        return JSONResponse({"detail":str(exc)},409)

    @app.middleware("http")
    async def security_headers(request,call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        # No uploaded text is rendered as HTML. SDK is loaded from the fixed Sandbox origin.
        response.headers.setdefault("Content-Security-Policy",(
            "default-src 'self'; script-src 'self' https://*.paypal.com https://*.paypalobjects.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.paypal.com https://*.paypalobjects.com; "
            "connect-src 'self' https://*.paypal.com https://*.paypalobjects.com; "
            "frame-src https://*.paypal.com; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"))
        if config.public_deployment:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def origin(request):
        if request.headers.get("origin") != config.app_origin:
            raise HTTPException(403,"Origin rejected")

    def user(request,mutation=False):
        token = request.cookies.get("accord_session","")
        with db.tx() as conn:
            row = conn.execute("""SELECT u.*,s.csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.expires_at>now()""",(digest(token),)).fetchone()
        if not row:
            raise HTTPException(401,"Authentication required")
        if mutation:
            origin(request)
            csrf = request.headers.get("x-csrf-token","")
            if not csrf or not hmac.compare_digest(row["csrf_hash"],digest(csrf)):
                raise HTTPException(403,"CSRF token rejected")
        return row

    def access(request,deal_id,mutation=False,role=None):
        actor = user(request,mutation)
        with db.tx() as conn:
            deal = conn.execute("SELECT * FROM deals WHERE id=%s",(deal_id,)).fetchone()
        if not deal or actor["id"] not in (deal["buyer_id"],deal["supplier_id"]):
            raise HTTPException(404,"Deal not found")
        if role and (actor["role"] != role or actor["id"] != deal[f"{role}_id"]):
            raise HTTPException(403,"Role not permitted")
        return actor,deal

    @app.get("/health")
    def health():
        with db.tx() as conn:
            conn.execute("SELECT 1")
        return {"status":"ok","environment":"sandbox"}

    @app.post("/api/login")
    def login(body: Login,request: Request,response: Response):
        origin(request)  # Pre-session login CSRF protection; subsequent mutations also require token.
        bucket = digest(body.username.lower())
        now = utcnow()
        with db.tx() as conn:
            conn.execute("""INSERT INTO login_limits(bucket,attempts,resets_at) VALUES(%s,0,%s)
                ON CONFLICT DO NOTHING""",(bucket,now+timedelta(minutes=15)))
            limit = conn.execute("SELECT * FROM login_limits WHERE bucket=%s FOR UPDATE",(bucket,)).fetchone()
            if limit["resets_at"]<=now:
                conn.execute("UPDATE login_limits SET attempts=0,resets_at=%s WHERE bucket=%s",(now+timedelta(minutes=15),bucket))
                limit["attempts"] = 0
            if limit["attempts"]>=10:
                raise HTTPException(429,"Login rate limit reached",headers={"Retry-After":"900"})
            conn.execute("UPDATE login_limits SET attempts=attempts+1 WHERE bucket=%s",(bucket,))
            account = conn.execute("SELECT * FROM users WHERE username=%s",(body.username,)).fetchone()
        try:
            valid = hasher.verify(account["password_hash"] if account else dummy_hash,body.password)
        except (VerifyMismatchError,InvalidHashError):
            valid = False
        if not account or not valid:
            raise HTTPException(401,"Invalid credentials")
        token,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        with db.tx() as conn:
            # Rotate existing session to prevent session fixation.
            conn.execute("DELETE FROM sessions WHERE token_hash=%s",(digest(request.cookies.get("accord_session","")),))
            conn.execute("INSERT INTO sessions(token_hash,csrf_hash,user_id,expires_at) VALUES(%s,%s,%s,%s)",
                         (digest(token),digest(csrf),account["id"],now+timedelta(hours=8)))
        response.set_cookie("accord_session",token,httponly=True,secure=config.session_secure,
                            samesite="lax",max_age=28800,path="/")
        return {"csrf_token":csrf,"role":account["role"]}

    @app.post("/api/logout")
    def logout(request: Request,response: Response):
        user(request,True)
        with db.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=%s",(digest(request.cookies["accord_session"]),))
        response.delete_cookie("accord_session",path="/",secure=config.session_secure,httponly=True,samesite="lax")
        return {"ok":True}

    @app.get("/api/deals/{deal_id}")
    def deal(deal_id: UUID,request: Request):
        actor,d = access(request,deal_id)
        with db.tx() as conn:
            ops = conn.execute("SELECT id,kind,state,provider_id,last_error,attempts FROM operations WHERE deal_id=%s",(deal_id,)).fetchall()
        return {"deal":{k:v for k,v in d.items() if k not in ("lease_generation","lease_until","active_operation")},"operations":ops}

    @app.get("/api/deals")
    def my_deals(request: Request):
        actor=user(request)
        with db.tx() as conn:
            rows=conn.execute("""SELECT id,policy,payment_state FROM deals WHERE buyer_id=%s OR supplier_id=%s
                ORDER BY created_at DESC LIMIT 50""",(actor["id"],actor["id"])).fetchall()
        return {"deals":rows}

    @app.get("/api/deals/{deal_id}/workspace")
    def workspace(deal_id: UUID,request: Request):
        actor,d=access(request,deal_id)
        with db.tx() as conn:
            docs=conn.execute("""SELECT id,version,kind,sha256,active,parse_state,extraction
                FROM documents WHERE deal_id=%s ORDER BY version DESC""",(deal_id,)).fetchall()
            decision=conn.execute("""SELECT * FROM decisions WHERE deal_id=%s AND evidence_generation=%s
                ORDER BY created_at DESC,id DESC LIMIT 1""",(deal_id,d["evidence_generation"])).fetchone()
            events=conn.execute("""SELECT event,data,created_at FROM audit WHERE deal_id=%s
                ORDER BY created_at DESC,id DESC LIMIT 40""",(deal_id,)).fetchall()
            ops=conn.execute("""SELECT id,kind,state,provider_id,decision_id,request_id,first_dispatch_at,
                retry_deadline,attempts,last_error FROM operations WHERE deal_id=%s ORDER BY created_at""",(deal_id,)).fetchall()
        return {"deal":{k:v for k,v in d.items() if k not in ("lease_generation","lease_until","active_operation")},
                "documents":docs,"decision":decision,"events":events,"operations":ops}

    @app.post("/api/deals/{deal_id}/approve")
    def approve(deal_id: UUID,body: Approval,request: Request):
        actor,_ = access(request,deal_id,True)
        coordinator.approve(deal_id,actor["id"],actor["role"],body.policy_hash)
        return {"approved":True}

    def operation_view(value):
        return {"id":str(value["id"]),"state":value["state"]} if value else {"state":"RECONCILING"}

    @app.post("/api/deals/{deal_id}/orders",status_code=202)
    def create_order(deal_id: UUID,request: Request):
        access(request,deal_id,True,"buyer")
        return operation_view(coordinator.queue_create(deal_id))

    @app.post("/api/deals/{deal_id}/authorize",status_code=202)
    def authorize(deal_id: UUID,request: Request):
        access(request,deal_id,True,"buyer")
        return operation_view(coordinator.queue_authorize(deal_id))

    @app.post("/api/deals/{deal_id}/capture",status_code=202)
    def capture(deal_id: UUID,request: Request):
        access(request,deal_id,True,"supplier")
        return operation_view(coordinator.queue_capture(deal_id))

    @app.post("/api/deals/{deal_id}/void",status_code=202)
    def void(deal_id: UUID,request: Request):
        access(request,deal_id,True,"supplier")
        return operation_view(coordinator.request_void(deal_id))

    @app.post("/api/deals/{deal_id}/documents",status_code=202)
    async def upload(deal_id: UUID,kind: Literal["certificate","receipt"],request: Request):
        actor,_ = access(request,deal_id,True)
        identity = coordinator.add_document(deal_id,actor["id"],actor["role"],kind,await request.body())
        return {"document_id":identity}

    @app.get("/api/deals/{deal_id}/documents/{document_id}")
    def document(deal_id: UUID,document_id: UUID,request: Request):
        access(request,deal_id)
        with db.tx() as conn:
            doc = conn.execute("SELECT body FROM documents WHERE deal_id=%s AND id=%s",(deal_id,document_id)).fetchone()
        if not doc:
            raise HTTPException(404,"Document not found")
        return Response(bytes(doc["body"]),media_type="application/pdf",headers={
            "Content-Disposition":'attachment; filename="evidence.pdf"',"Content-Security-Policy":"sandbox"})

    @app.get("/api/deals/{deal_id}/documents/{document_id}/findings")
    def findings(deal_id: UUID,document_id: UUID,request: Request):
        access(request,deal_id)
        with db.tx() as conn:
            doc = conn.execute("SELECT version,sha256,parse_state,parsed,extraction FROM documents WHERE deal_id=%s AND id=%s",(deal_id,document_id)).fetchone()
        if not doc:
            raise HTTPException(404,"Document not found")
        return doc

    @app.post("/api/deals/{deal_id}/delivery-ack")
    def delivery(deal_id: UUID,request: Request):
        actor,_ = access(request,deal_id,True,"buyer")
        coordinator.acknowledge_delivery(deal_id,actor["id"])
        return {"acknowledged":True}

    @app.post("/api/deals/{deal_id}/supersede")
    def supersede(deal_id: UUID,body: Replacement,request: Request):
        actor,_ = access(request,deal_id,True,"buyer")
        coordinator.supersede(deal_id,actor["id"],str(body.old_id),str(body.new_id))
        return {"reviewed":True}

    @app.post("/api/deals/{deal_id}/review")
    def review(deal_id: UUID,body: Review,request: Request):
        actor,_ = access(request,deal_id,True,"buyer")
        review_interpretation(db,deal_id,actor["id"],body.finding,body.reason)
        return {"reviewed":True}

    @app.post("/api/deals/{deal_id}/evaluate")
    def evaluation(deal_id: UUID,request: Request):
        access(request,deal_id,True)
        decision = evaluate(db,deal_id)
        if decision["outcome"] == "PASS":
            try:
                decision["capture_operation"] = operation_view(coordinator.queue_capture(deal_id))
            except Conflict as exc:
                decision["capture_blocked"] = str(exc)
        return decision

    @app.post("/api/webhooks/paypal")
    async def webhook(request: Request):
        raw = await request.body()
        try:
            event = json.loads(raw)
            if (not isinstance(event,dict) or not isinstance(event.get("id"),str) or len(event["id"])>150 or
                not isinstance(event.get("event_type"),str) or not isinstance(event.get("resource"),dict)):
                raise ValueError()
            supplementary = event["resource"].get("supplementary_data",{})
            if not isinstance(supplementary,dict) or not isinstance(supplementary.get("related_ids",{}),dict):
                raise ValueError()
            event_resource_ids(event)
        except (ValueError,TypeError,UnicodeDecodeError):
            raise HTTPException(400,"Invalid webhook")
        try:
            valid = await paypal.verify(raw,request.headers)
        except ProviderError:
            raise HTTPException(503,"Webhook verification temporarily unavailable",headers={"Retry-After":"30"})
        if not valid:
            raise HTTPException(401,"Webhook verification failed")
        if event["event_type"] not in {"PAYMENT.AUTHORIZATION.CREATED","PAYMENT.AUTHORIZATION.VOIDED",
                "PAYMENT.CAPTURE.COMPLETED","PAYMENT.CAPTURE.PENDING","PAYMENT.CAPTURE.DECLINED",
                "PAYMENT.CAPTURE.REFUNDED","PAYMENT.CAPTURE.REVERSED"}:
            return {"accepted":True,"ignored":True}
        with db.tx() as conn:
            verification_headers={key:request.headers.get(key,"") for key in (
                "paypal-auth-algo","paypal-cert-url","paypal-transmission-id","paypal-transmission-sig","paypal-transmission-time")}
            conn.execute("""INSERT INTO webhook_inbox(event_id,raw_body,event,verification_headers,verification_method)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(event_id) DO NOTHING""",
                (event["id"],raw,Jsonb(event),Jsonb(verification_headers),getattr(paypal,"verification_method","test_double")))
        return {"accepted":True}

    return app
