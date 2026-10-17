# Sentinel verification record

Status: revised local payment foundation passes; real-provider and deployment acceptance pending.

- Final run: **71 passed, 0 failed, 0 errors, 0 skipped**, 189.426 seconds.
- Run timestamp: 2026-10-07T19:45:24.721594+05:30 (Asia/Calcutta client time).
- Runtime: Python 3.12.14 on Windows; actual PostgreSQL 17.10.
- Independent baseline: original packaged source passed all 54 tests in 60.381 seconds.
- Audit reproductions: `sentinel-before.xml` records six failing payment/event cases before correction. `sentinel-evidence-before.xml` records the failing review-bypass case before its correction. These expected failing reports are historical audit evidence.
- Added regressions: 17 cases, including seven reproduced defects and ten checks for pending/denied/consumed authorizations and unsafe or conflicting refund relationships.
- Final report: `review-test-results.xml` and `test-results.xml` contain the passing full suite.
- Python compilation: `compileall -q accord migrations tests` passed.
- One existing Starlette TestClient deprecation warning remains; dependency versions were not changed solely to suppress it.

The audit used its own database, `accord_test_sentinel`, on the existing loopback test PostgreSQL service at port 55439. It did not reset the Architect's original test database. The application package does not include credentials or an `.env` file.

```powershell
$env:TEST_DATABASE_URL='postgresql://accord@127.0.0.1:55439/accord_test_sentinel'
python -m pytest -q --tb=short --junitxml=review-test-results.xml
```

That passwordless URL describes the existing temporary test cluster, not deployment configuration. Re-create a dedicated disposable test database before rerunning; tests reset its public schema. Normal deployment requires owner/runtime credentials as documented in README.

## Verification boundaries

PayPal financial calls and signature verification use explicit test doubles or HTTPX mock transport. The seven failing scenarios establish application defects; they do not establish an actual duplicate or invalid charge at PayPal. A real PayPal capture against a voided authorization should be rejected; the audit demonstrates that the original application still dispatched that prohibited attempt.

Actual Windows PDF parsing, timeout/memory limits, PostgreSQL locking, permissions, and ingress checks were exercised. Real OpenAI extraction was not executed. Official OpenAI documentation was checked for the pinned GPT-4.1 model and Responses Structured Outputs configuration; compatibility documentation is not a model accuracy result.

Still outstanding: supplier/buyer Sandbox provisioning, real Checkout approval and authorize/capture response shapes, a real transaction's signature-verified webhook, provider idempotency/recovery behavior, model extraction on varied/adversarial PDFs, Linux limits and Docker execution, hosted HTTPS behavior, and the checkout/evidence UI. The inherited Docker/Compose setup remains unexecuted in this audit. See README's acceptance procedure.

The original temporary PostgreSQL server on localhost:55439 was not stopped by this audit. Automatic approval review rejected cleanup of the isolated review database and generated caches because sandbox approval is disabled. The isolated review database and original server remain running. Generated caches are excluded from the ZIP and complete-source artifact.
