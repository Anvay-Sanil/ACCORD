# ACCORD — Phase 3 payment foundation

**ACCORD checks delivery paperwork against approved requirements before a supplier captures a PayPal payment.**

This package contains the backend, migrations, payment worker, extraction worker, webhook handler, and executable tests. It is a Sandbox-only implementation for review. The React checkout/review interface, deployment, submission materials, and real-provider acceptance are outside this delivered phase. See `VERIFICATION.md` for what was actually exercised.

## Sentinel review amendments

The revised payment foundation passes **71 local tests using real PostgreSQL**. Seven failure scenarios in the original implementation were reproduced and corrected. See `SENTINEL_REVIEW.md`, `sentinel.patch`, and `VERIFICATION.md` for the findings, complete diff, and verification boundaries.

- Verified webhooks remain pending while a matching deal has an active worker; an older reader cannot consume the event's only wakeup.
- Refund resources may correlate through a validated Sandbox capture `up` link. The code extracts its ID and constructs a fixed-base resource request; it never fetches a webhook-supplied URL or mistakes a refund ID for a capture ID.
- Pending authorizations remain in `AUTHORIZATION_PENDING` / operation `PENDING` and continue read-only polling. Denied authorizations are recorded as `AUTHORIZATION_DENIED` / operation `FAILED`.
- New capture operations require confirmed available authorization state. Before a capture POST, canonical authorization state is read. Voided/denied authorizations block an unsent capture; pending ones wait. If an authorization is consumed while the capture is not visible, retain reconciliation and send no blind capture retry.
- Confirmed full refunds do not regress to partial refunds on an older observation.
- Buyer interpretation review cannot replace a recognized delivered grade or receipt value with a different value. This check applies at review submission and again when evaluating stored reviews. Corrected evidence and explicit supersession remain necessary. The evidence rule version is `bounded-layouts-v2`; the field form also recognizes an optional `Required grade` line between lot and delivered grade.

No migration changes are needed for these corrections. Provider keys, frozen requests, capture/void exclusion, commercial amounts, and session boundaries retain the existing implementation contract. Existing review records remain append-only and are evaluated under the updated rule version.

## Commercial and authority boundary

One buyer, one supplier acting as the PayPal merchant, USD 750.00, one authorization, and one final capture. The supplier owns the Sandbox business account and REST application. Its application credentials stay on the server; ACCORD is not a marketplace intermediary or an escrow service.

The immutable policy includes the complete mandate in `accord/contract.py`. Both authenticated parties approve the same policy hash. The supplier authorizes conditional capture using its merchant application. The buyer separately approves the purchase in PayPal. Application consent never substitutes for PayPal buyer approval. The backend checks the order's merchant, buyer payer ID, amount, currency, and deal reference before authorization.

The fulfillment promise is **capture initiation within 48 hours of PayPal's authorization `create_time`**. A delayed local observation does not extend it. The worker uses a 30-second dispatch margin; the HTTP adapter checks the persisted deadline again after obtaining OAuth. Pending settlement may resolve later. Passing evidence never guarantees provider success.

## Source map

| File | Responsibility |
| --- | --- |
| `accord/contract.py`, `seed.py` | Frozen mandate, approved requirements, operator bootstrap |
| `accord/api.py` | Session/CSRF/role/deal enforcement, document ingestion, verified webhook inbox |
| `accord/coordinator.py` | One durable operation per deal and kind, shared deal lock, decision invalidation |
| `accord/worker.py` | Lease fencing, dispatch journal, recovery, deadlines, webhook wakeups |
| `accord/paypal.py` | Sandbox HTTP, token reuse, timeouts, rate limits, signature postback |
| `accord/evidence.py` | Executable citation and semantic admissibility; frozen policy evaluation |
| `accord/parser.py`, `pdf_child.py` | Constrained PDF subprocess |
| `accord/extraction_worker.py` | Independent parsing/model queue, versioned extraction history |
| `migrations/versions/0001_foundation.py` | Operations, evidence, audit, immutable-field triggers |
| `migrations/versions/0002_recovery_and_extraction_history.py` | Read-failure counters and append-only extraction runs |
| `accord/provision_db.py` | Migrations and non-owner runtime database grants |
| `tests/` | PostgreSQL integration tests, ingress, provider faults, adversarial evidence |

## Local setup

Requirements: Python 3.12 and PostgreSQL 17, or Docker Compose. Use a private working directory. Copy `.env.example` to `.env`; every value marked `replace-...` is a placeholder. No credential is included. Use separate random URL-safe database passwords so the Compose connection strings remain valid.

For a local HTTP-only session, set `APP_ORIGIN=http://localhost:8000`, `SESSION_SECURE=false`, and `PUBLIC_DEPLOYMENT=false`. This exception is only for a loopback development server. For an HTTPS deployment set the exact HTTPS origin, `SESSION_SECURE=true`, and `PUBLIC_DEPLOYMENT=true`. The supplied Compose configuration binds the API to loopback and does not configure a public reverse proxy or TLS.

### Compose route

These commands are provided for deployment testing; Docker execution was not verified in this workspace.

```powershell
Copy-Item .env.example .env
# Edit .env: passwords, origin/cookies, supplier Sandbox credentials, webhook ID.
docker compose up -d --build db migrate api payments extraction
docker compose logs migrate
docker compose --profile tools run --rm seed --buyer-payer-id YOUR_SANDBOX_BUYER_PAYER_ID
```

The seed command prompts for buyer and supplier application passwords (at least 12 characters), then prints the deal ID, policy, and policy hash. It creates no PayPal transaction and grants no approval. Seed only once for these usernames; use the CLI username options for another fixture deal. Keep the schema-owner credential only in migration/seed tooling; web and workers use `accord_app`.

### Native Python route

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
Copy-Item .env.example .env
# Edit .env. For provisioning set DATABASE_URL to a dedicated schema-owner URL.
# APP_DB_PASSWORD must be at least 16 characters.
$env:DATABASE_URL = 'postgresql://OWNER:URL_SAFE_PASSWORD@localhost:5432/accord'
$env:APP_DB_PASSWORD = 'REPLACE_WITH_A_RANDOM_URL_SAFE_PASSWORD'
.\.venv\Scripts\python.exe -m accord.provision_db
.\.venv\Scripts\python.exe -m accord.seed --buyer-payer-id YOUR_SANDBOX_BUYER_PAYER_ID
Remove-Item Env:DATABASE_URL
Remove-Item Env:APP_DB_PASSWORD
# Now set DATABASE_URL in .env to the accord_app credential, not the owner.
.\.venv\Scripts\python.exe -m uvicorn accord.api:create_app --factory --host 127.0.0.1 --port 8000
```

In two additional terminals, from this directory with the same runtime `.env`:

```powershell
.\.venv\Scripts\python.exe -m accord.worker
```

```powershell
.\.venv\Scripts\python.exe -m accord.extraction_worker
```

The extraction service should receive only its database/model credentials. Compose enforces that environment separation. A native developer `.env` is shared, but the PDF child itself receives a scrubbed environment. Do not place the payment loop in the API or extraction process.

### Test route

Tests intentionally reset the `public` schema. Use a dedicated disposable database whose name contains `accord_test`; never point them at a working deal database. Tests require an owner credential and role-creation permission for the grant test.

```powershell
$env:TEST_DATABASE_URL = 'postgresql://TEST_OWNER:URL_SAFE_PASSWORD@localhost:5432/accord_test'
.\.venv\Scripts\python.exe -m pytest -q --junitxml=test-results.xml
```

Compose equivalent, after starting `db`:

```powershell
docker compose exec db createdb -U accord_admin accord_test
docker compose --profile tools run --rm test
```

Do not run these tests concurrently against the same database. The test uses a separate `accord_test_runtime` role so it does not rotate the application's role password. Its password is ephemeral and does not belong in an application `.env`. `requirements.lock` freezes the dependency versions used for the recorded run. This is a backend correctness suite using a named PayPal test double, not a PayPal certification or model-quality evaluation.

## Payment operation contract

Every financial operation persists a UUID, exact canonical request bytes and hash, endpoint, unique `PayPal-Request-Id`, first dispatch time, retry deadline, provider reference, attempt journal, and state. `(deal_id, kind)` is unique. Triggers prevent request changes and prevent changing the decision/deadline after first dispatch. A lost transaction commit cannot cause an unjournaled application dispatch: HTTP follows the committed preparation transaction.

| Operation | Recovery and permitted retry |
| --- | --- |
| CREATE order | Recover a known order by GET. Without an order ID, retry only the original key and identical payload. Stop POSTs at first dispatch + 5h55m; an unresolved unknown order remains reconciling for operator investigation. Never invent a replacement key. |
| AUTHORIZE order | Read the known order, then its authorization resource. Check buyer/merchant/amount/deal binding. Stop POSTs at first dispatch + 5h55m, before the documented six-hour key-retention window. Continue read-only recovery afterward. |
| CAPTURE authorization | Read a known capture ID. Otherwise read the known order's captures, or discover an ID from a correlated verified event and GET the canonical capture. Retry only the persisted key/body, with the original decision and still-valid evidence. The local retry budget is five minutes from first dispatch, capped by the authorization-based 48-hour deadline; afterward use reads only. |
| VOID authorization | Read the authorization; `VOIDED` or `EXPIRED` confirms resolution. The endpoint accepts a request key, but this build does not assume an undocumented retention window: after first dispatch it performs read-only recovery, without another void POST. |

Orders creation/authorization have a documented six-hour key-retention window. The capture reference accepts the idempotency header, and PayPal's idempotency guide explicitly describes retrying a timed-out capture with its original key. This build uses a short five-minute capture retry budget; that local limit is not a claim of a provider retention guarantee. PayPal's general guidance says keys may be stored for up to 45 days, which is not treated here as an endpoint-specific minimum. Confirm the merchant integration's behavior during Sandbox acceptance. [Orders authorization](https://developer.paypal.com/sdk/orders/v2/orders-authorize/), [Capture reference](https://developer.paypal.com/api/payments/v2/authorizations-capture), [PayPal idempotency](https://developer.paypal.com/api/rest/reference/idempotency/), [Request headers](https://developer.paypal.com/api/make-api-requests/).

All operations acquire the same deal row for claims and writes; no transaction stays open across HTTP. A 90-second lease and increasing generation fence state updates. An expired active operation is reconciled before another operation kind. **A lease cannot stop an already-running process from sending HTTP.** Accordingly, this implementation forbids an automatic void after *any* capture dispatch, even if a later authorization read still says `CREATED`. Such a void requires manual resolution outside this MVP. It favors unresolved state over a conflicting financial action.

Repeated and concurrent capture submissions return the same operation. If queued evidence becomes invalid, that operation is blocked. A fresh passing decision can replace its decision reference with an audit entry only before first dispatch. After dispatch, the original reference is frozen; changed evidence prevents further capture POSTs and permits read-only reconciliation. Reviewed interpretations also invalidate older decisions.

`QUEUED` → `IN_FLIGHT` → `SUCCEEDED`, `PENDING`, or `RECONCILING`. `BLOCKED` means unmet evidence/deadline/cancellation constraints before dispatch. `MANUAL_REVIEW` denotes a validation/conflict condition requiring investigation. `CAPTURE_PENDING` is not paid. An uncertain capture remains reconciling until observed; a failed refresh preserves a previously confirmed financial state while the operation shows reconciliation separately. Verified events wake canonical reads and never independently mark payment complete. Refund/reversal observation is supported; initiating refunds is outside this MVP.

## Executable evidence boundary

Each finding binds `document_id`, immutable version and SHA-256, field meaning, purchase order, lot, value, exact parser span IDs, and exact quoted text. The stored extraction includes parser/schema/prompt/model versions and prompt hash; decisions retain the rule version, findings, extraction record, and any buyer interpretation review. Completed or failed accepted worker runs have append-only history. Stale generations cannot overwrite the document's result.

Automatic admission supports native-text PDFs with exactly one association in one of three certificate forms:

```text
PO: PO-001
Lot: LOT-001
Delivered grade: 316L
```

```text
PO | Lot | Delivered grade
PO-001 | LOT-001 | 316L
```

```text
For purchase order PO-001, lot LOT-001, the delivered material grade is 316L.
```

Receipt form: `PO: PO-001`, `Lot: LOT-001`, `Received: YES`, each on its own line. Recognized headers are enumerated in `evidence.py`. A required-grade label is allowed only when it introduces no competing grade. The entire extracted document must fit the bounded grammar: unsupported prose, mixed lots/POs, competing values, negation, instructions, multiple associations, or absent evidence require review. A valid citation containing required 316L and delivered 304 cannot automatically pass as delivered 316L.

The model proposes findings; the backend does not trust its `found`/`conflicting` label. A buyer may explicitly review an interpretation with exact cited context and a reason; the backend still enforces field, version, PO, lot, and the frozen required value. Review is a human assertion, not proof that the material or certificate is truthful. A different delivered grade cannot change the frozen policy. Failed parsing requires a usable replacement document; a review cannot cite text that was never parsed.

Exactly one active certificate and one active receipt are required. New uploads do not silently replace earlier evidence. The buyer explicitly supersedes an old document after reviewing its replacement. Buyer delivery acknowledgment is separate. All these changes invalidate queued decisions. Authenticated upload establishes provenance, not authenticity of the certificate's contents.

## Ingress and resource controls

- Opaque server-side sessions; Argon2 password hashes; `HttpOnly`, explicit `SameSite=Lax`, and configurable `Secure` cookies. Public configuration requires `Secure` and HTTPS.
- Exact Origin validation for login; Origin plus a session-bound CSRF token for browser mutations. Deal membership and actor role checks cover documents, approvals, reviews, and financial operations. No mutation uses GET.
- Bounded request bodies and ingress time; 5 MB PDFs, ten pages, 100,000 extracted characters, 2,000 spans, and 32 documents per deal.
- PDF parser runs in a separate subprocess with a 512 MB address-space/process-memory bound, five CPU seconds, and an eight-second wall timeout. Linux uses resource limits; Windows uses a Job object. It fails closed if limits cannot be installed. The child has no inherited database/PayPal/model secrets. These are resource constraints, not a claim of a complete malicious-code sandbox.
- Extraction runs separately from payment reconciliation. Compose also gives extraction a separate 768 MB/one-CPU service budget.
- PayPal HTTP: connect 3s, read 10s, write 5s, pool 2s; each HTTP exchange has a 20s wall bound and a 1 MB response bound. OAuth tokens are cached until expiry minus 60 seconds. A 401 clears the token for a later durable attempt. No hidden financial POST retries.
- Exponential backoff with jitter counts read failures as well as dispatch failures. `Retry-After` is a lower bound; its cooldown cannot authorize a POST beyond the persisted deadline. Events reduce unnecessary polling. [PayPal rate limiting](https://developer.paypal.com/api/rest/reference/rate-limiting/).
- Fault injection is an environment-only local hook that drops a successful capture response. There is no browser fault endpoint. Compose forces it off, and public configuration rejects it.

## API integration sequence

`/docs` describes request schemas. A future browser client must keep the returned CSRF token in memory and send it as `X-CSRF-Token`; the session cookie remains HttpOnly. Normal browser fetch supplies the matching Origin. Do not put secrets in a browser client.

1. Buyer and supplier independently `POST /api/login` with application credentials. `GET /api/deals/{id}` returns the frozen policy, its hash, and operation states.
2. Each actor approves that exact hash through `POST /api/deals/{id}/approve`.
3. Buyer `POST /api/deals/{id}/orders`; poll the deal until its persisted `order_id` appears.
4. A PayPal JS SDK checkout configured for `intent=authorize&currency=USD` must approve that existing order. Its `createOrder` returns the backend's persisted order ID; its `onApprove` calls the backend `/authorize` route. Do not authorize a second time in browser SDK code. The visual checkout harness is not included in this phase.
5. Buyer `POST /api/deals/{id}/authorize`. The worker validates PayPal approval and stores the authorization timestamp/deadline.
6. Supplier uploads raw `application/pdf` bytes to `POST /api/deals/{id}/documents?kind=certificate`; buyer uploads `kind=receipt`. Read `/documents/{document_id}/findings` for exact spans and extraction provenance.
7. Buyer acknowledges physical delivery via `/delivery-ack`; if needed, reviews an interpretation via `/review` or supersedes via `/supersede`. After changes, call `/evaluate`.
8. A passing evaluation queues the single capture. An extraction-worker evaluation can also queue it under the existing mandate. `/capture` is a supplier-only idempotent entrypoint, subject to the same policy check.
9. Read the deal's payment state and operation state together. Supplier `/void` requests cancellation; an already-dispatched capture remains protected from conflicting automatic void.

The document download, findings endpoint, and deal view are access controlled. There are no public document URLs. The model key is optional for local recovery testing; without it, documents become review-required instead of receiving fabricated AI findings.

## Real Sandbox acceptance — not yet executed

1. Provision a supplier Sandbox business account and REST app. Confirm `PAYPAL_MERCHANT_ID` belongs to that supplier. Provision a separate Sandbox buyer and record its payer ID. Use the supplier app credentials on the server.
2. Register the reachable HTTPS `/api/webhooks/paypal` endpoint on that same app and set its webhook ID. Subscribe to authorization CREATED/VOIDED and capture PENDING/COMPLETED/DECLINED/REFUNDED/REVERSED events supported by the account.
3. Perform actual Sandbox buyer approval and authorization through a checkout harness. Preserve redacted order/authorization IDs, creation time, local deadline, approved policy hash, and provider responses.
4. Submit a required-316L/delivered-304 certificate. Confirm review-required and zero capture dispatches. Submit a corrected certificate in another supported layout, explicitly supersede the prior one, add the buyer receipt/acknowledgment, and evaluate. Preserve the exact finding, cited passage, and decision ID.
5. Confirm one actual Sandbox capture, matching amount/merchant, and a verified real-transaction webhook in the durable inbox. The postback verification method does **not** support simulator-generated events. [Simulator limitation](https://developer.paypal.com/api/rest/webhooks/simulator/).
6. Repeat and concurrently submit capture requests. Compare the unique operation/key, dispatch journal, canonical provider state, and merchant activity: there must be one capture resource for this deal.
7. In an isolated local, non-public deployment, enable `FAULT_DROP_CAPTURE_RESPONSE=true`, restart only the payment worker, and repeat with a fresh deal. Confirm reconciling after the dropped response and recovery of the same capture ID. Disable the hook afterward. Compose intentionally does not pass this flag through.
8. Exercise missing authorization responses, key-deadline expiry, stale worker suspension/resumption, capture/void races, invalidated queued evidence, and pending captures. Local tests demonstrate the coordinator paths; record provider behavior separately wherever Sandbox permits the scenario. Never turn an unavailable Sandbox fault scenario into a claimed pass.
9. Record real model outputs for varied documents and adversarial fixtures, including required-vs-delivered, mixed lots, negation, contradictions, and embedded instructions. Report both model proposal correctness and backend admissibility. The deterministic fixture suite alone is not evidence of useful real-model extraction.

Authorization capture success is highest in the initial honor period; this build's 48-hour initiation limit is inside that period but does not guarantee success. [Delayed capture](https://developer.paypal.com/checkout/delay-capture/).

Phase 3 acceptance remains conditional on these real-provider and deployment checks. No live-money endpoint is enabled in this build.
