# Gemini extraction option

This is an optional model adapter for the Phase 3 evidence worker. The financial
coordinator, evidence grammar, schema and prompt remain unchanged. No migration
is required. Select the provider explicitly; failures never trigger a fallback.

Use a Google AI Studio project on the Free tier with billing disabled. Add these
settings to a private `.env` or the extraction service's environment:

```dotenv
EXTRACTION_PROVIDER=gemini
GEMINI_API_KEY=REPLACE_LOCALLY_WITH_YOUR_KEY
GEMINI_MODEL=gemini-3.5-flash-lite
```

Do not give the model credential to the browser or payment worker. Do not commit
the private file. The supplied Compose extraction service passes only its
database and model configuration. The existing OpenAI path is retained when
`EXTRACTION_PROVIDER=openai`.

The adapter calls Google's fixed Interactions endpoint with structured JSON,
no tools, and `store=false`. The backend still validates every citation and
document/field/PO/lot association. HTTP responses are bounded to 1 MB and the
whole model exchange to 50 seconds. At most one retry for 429/503 is permitted,
with jitter and respect for Retry-After; a long cooldown becomes review-required.
The parser plus model deadline fits inside the existing 90-second document lease.

Extraction records include provider, requested/returned model, adapter, parser,
schema, prompt version/hash, response ID, provider output, usage, and HTTP attempt
count. Incomplete, malformed, unexpected-tool and refusal responses fail closed.

Direct API diagnostics rejected nested array cardinality constraints. The
generation schema therefore inlines definitions and omits array min/max bounds;
all field types, enums, required fields and extra-property restrictions remain.
The unchanged full Proposal schema enforces the omitted bounds locally before
any finding can reach evidence evaluation. The requested schema hash is recorded
alongside the canonical schema version. Tests cover empty/oversized citations
and oversized finding lists so provider compatibility cannot loosen admission.

Run `python -m acceptance.model_proof` for ten synthetic fixtures. The resulting
record distinguishes model value correctness, exact citations, and deterministic
admission. Local adapter tests use an explicit HTTP double and cannot satisfy
this real-model gate.

Free-tier quotas and availability are project dependent. `store=false` does not
change Google's Free-tier data-use terms; use synthetic acceptance documents.
No billing enablement or paid fallback is performed by this code.

Gemini 3.5 Flash-Lite passed all ten real synthetic fixtures on 19 October 2026
(Asia/Calcutta). Gemini 3.8 Flash's partial run and daily-quota rejection are
preserved in the acceptance record; they are not presented as a complete pass.

Official references checked 19 October 2026:
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/api/interactions-api
