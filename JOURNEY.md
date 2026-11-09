# Runnable buyer/supplier journey

Phase 3 checkpoint only. The accepted 71-test foundation is preserved and extended. The current review is [SENTINEL_JOURNEY_REVIEW.md](SENTINEL_JOURNEY_REVIEW.md); the builder's historical handoff is [JOURNEY_ACCEPTANCE.md](JOURNEY_ACCEPTANCE.md); it distinguishes real provider execution from test doubles. Phase 4 is unopened.

## What is implemented

FastAPI serves a responsive, buildless interface at `/`. Buyer and supplier use separate application accounts. Both approve the same immutable mandate. The buyer prepares the existing PayPal order and approves it through the Sandbox JavaScript SDK v6; its callback requests backend authorization. No browser code receives a PayPal secret or directly captures payment.

The supplier uploads a certificate, the buyer uploads a receipt and acknowledges delivery. The interface shows the approved requirement, delivered value, document version/hash, PO/lot, exact parser spans, extraction versions and current payment state. The buyer must explicitly inspect a replacement before superseding an earlier document. Successful evaluation can immediately queue capture under the approved mandate. Missing/conflicting evidence blocks capture; a buyer cannot relabel a recognized delivered 304 as 316L.

Payment operations and the audit trail remain visible separately from the evidence decision. An uncertain capture displays **Reconciling**. Repeated capture requests reuse the existing operation. The server uses role/deal checks, origin checks and CSRF tokens; sessions use HttpOnly and SameSite=Lax, with Secure required for public deployment. Uploaded PDFs download as attachments with a sandbox CSP.

## Configuration and startup

Use the Python/PostgreSQL or Docker provisioning instructions in [README.md](README.md). Never replace an existing `.env` with `.env.example`. The latter contains configuration names and placeholders only. Dependencies are pinned in `requirements.lock`; the browser harness also uses `requirements.acceptance.txt`.

For an existing database, add these **operator-only** values to the local `.env`:

```dotenv
# Schema-owner connection for migrations/account creation; keep runtime DATABASE_URL restricted.
ACCORD_OWNER_DATABASE_URL=postgresql://OWNER:REPLACE@HOST:5432/DATABASE
BUYER_PAYER_ID=REPLACE_WITH_SANDBOX_BUYER_ACCOUNT_ID
# PAYPAL_BUYER_PAYER_ID is also accepted as an alias.
ACCORD_BUYER_USERNAME=buyer
ACCORD_SUPPLIER_USERNAME=supplier
ACCORD_BUYER_PASSWORD=REPLACE_WITH_AT_LEAST_12_CHARACTERS
ACCORD_SUPPLIER_PASSWORD=REPLACE_WITH_AT_LEAST_12_CHARACTERS
```

From the project directory in the configured Python environment:

```powershell
python -m acceptance.preflight
python -m acceptance.bootstrap --migrate
python -m acceptance.fixtures --output acceptance/fixtures
python -m uvicorn accord.api:create_app --factory --host 127.0.0.1 --port 8000
```

Run `python -m accord.worker` and `python -m accord.extraction_worker` in separate terminals. Bootstrap reuses a matching existing deal and never resets passwords, approves requirements, or contacts PayPal. It writes the deal ID and policy hash to `acceptance-record/bootstrap.json`. Migration **0003** retains webhook verification inputs; it must be applied before this API handles events.

Use the exact configured `APP_ORIGIN`. For a private loopback HTTP session use `PUBLIC_DEPLOYMENT=false` and `SESSION_SECURE=false`; a public origin requires HTTPS and Secure cookies. A local reverse proxy or your existing deployment must route the origin to the API. Configure the supplier application's registered webhook URL as `APP_ORIGIN/api/webhooks/paypal`. Owner credentials and application-account passwords must stay out of the web/worker deployment environment. The supplied Compose services already separate runtime, extraction and provisioning credentials.

## Browser sequence

1. Sign in as supplier and approve the mandate. Use a separate browser profile/context for the buyer and approve the same mandate.
2. As buyer, prepare checkout and approve the **$750.00 authorization** in the real PayPal Sandbox popup. Wait for `Authorized · not captured` before submitting evidence. If the popup closes after approval, the recheck button reads the same order.
3. Upload `wrong-grade.pdf` as supplier and `receipt.pdf` as buyer. Acknowledge receipt and inspect the required-versus-delivered passages. Capture must remain blocked; reviewing delivered 304 as 316L must fail.
4. Upload `valid-table.pdf` as supplier. The buyer selects it, checks its PO/lot and delivered 316L passage, checks the review box, and accepts the replacement. This supersedes the wrong certificate and evaluates the current evidence. Observe capture or reconciliation separately from the passing decision.
5. Repeat the supplier capture request. After confirmed capture, run the authenticated repeat/concurrency check below. Inspect the provider record and verified webhook.

These fixture documents are synthetic. An authenticated upload proves who uploaded the file, not that its contents are truthful.

## Executable acceptance commands

### Browser, actual PostgreSQL, explicit provider/model doubles

```powershell
python -m pip install -r requirements.acceptance.txt
python -m playwright install chromium
$env:TEST_DATABASE_URL = 'postgresql://TEST_OWNER:REPLACE@localhost:5432/accord_test'
python -m acceptance.browser_proof --output acceptance-record/browser-final
python -m acceptance.process_recovery
```

Each harness creates its own `accord_test_*` database and does not reset an application database. The account needs CREATE DATABASE. Databases are retained for inspection. The browser harness drives real Chromium and the real PDF parser, screenshots wrong evidence, uncertain capture and the corrected result, and records browser video paths. PayPal REST, SDK approval and model interpretation are explicitly doubled, with `LOCAL TEST DOUBLES` displayed in the UI. No public fake or fault routes are added.

The recovery command starts a separate worker process, persists a dispatch, simulates provider completion, and exits abruptly before recording the response. It waits for the actual 90-second lease to expire and starts a new process. The replacement must recover through reads with the same key/payload and zero additional capture POSTs. This is actual process/DB recovery with a provider double.

### Real model

```powershell
python -m acceptance.model_proof
```

The explicitly selected model processes ten PDFs spanning field, table and narrative layouts, required-versus-delivered grade, mixed lots, negation, contradiction, missing delivery grade and embedded instructions. See [GEMINI_SETUP.md](GEMINI_SETUP.md) for the tested free Gemini option. Records retain the provider response ID when returned (Gemini may omit it with storage disabled), provider output, usage, parser/schema/prompt/model versions, source hash/spans, citation checks and backend decision. Authentication or quota errors stop the run with BLOCKED; no fabricated extraction substitutes for them. Honor the provider's cooldown before retrying an exhausted model.

### Real PayPal

```powershell
python -m acceptance.duplicate_proof --deal-id YOUR_DEAL_UUID
python -m acceptance.paypal_proof --deal-id YOUR_DEAL_UUID
```

The duplicate check requires an already confirmed capture and submits two sequential and four concurrent requests using a real authenticated supplier application session. It never creates a second deal/order.

The recorder only reads real Sandbox order/authorization/capture resources and re-verifies retained signature inputs through PayPal. It checks one USD 750 capture, buyer/merchant/deal associations, provider authorization time plus 48 hours, frozen request key/hash, audited repeats, a recorded uncertain response and subsequent recovery, and a processed real completed-capture webhook. OAuth success or database state alone cannot produce PASS. It exports identifiers, hashes and verification results, excluding credentials and customer contact details. Original verification inputs remain in the protected database.

For the controlled lost-response proof, use a **private acceptance instance** before its single capture, with `PUBLIC_DEPLOYMENT=false` and the server-only `FAULT_DROP_CAPTURE_RESPONSE=true` setting. The adapter sends the real request, then discards its successful response once; the worker must reconcile the uncertain result. Preserve its database, operation key and payload. Disable the fault afterward. Never enable it in a public deployment; configuration rejects that combination and normal Compose forces it off. A simulator event cannot satisfy postback signature verification.

### Docker/Linux

```powershell
python -m acceptance.docker_run --run
```

This runs the isolated `compose.acceptance.yaml` stack with PostgreSQL 17, a Linux Python/Chromium runner, migrations, the regression suite, direct 512 MB memory/five-second CPU/wall-time probes, the abrupt process recovery, and the browser journey. No real PayPal/OpenAI credentials enter this stack. The runner writes Compose logs and individual execution records under `acceptance-record/linux`. The Docker gate cannot pass when the engine is inaccessible or the commands are merely provided. The regular `compose.yaml` remains the separate application deployment recipe.

## Source changes and records

| Area | Files |
| --- | --- |
| Browser journey | `accord/web/index.html`, `style.css`, `app.js` |
| Scoped workspace, static UI, retained verification inputs | `accord/api.py`, migration `0003_webhook_acceptance_evidence.py` |
| Repeat audit and model output retention | `accord/coordinator.py`, `extraction_worker.py` |
| Real-provider/configuration records | `acceptance/preflight.py`, `bootstrap.py`, `model_proof.py`, `duplicate_proof.py`, `paypal_proof.py` |
| Browser and process execution | `acceptance/browser_proof.py`, `process_recovery.py`, `fixtures.py` |
| Linux execution | `compose.acceptance.yaml`, `acceptance/Dockerfile`, `docker_run.py`, `linux_proof.py`, `parser_limits.py` |
| Additional regression checks | `tests/test_journey.py`, `tests/test_acceptance_records.py` |

Tests disable `.env` loading and clear provider settings so a developer's live configuration cannot contaminate fake-provider regressions. Migration tooling only loads `.env` when DATABASE_URL is not already supplied. Secret-bearing `.env` files, caches and acceptance output directories are excluded from images/source control. The delivery ZIP includes only selected, reviewed acceptance records.

Implementation references: [PayPal JavaScript SDK v6](https://developer.paypal.com/sdk/js/reference/), [Orders authorization](https://developer.paypal.com/sdk/orders/v2/orders-authorize/), [PayPal idempotency](https://developer.paypal.com/api/rest/reference/idempotency/), [webhook simulator limitations](https://developer.paypal.com/api/rest/webhooks/simulator/), [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
