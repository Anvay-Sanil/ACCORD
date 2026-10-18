# ACCORD journey acceptance record

> Historical builder handoff, retained as submitted. The current Sentinel review and environment status are in [SENTINEL_JOURNEY_REVIEW.md](SENTINEL_JOURNEY_REVIEW.md).

**Disposition: Phase 3 remains conditional. Phase 4 is unopened.**

Local execution proofs recorded on **13 October 2026**; external readiness rechecked on **18 October 2026 (Asia/Calcutta)**. The runnable implementation and execution scripts are delivered. The browser and local recovery proofs passed with explicit provider/model doubles. Real Sandbox payment, real model extraction and Docker/Linux acceptance are not established.

| Required proof | Result | Direct evidence |
| --- | --- | --- |
| Browser: wrong evidence blocks; reviewed replacement permits capture | **PASS with PayPal/model doubles** | [Browser run](acceptance-record/browser-final/browser-result.json), screenshots and recorded videos below. Real Chromium, PostgreSQL and PDF parsing; synthetic SDK/REST/model responses. |
| Real PayPal Sandbox: approval, $750 authorization/capture, verified webhook, duplicate/recovery | **BLOCKED** | [OAuth/configuration preflight](acceptance-record/preflight.json) passed OAuth and DB connectivity. [Payment recorder](acceptance-record/paypal/paypal-result.json) has no real deal/payment proof. Owner connection is missing; application DB is at migration 0002. |
| Real model: outputs, citations, adversarial evidence | **BLOCKED** | [Real model request](acceptance-record/model/model-result.json) returned HTTP 429, `credit_balance_exhausted`. No real model output or successful adversarial evaluation is claimed. |
| Docker/Linux: startup, migrations, recovery, parser limits | **BLOCKED** | [Docker probe](acceptance-record/linux/docker-result.json), [command output](acceptance-record/linux/docker-probe.log). Latest probe: Linux engine pipe is absent. No Linux build/run success is claimed. |

## Executed local proofs

**78 tests passed, zero failures/errors/skips**, using actual PostgreSQL 17 on Windows and Python 3.12. [JUnit record](acceptance-record/local-tests.xml). This comprises the accepted 71 regressions, four journey/ingress tests and three acceptance-recorder validation tests. One upstream Starlette/httpx deprecation warning remains; it did not fail execution.

The seven browser assertions directly exercised:

1. Separate buyer/supplier application sessions approved the same mandate; the SDK-double callback requested backend authorization of the existing order.
2. A certificate containing required 316L and delivered 304 was visible with exact passages; capture was disabled and no capture POST occurred.
3. Buyer review attempting to relabel delivered 304 as 316L was rejected.
4. A simulated lost capture response displayed **Reconciling**, without claiming payment completion.
5. Buyer review and explicit supersession with the table-layout 316L replacement permitted one final capture.
6. Repeated button clicks and concurrent browser requests returned one capture operation; there was exactly one provider-double capture POST. Recovery discovered the missing capture ID through reads.
7. The 390-pixel mobile viewport had no horizontal overflow; no JavaScript/worker errors were recorded.

Screenshots were visually inspected:

- [Wrong evidence blocks capture](acceptance-record/browser-final/01-wrong-evidence-blocked.png)
- [Uncertain capture remains reconciling](acceptance-record/browser-final/02-capture-outcome-uncertain.png)
- [Reviewed replacement and confirmed capture](acceptance-record/browser-final/02-reviewed-replacement-captured.png)
- [Mobile interface](acceptance-record/browser-final/03-mobile.png)

The [browser JSON](acceptance-record/browser-final/browser-result.json) identifies the buyer/supplier video files and all provider-double requests. Both roles' videos are included in the delivery ZIP. Every screen identifies `LOCAL TEST DOUBLES`.

**A separate process recovery run also passed:** the worker process exited with code 23 after a simulated provider completed capture, before persisting its response. A second worker could not take the live lease. After an actual approximately 90-second lease expiry, a fresh process recovered the capture through reads with the original operation key/hash and **zero replacement-worker POSTs**. See [process record](acceptance-record/process-recovery/process-result.json) and [explicit provider-double snapshot](acceptance-record/process-recovery/provider-double.json). This was Windows execution; it does not substitute for Linux or real PayPal recovery.

All project Python files compiled successfully. Database migration 0003 was exercised in the local test/browser/recovery databases. It remains unapplied to the restricted remote application database.

## Real-environment observations and remaining actions

- Real supplier-app **PayPal Sandbox OAuth succeeded**. This establishes usable OAuth credentials only. No real order, authorization, capture or verified transaction event was created by this acceptance run.
- The supplied `PAYPAL_BUYER_PAYER_ID` is accepted as an alias for `BUYER_PAYER_ID`. Missing application passwords were generated into the local `.env` without printing them; no existing account password was changed. Both application-password fields now pass presence checks.
- The configured Render database is reachable through a restricted runtime role, contains zero application deals at the recorded preflight, and is at migration 0002. Add the schema-owner connection as `ACCORD_OWNER_DATABASE_URL`, then run `python -m acceptance.bootstrap --migrate`. Runtime `DATABASE_URL` should remain restricted.
- The configured public origin returned `/health` 200 on 13 October; the resumed health request timed out. `/` and `/api/browser-config` still return 404 in the latest [HTTP readiness record](acceptance-record/readiness.json). Deploy this updated source and apply migration 0003 before real checkout. No public deployment was performed by this task.
- Restore OpenAI project credits or configure a funded API key locally, then run `python -m acceptance.model_proof`. The ten prepared fixtures include all required adversarial cases; a prepared fixture is not a successful real-model evaluation.
- Docker must be available to the process executing `python -m acceptance.docker_run --run`. The latest probe could read Docker configuration, but `dockerDesktopLinuxEngine` was absent. Earlier probes encountered access denial; a named-pipe permission request could not be represented by the permission tool. On resumption, automatic permission review granted network access but did not grant requested writes to Docker's runtime directories; it returned no reason. No engine start was attempted after that partial grant. The supplied Linux runner remains unexecuted; a ready Docker Desktop window alone does not establish this gate.
- Once the updated real instance is running, both parties approve the mandate and the buyer completes the real Sandbox popup. Follow [JOURNEY.md](JOURNEY.md), then run the authenticated duplicate check and real PayPal recorder. The private lost-response run must retain its existing key/payload and display reconciliation. Do not enable fault injection in a public deployment.

The real PayPal recorder re-reads actual provider resources and re-verifies stored signature inputs. It requires each payment, webhook, duplicate and uncertain-response check before emitting PASS. The model and Docker tools likewise cannot convert missing credentials, unavailable services or unexecuted scripts into a passing result.

## Delivery and retained state

Source and runnable instructions are in this directory and `JOURNEY.md`; the delivery ZIP contains a SHA-256 manifest. Secret-bearing `.env` files, database files, caches and obsolete browser-run videos are excluded. The accepted reviewed baseline ZIP is retained separately without alteration.

Local `accord_test_*` database files are retained for audit. Browser servers and their workers were stopped when their harnesses completed. No real payment worker was started. At the 18 October handoff, the local PostgreSQL server on port 55439 was not reachable. No destructive database/cache cleanup was attempted.
