# ACCORD journey: Sentinel review

**18 October 2026 — Phase 3 remains conditional. Phase 4 is unopened.**

The backend regression claim was independently reproduced: **78 passed, zero failures/errors/skips**, on isolated real PostgreSQL **17.10**, Windows and Python 3.12. The corrected browser journey passed **eight assertions** in actual Chromium, with real PostgreSQL/PDF parsing and explicit PayPal SDK/REST and model doubles. This establishes the local journey, not real payment or model acceptance.

## Required corrections, applied

1. **Document selection could display the wrong response (P2).** Two inspection requests could finish out of order. A controlled browser test selected the new `v3` certificate, released the older request afterward, and reproduced the interface returning to `v1`. `accord/web/app.js` now tracks inspection order, rejects stale responses and clears the prior passages/review controls while a newly selected document loads. The same controlled response sequence passed after the fix.
2. **The blocked-capture screenshot did not isolate wrong-grade evidence (P2).** The original harness could record the receipt while still queued and screenshot the certificate before its passages loaded. It now requires both documents READY, both mandates approved, a recorded authorization and delivery acknowledgement, then visibly asserts required 316L versus delivered 304, disabled capture and zero capture POSTs. It selects the replacement by its delivered value instead of assuming the first certificate row is current.

These corrections change the browser and its acceptance harness. The local database sample password in `.env.example` is also replaced with an explicit placeholder; the private `.env` is preserved. The production payment, evidence and reconciliation Python modules are unchanged. `journey-sentinel.patch` contains the exact two-file code diff against the submitted ZIP; the old sample password is excluded from that patch.

## Independent evidence

| Check | Result | Record in the full reviewed package |
| --- | --- | --- |
| Regression suite | 78 passed in 92.02 seconds; one upstream Starlette/httpx deprecation warning | `acceptance-record/sentinel/local-tests.xml` |
| Corrected browser journey | Eight assertions passed; no browser/worker errors recorded; one capture operation and one provider-double capture POST | `acceptance-record/sentinel/browser/browser-result.json` |
| Response-order defect | Reproduced before the UI fix; passed afterward | `acceptance-record/sentinel/browser-selection-race-before-fix.json` and the final browser record |
| Original delivery integrity | All 101 listed file hashes/sizes matched; 102 total archive files including the manifest | `DELIVERY_MANIFEST.json` records the original archive hash and current payload |
| Secrets | No configured API key, PayPal secret, application password, database password or owner connection string in the reviewed delivery/upload ZIPs | `acceptance-record/sentinel/review-verification.json` |
| Database preparation | Migration 0003 applied; two application users and one unapproved deal provisioned; zero payment operations | `acceptance-record/sentinel/bootstrap-verification.json` |
| Runtime privileges | `accord_app` remains the runtime user and can read the new webhook evidence columns | Same bootstrap record |
| Public deployment | `/health` 200; `/` and `/api/browser-config` 404 | `acceptance-record/sentinel/public-readiness.json` |

The browser checks cover separate buyer/supplier approvals, backend authorization, isolated wrong-grade blocking, rejected relabeling, response-order safety, explicit replacement review, an uncertain capture shown as Reconciling, read recovery, repeated/concurrent capture requests and a 390px layout without horizontal overflow. Screenshots and both current videos are included. Desktop blocked/captured screens and the mobile result were visually inspected.

An earlier regression attempt against a separate Render test database timed out after 900 seconds and emitted a failure marker without final diagnostics. It is not counted as a pass. Local PostgreSQL was then started successfully and the entire suite completed. This is not proof of acceptable performance over a distant database connection. A remote browser attempt also exceeded the harness's ten-second startup wait; the completed proof uses local PostgreSQL. No application database was used for destructive tests.

The original builder's process-recovery record remains supplied evidence: a separate process/90-second lease/replacement worker with provider doubles. It was inspected, not independently rerun in this review. The final browser run independently exercises uncertain-response recovery within its harness.

## Environment work completed

The existing private owner connection was configured as local `ACCORD_OWNER_DATABASE_URL`, leaving runtime `DATABASE_URL` unchanged. The reviewed additive migration 0003 was applied using the owner, then the idempotent bootstrap created the buyer/supplier accounts using the locally generated passwords. Both approvals and delivery acknowledgement remained false. Provisioning made no PayPal or model calls. Owner credentials and application passwords are excluded from deployment configuration and all packages.

The current database is ready for the updated API. Upload/deploy the reviewed source and keep the payment and extraction workers running. See `RENDER_JOURNEY_UPDATE.md` for the exact paths, command and environment separation. No GitHub publication, Render deployment or paid service creation was performed by this review.

## Remaining acceptance gates

- **PayPal: unverified.** Existing supplier OAuth success does not establish the real buyer popup, USD 750 authorization/capture, retained authentic signature verification, duplicate requests or uncertain-response recovery. Deploy the updated source and workers, then complete the real acceptance sequence and recorder. The configured public origin still serves the old API.
- **Model: blocked.** The recorded real call returned `credit_balance_exhausted`; no successful provider output or adversarial real-model run is claimed. Restore usable API credits or configure a funded project key before running the model proof. Doubles are explicitly labelled.
- **Linux: blocked.** The supplied handoff reports the Docker Linux engine pipe absent. The earlier automatic permission review did not grant Docker runtime-directory writes and returned no reason. No further Docker start or directory-write attempt was made in this review, and no Linux execution pass is claimed.

`JOURNEY_ACCEPTANCE.md` and its original records are preserved as the builder's historical handoff. This review supersedes their owner/migration/deployment readiness statements. Original and reviewed ZIPs are distinct; each reviewed ZIP has its own freshly generated manifest. Test databases are retained for inspection; the audit-owned local PostgreSQL server is stopped after verification.
