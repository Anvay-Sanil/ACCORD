"""Bounded HTTP. Retry scheduling belongs to the durable coordinator, not this client."""
import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx


class ProviderError(Exception):
    def __init__(self, status=0, name="TRANSPORT_UNCERTAIN", retry_after=0):
        super().__init__(name)
        self.status, self.name, self.retry_after = status, name, retry_after


def resource_path(kind, identity):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identity or ""):
        raise ValueError("Invalid provider resource ID")
    base = {"order": "/v2/checkout/orders", "authorization": "/v2/payments/authorizations",
            "capture": "/v2/payments/captures"}[kind]
    return f"{base}/{identity}"


def event_resource_ids(event):
    """Extract correlation IDs; never request a URL supplied by a webhook.

    A refunded event identifies a refund resource, whose documented `up` link
    may be the only capture relationship. Decode only a Sandbox capture path,
    then build canonical resource requests with resource_path().
    """
    resource = event.get("resource", {})
    related = resource.get("supplementary_data", {}).get("related_ids", {})
    ids = {kind: related.get(kind + "_id") for kind in ("order", "authorization", "capture")}
    event_type = event.get("event_type", "")
    if event_type.startswith("PAYMENT.AUTHORIZATION."):
        ids["authorization"] = resource.get("id") or ids["authorization"]
    elif event_type.startswith("PAYMENT.CAPTURE."):
        if event_type != "PAYMENT.CAPTURE.REFUNDED":
            ids["capture"] = resource.get("id") or ids["capture"]
        else:
            captures = {ids["capture"]} if ids["capture"] else set()
            links = resource.get("links", [])
            if not isinstance(links, list):
                raise ValueError("Invalid refund relationships")
            for link in links:
                if not isinstance(link, dict) or link.get("rel") != "up":
                    continue
                if link.get("method", "GET") != "GET" or not isinstance(link.get("href"), str):
                    raise ValueError("Invalid refund capture link")
                url = urlsplit(link["href"])
                match = re.fullmatch(r"/v2/payments/captures/([A-Za-z0-9-]{1,64})", url.path)
                if (url.scheme != "https" or url.hostname not in
                    {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"} or
                    url.port not in (None, 443) or url.username or url.password or
                    url.query or url.fragment or not match):
                    raise ValueError("Invalid Sandbox refund capture relationship")
                captures.add(match.group(1))
            if len(captures) > 1:
                raise ValueError("Conflicting refund capture relationships")
            ids["capture"] = next(iter(captures), None)
    for kind, identity in ids.items():
        if identity is not None:
            resource_path(kind, identity)  # Validate IDs before database correlation.
    return ids


def retry_after_seconds(value):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


def backoff(attempt, retry_after=0):
    # Retry-After is a lower bound. The worker refuses POSTs beyond their deadline.
    return max(retry_after, random.uniform(1, min(300, 2 ** min(attempt + 1, 8))))


class PayPal:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.paypal_base_url, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(10, connect=3, write=5, pool=2),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        self.token, self.token_until = None, 0.0
        self.token_lock = asyncio.Lock()
        self.cooldown_until = 0.0
        self.drop_used = False

    async def close(self):
        await self.client.aclose()

    async def _http(self, method, path, **kwargs):
        if self.cooldown_until > time.monotonic():
            raise ProviderError(429, "RATE_LIMITED", self.cooldown_until - time.monotonic())
        try:
            async with asyncio.timeout(20):
                async with self.client.stream(method, path, **kwargs) as response:
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > 1_048_576:
                            raise ProviderError(0, "OVERSIZED_PROVIDER_RESPONSE")
                    status = response.status_code
                    if status == 429:
                        wait = max(1, retry_after_seconds(response.headers.get("Retry-After")))
                        self.cooldown_until = time.monotonic() + wait
                        raise ProviderError(429, "RATE_LIMITED", wait)
                    try:
                        body = json.loads(data) if data else {}
                    except (ValueError, UnicodeDecodeError):
                        raise ProviderError(status, "INVALID_PROVIDER_JSON")
                    if not isinstance(body, dict):
                        raise ProviderError(status, "INVALID_PROVIDER_JSON")
                    if status >= 300:
                        details = body.get("details") or []
                        name = details[0].get("issue") if details else body.get("name", "PROVIDER_ERROR")
                        raise ProviderError(status, name or "PROVIDER_ERROR")
                    return body
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderError() from exc

    async def access_token(self):
        async with self.token_lock:
            if self.token and time.monotonic() < self.token_until:
                return self.token
            body = await self._http("POST", "/v1/oauth2/token",
                auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
                data={"grant_type": "client_credentials"})
            self.token = body["access_token"]
            self.token_until = time.monotonic() + max(0, int(body["expires_in"]) - 60)
            return self.token

    async def request(self, method, path, payload=None, key=None, deadline=None):
        # No hidden POST retry. A 401 invalidates the token for a later durable attempt.
        token = await self.access_token()
        if deadline is not None and datetime.now(timezone.utc) >= deadline:
            raise ProviderError(0,"POST_DEADLINE_REACHED")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if key:
            headers["PayPal-Request-Id"] = str(key)
        try:
            body = await self._http(method, path, headers=headers,
                                    content=payload.encode() if payload is not None else None)
        except ProviderError as exc:
            if exc.status == 401:
                self.token = None
            raise
        if (method == "POST" and path.endswith("/capture") and
                self.settings.fault_drop_capture_response and not self.drop_used):
            self.drop_used = True
            raise ProviderError(0, "LOCAL_FAULT_RESPONSE_DROPPED")
        return body

    async def get(self, path):
        return await self.request("GET", path)

    async def post(self, path, payload, key, deadline=None):
        return await self.request("POST", path, payload, key, deadline)

    async def verify(self, raw, headers):
        required = {"auth_algo": "paypal-auth-algo", "cert_url": "paypal-cert-url",
                    "transmission_id": "paypal-transmission-id",
                    "transmission_sig": "paypal-transmission-sig",
                    "transmission_time": "paypal-transmission-time"}
        if not self.settings.paypal_webhook_id or any(not headers.get(v) for v in required.values()):
            return False
        # Retain webhook_event's original JSON representation inside the request envelope.
        envelope = {k: headers[v] for k, v in required.items()}
        envelope["webhook_id"] = self.settings.paypal_webhook_id
        payload = json.dumps(envelope)[:-1] + ',"webhook_event":' + raw.decode("utf-8") + '}'
        answer = await self.request("POST", "/v1/notifications/verify-webhook-signature", payload)
        return answer.get("verification_status") == "SUCCESS"
