import json
import gzip
import asyncio
from functools import wraps

import httpx
import pytest
from pydantic import ValidationError

from accord.config import Settings
from accord.extraction_worker import propose
from accord import gemini


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return wrapped


def setup_provider(monkeypatch, replies):
    sent = []
    original = httpx.AsyncClient
    async def handler(request):
        sent.append(request)
        reply = replies[min(len(sent)-1, len(replies)-1)]
        return httpx.Response(request=request, **reply)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    config = Settings(database_url="postgresql://unused", extraction_provider="gemini", gemini_api_key="test-key-only")
    doc = {"id":"document-1","version":1,"kind":"certificate"}
    parsed = {"parser":"test-parser","spans":[{"id":"p1l1","text":"PO: PO-001"}]}
    return config,doc,parsed,sent


def completed(proposal=None):
    return {"id":"response-test","model":"gemini-3.8-flash","status":"completed",
            "steps":[{"type":"model_output","content":[{"type":"text","text":json.dumps(
                proposal or {"status":"missing","findings":[]})}]}],"usage":{"total_tokens":10}}


@run_async
async def test_gemini_compressed_http_body_is_decoded_only_once(monkeypatch):
    compressed=gzip.compress(json.dumps(completed()).encode())
    c,d,p,_=setup_provider(monkeypatch,[{"status_code":200,"content":compressed,"headers":{"Content-Encoding":"gzip"}}])
    extraction=await propose(c,d,p)
    assert extraction["proposal"] == {"status":"missing","findings":[]}


@run_async
async def test_gemini_thought_metadata_is_not_an_evidence_value(monkeypatch):
    result=completed()
    result["steps"].insert(0,{"type":"thought","signature":"test-signature","summary":[{"type":"text","text":"316L"}]})
    c,d,p,_=setup_provider(monkeypatch,[{"status_code":200,"json":result}])
    extraction=await propose(c,d,p)
    assert extraction["proposal"] == {"status":"missing","findings":[]}
    assert extraction["provider_output"] == result["steps"]


@run_async
async def test_gemini_records_provider_output_and_uses_no_tools(monkeypatch):
    result = completed()
    c,d,p,sent = setup_provider(monkeypatch,[{"status_code":200,"json":result}])
    extraction = await propose(c,d,p)
    body = json.loads(sent[0].content)
    assert str(sent[0].url) == gemini.ENDPOINT
    assert sent[0].headers["x-goog-api-key"] == "test-key-only"
    assert body["store"] is False and "tools" not in body
    assert json.loads(body["input"])["document_id"] == d["id"]
    assert body["response_format"]["schema"]["additionalProperties"] is False
    assert extraction["provider_output"] == result["steps"]
    assert extraction["versions"]["provider"] == "gemini"
    assert extraction["versions"]["model"] == result["model"]
    assert extraction["response_id"] == result["id"]


@run_async
@pytest.mark.parametrize("change", ["incomplete","refusal","tool","empty","multiple","malformed","extra"])
async def test_gemini_fails_closed(monkeypatch,change):
    result = completed()
    if change == "incomplete": result["status"] = "in_progress"
    if change == "refusal": result["steps"][0]["content"] = [{"type":"refusal","text":"no"}]
    if change == "tool": result["steps"].append({"type":"function_call"})
    if change == "empty": result["steps"] = []
    if change == "multiple": result["steps"] *= 2
    if change == "malformed": result["steps"][0]["content"][0]["text"] = "not JSON"
    if change == "extra": result = completed({"status":"missing","findings":[],"capture":True})
    c,d,p,_ = setup_provider(monkeypatch,[{"status_code":200,"json":result}])
    with pytest.raises((ValueError,ValidationError)):
        await propose(c,d,p)


@run_async
async def test_gemini_rate_limit_retry_bounded_and_payload_unchanged(monkeypatch):
    c,d,p,sent = setup_provider(monkeypatch,[{"status_code":429,"headers":{"Retry-After":"2"}},
                                          {"status_code":200,"json":completed()}])
    sleeps = []
    async def sleep(delay): sleeps.append(delay)
    monkeypatch.setattr(gemini.asyncio,"sleep",sleep)
    result = await propose(c,d,p)
    assert len(sent) == 2 and sent[0].content == sent[1].content
    assert 2 <= sleeps[0] <= 10 and result["http_attempts"] == 2


@run_async
async def test_gemini_long_cooldown_does_not_wait_past_lease(monkeypatch):
    c,d,p,sent = setup_provider(monkeypatch,[{"status_code":429,"headers":{"Retry-After":"120"}}])
    with pytest.raises(httpx.HTTPStatusError): await propose(c,d,p)
    assert len(sent) == 1


@run_async
async def test_gemini_response_limit(monkeypatch):
    c,d,p,_ = setup_provider(monkeypatch,[{"status_code":200,"content":b"x"*(gemini.MAX_RESPONSE_BYTES+1)}])
    with pytest.raises(ValueError,match="byte limit"): await propose(c,d,p)


@run_async
async def test_gemini_missing_key_cannot_fall_back_to_openai(monkeypatch):
    c,d,p,sent = setup_provider(monkeypatch,[{"status_code":200,"json":completed()}])
    c.gemini_api_key=""; c.openai_api_key="must-not-use"
    with pytest.raises(ValueError,match="key unavailable"): await propose(c,d,p)
    assert not sent


@run_async
@pytest.mark.parametrize("field,value",[("findings",21),("span_ids",0),("span_ids",21)])
async def test_gemini_backend_still_enforces_array_limits(monkeypatch,field,value):
    finding={"document_id":"document-1","document_version":1,"field":"delivered_grade",
             "purchase_order":"PO-001","lot":"LOT-001","value":"316L","span_ids":["p1l1"],"quote":"PO: PO-001"}
    proposal={"status":"found","findings":[finding]}
    if field=="findings": proposal["findings"]=[finding]*value
    else: finding["span_ids"]=["p1l1"]*value
    c,d,p,_=setup_provider(monkeypatch,[{"status_code":200,"json":completed(proposal)}])
    with pytest.raises(ValidationError): await propose(c,d,p)
