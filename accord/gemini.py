"""Gemini structured proposals. Admissibility stays in evidence.py.

No tools, provider fallback, or durable conversational state. HTTP retries are
bounded inside the extraction lease; quota exhaustion becomes buyer review.
"""
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
ADAPTER_VERSION = "gemini-interactions-v3"
MAX_RESPONSE_BYTES = 1024 * 1024
GENERATION_CONFIG = {"max_output_tokens": 4000, "thinking_level": "low", "thinking_summaries": "none"}


def provider_schema(schema):
    """Project the schema for constrained generation, not backend admission.

The real API rejected nested array cardinality constraints. Keep all field
types, enums, required fields and extra-property restrictions in its request;
the full Proposal validator still enforces every array bound after generation.
"""
    def visit(value, refs=()):
        if isinstance(value,list): return [visit(x,refs) for x in value]
        if not isinstance(value,dict): return value
        if "$ref" in value:
            ref=value["$ref"]
            if not ref.startswith("#/$defs/") or ref in refs:
                raise ValueError("Unsupported extraction schema reference")
            return visit(schema["$defs"][ref.split("/")[-1]],refs+(ref,))
        return {k:visit(v,refs) for k,v in value.items()
                if k not in ("$defs","title","minItems","maxItems")}
    return visit(schema)


def retry_delay(response, attempt):
    raw = response.headers.get("Retry-After", "")
    try:
        minimum = float(raw)
    except ValueError:
        try:
            minimum = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            minimum = 0
    return max(0, minimum, 2 ** attempt + random.uniform(0, 1))


async def generate(config, prompt, document_input, schema):
    if not config.gemini_api_key:
        raise ValueError("Gemini key unavailable; buyer review required")
    payload = {
        "model": config.gemini_model, "store": False, "stream": False,
        "system_instruction": prompt, "input": json.dumps(document_input),
        "generation_config": GENERATION_CONFIG,
        "response_format": {"type": "text", "mime_type": "application/json", "schema": provider_schema(schema)},
    }
    deadline = time.monotonic() + 50
    async with asyncio.timeout(50):
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=5), follow_redirects=False) as client:
            for attempt in range(2):
                async with client.stream("POST", ENDPOINT,
                        headers={"x-goog-api-key": config.gemini_api_key}, json=payload) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ValueError("Model response exceeds byte limit")
                    # Retain the bounded body for safe provider-error classification.
                    # aiter_bytes already decoded Content-Encoding. Keeping that
                    # header would decompress the bounded body a second time.
                    headers = {k:v for k,v in response.headers.items()
                               if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
                    bounded = httpx.Response(response.status_code, headers=headers,
                                             content=bytes(body), request=response.request)
                if bounded.status_code in (429, 503) and attempt == 0:
                    delay = retry_delay(bounded, attempt)
                    if delay <= 10 and time.monotonic() + delay + 40 < deadline:
                        await asyncio.sleep(delay)
                        continue
                bounded.raise_for_status()
                result = bounded.json()
                break
    if result.get("status") != "completed":
        raise ValueError("Model response incomplete")
    steps = result.get("steps", [])
    # Reject tool requests, refusals, missing output and multiple JSON messages.
    outputs = [s for s in steps if s.get("type") == "model_output"]
    # The API may include a signed thought step even with summaries disabled.
    # It is retained as provider metadata, never interpreted as evidence.
    if len(outputs) != 1 or any(s.get("type") not in ("model_output", "thought") for s in steps):
        raise ValueError("Unexpected model output steps")
    content = outputs[0].get("content", [])
    if len(content) != 1 or content[0].get("type") != "text" or not isinstance(content[0].get("text"), str):
        raise ValueError("Expected one structured text response")
    return content[0]["text"], result, attempt + 1
