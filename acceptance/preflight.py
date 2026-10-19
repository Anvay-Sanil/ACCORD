"""Readiness checks with redacted output. Does not create or authorize a payment."""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from accord.config import Settings
from accord.db import DB,utcnow
from accord.paypal import PayPal,ProviderError


async def main():
    load_dotenv()
    destination=Path("acceptance-record/preflight.json");destination.parent.mkdir(parents=True,exist_ok=True)
    report={"timestamp":utcnow().isoformat(),"gates":{}}
    config=Settings();client=PayPal(config)
    report["configuration"]={k:bool(os.environ.get(k)) for k in (
        "PAYPAL_CLIENT_ID","PAYPAL_CLIENT_SECRET","PAYPAL_MERCHANT_ID","PAYPAL_WEBHOOK_ID","OPENAI_API_KEY",
        "GEMINI_API_KEY","BUYER_PAYER_ID","ACCORD_BUYER_PASSWORD","ACCORD_SUPPLIER_PASSWORD")}
    report["extraction_provider"]=config.extraction_provider
    report["extraction_model"]=config.gemini_model if config.extraction_provider=="gemini" else config.openai_model
    report["configuration"]["BUYER_PAYER_ID"]=bool(os.environ.get("BUYER_PAYER_ID") or os.environ.get("PAYPAL_BUYER_PAYER_ID"))
    report["configuration"]["ACCORD_OWNER_DATABASE_URL"]=bool(os.environ.get("ACCORD_OWNER_DATABASE_URL"))
    report["https_origin"]=config.app_origin.startswith("https://")
    try:
        with DB(config.database_url).tx() as conn:
            report["gates"]["application_database"]={"status":"PASS",
                "migration":conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],
                "deal_count":conn.execute("SELECT count(*) n FROM deals").fetchone()["n"],
                "schema_owner_connection":conn.execute("SELECT pg_has_role(current_user,relowner,'USAGE') ok FROM pg_class WHERE oid=to_regclass('public.deals')").fetchone()["ok"]}
    except Exception as exc:
        report["gates"]["application_database"]={"status":"BLOCKED","error":type(exc).__name__}
    try:
        await client.access_token()
        report["gates"]["paypal_oauth"]={"status":"PASS","note":"OAuth only; no buyer approval or payment proof."}
    except ProviderError as exc:
        report["gates"]["paypal_oauth"]={"status":"BLOCKED","http_status":exc.status,"error":exc.name}
    finally:await client.close()
    destination.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report))


if __name__=="__main__":asyncio.run(main())
