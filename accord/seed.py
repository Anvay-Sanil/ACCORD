"""Operator-only bootstrap. It neither contacts PayPal nor approves a deal."""
import argparse
import getpass
import json
import re

from argon2 import PasswordHasher
from psycopg.types.json import Jsonb

from .config import settings
from .contract import policy as make_policy
from .db import DB,canonical,digest,uid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--buyer-payer-id",required=True)
    parser.add_argument("--buyer-username",default="buyer")
    parser.add_argument("--supplier-username",default="supplier")
    parser.add_argument("--po",default="PO-001")
    parser.add_argument("--lot",default="LOT-001")
    args = parser.parse_args()
    config = settings()
    if not config.paypal_merchant_id or any(not re.fullmatch(r"[A-Z0-9-]{1,64}",x) for x in (args.po,args.lot,args.buyer_payer_id)):
        raise SystemExit("Set the merchant ID and valid PO, lot, and buyer payer ID")
    buyer_password = getpass.getpass("New buyer application password (at least 12 characters): ")
    supplier_password = getpass.getpass("New supplier application password (at least 12 characters): ")
    if min(len(buyer_password),len(supplier_password))<12:
        raise SystemExit("Password too short")
    buyer,supplier,deal_id = uid(),uid(),uid()
    policy = make_policy(args.po,args.lot)
    with DB(config.database_url).tx() as conn:
        hasher = PasswordHasher()
        for identity,username,password,role in ((buyer,args.buyer_username,buyer_password,"buyer"),
                (supplier,args.supplier_username,supplier_password,"supplier")):
            conn.execute("INSERT INTO users(id,username,password_hash,role) VALUES(%s,%s,%s,%s)",
                         (identity,username,hasher.hash(password),role))
        conn.execute("""INSERT INTO deals(id,buyer_id,supplier_id,merchant_id,payer_id,policy,policy_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s)""",(deal_id,buyer,supplier,config.paypal_merchant_id,args.buyer_payer_id,
                                          Jsonb(policy),digest(canonical(policy))))
    print(json.dumps({"deal_id":deal_id,"policy":policy,"policy_hash":digest(canonical(policy))},indent=2))


if __name__ == "__main__":
    main()
