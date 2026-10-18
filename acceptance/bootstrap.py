"""Idempotent operator bootstrap. No PayPal calls or application approvals."""
import argparse
import json
import os
from pathlib import Path
import re

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from accord.config import Settings
from accord.contract import policy
from accord.db import DB,canonical,digest,uid,utcnow


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--migrate",action="store_true")
    args=parser.parse_args();load_dotenv();config=Settings()
    out=Path("acceptance-record");out.mkdir(exist_ok=True)
    if not os.environ.get("BUYER_PAYER_ID") and os.environ.get("PAYPAL_BUYER_PAYER_ID"):
        os.environ["BUYER_PAYER_ID"]=os.environ["PAYPAL_BUYER_PAYER_ID"]
    required=("BUYER_PAYER_ID","ACCORD_BUYER_PASSWORD","ACCORD_SUPPLIER_PASSWORD")
    missing=[key for key in required if not os.environ.get(key)]
    if missing:
        print(json.dumps({"status":"BLOCKED","missing_configuration":missing}));return 2
    payer=os.environ["BUYER_PAYER_ID"]
    if not re.fullmatch(r"[A-Z0-9-]{1,64}",payer) or not config.paypal_merchant_id:
        raise SystemExit("Configure the Sandbox merchant ID and valid buyer payer ID")
    if min(len(os.environ[k]) for k in required[1:])<12:
        raise SystemExit("Application passwords must each contain at least 12 characters")
    operator_url=os.environ.get("ACCORD_OWNER_DATABASE_URL") or config.database_url
    with DB(operator_url).tx() as conn:
        owner=conn.execute("""SELECT CASE WHEN to_regclass('public.deals') IS NULL
            THEN has_schema_privilege(current_user,'public','CREATE') ELSE
            (SELECT pg_has_role(current_user,relowner,'USAGE') FROM pg_class WHERE oid=to_regclass('public.deals')) END ok""").fetchone()
    if not owner["ok"]:
        print(json.dumps({"status":"BLOCKED","reason":"Set ACCORD_OWNER_DATABASE_URL to the schema-owner connection for migration and account provisioning."}));return 2
    if args.migrate:
        os.environ["DATABASE_URL"]=operator_url
        command.upgrade(Config("alembic.ini"),"head")
    db=DB(operator_url);hasher=PasswordHasher();p=policy();policy_hash=digest(canonical(p))
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(720750)")
        ids={}
        for role in ("buyer","supplier"):
            username=os.environ.get(f"ACCORD_{role.upper()}_USERNAME",role)
            password=os.environ[f"ACCORD_{role.upper()}_PASSWORD"]
            row=conn.execute("SELECT * FROM users WHERE username=%s",(username,)).fetchone()
            if row:
                if row["role"]!=role:
                    raise SystemExit("An existing username has a different role; choose another username")
                try:hasher.verify(row["password_hash"],password)
                except Exception:raise SystemExit("Existing application password differs; bootstrap will not reset it")
                ids[role]=row["id"]
            else:
                ids[role]=uid()
                conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,%s,%s,%s)",
                    (ids[role],username,hasher.hash(password),role))
        row=conn.execute("""SELECT id FROM deals WHERE buyer_id=%s AND supplier_id=%s
            AND merchant_id=%s AND payer_id=%s AND policy_hash=%s ORDER BY created_at LIMIT 1""",
            (ids["buyer"],ids["supplier"],config.paypal_merchant_id,payer,policy_hash)).fetchone()
        identity=row["id"] if row else uid()
        if not row:
            conn.execute("""INSERT INTO deals(id,buyer_id,supplier_id,merchant_id,payer_id,policy,policy_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (identity,ids["buyer"],ids["supplier"],config.paypal_merchant_id,payer,Jsonb(p),policy_hash))
    report={"status":"READY","timestamp":utcnow().isoformat(),"deal_id":str(identity),
        "policy_hash":policy_hash,"reused_existing_deal":bool(row),"note":"No approvals or PayPal operations performed."}
    (out/"bootstrap.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report));return 0


if __name__=="__main__":raise SystemExit(main())
