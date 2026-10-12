import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class DB:
    def __init__(self, url):
        self.url = url

    @contextmanager
    def tx(self):
        with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SET LOCAL statement_timeout = '10s'")
            yield conn


def audit(conn, deal_id, event, data, actor=None):
    conn.execute("INSERT INTO audit(id,deal_id,event,data,actor_id) VALUES(%s,%s,%s,%s,%s)",
                 (uid(), deal_id, event, Jsonb(data), actor))

