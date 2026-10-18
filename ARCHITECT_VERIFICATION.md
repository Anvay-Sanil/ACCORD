# Phase 3 verification record

Status: source and local backend validation complete; real-provider acceptance pending.

## Recorded run

- Timestamp: 2026-10-07T04:47:59.199107+05:30.
- Result: **54 passed, 0 failed, 0 errors, 0 skipped** in 56.434 seconds.
- Runtime: Python 3.12.14 on Windows; PostgreSQL 17.10 (64-bit).
- Database: actual disposable PostgreSQL, not SQLite or an in-memory database replacement.
- External financial provider: explicit `tests/fake_paypal.py` double. HTTP adapter tests use HTTPX mock transport.
- Model: no real OpenAI call. Tests supply clearly identified fixture proposals.
- Report: `test-results.xml` contains the individual test results.
- One dependency warning: Starlette deprecates its HTTPX TestClient integration. It did not fail a test; no dependency upgrade was attempted solely to suppress it.

Executed command from the project directory:

```powershell
$env:TEST_DATABASE_URL='postgresql://accord@127.0.0.1:55439/accord_test'
python -m pytest -q --tb=short --junitxml=test-results.xml
```

The passwordless URL above belonged to a temporary loopback-only test cluster. It is not deployment configuration. Application examples require separate owner/runtime credentials. The permission test uses its own ephemeral `accord_test_runtime` role.

## Evidence matrix

| Requirement | Local evidence |
| --- | --- |
| Repeated/concurrent captures | Ten concurrent submissions reuse one operation; two workers cannot simultaneously claim the same deal. |
| Durable financial operations | Frozen payload/key database triggers; lost create/auth/capture/void responses; original request replay where permitted. |
| Uncertain capture | Missing capture ID recovered through order data and a correlated verified-inbox fixture; same-key replay; five-minute retry cutoff. |
| Stale workers | Expired generation cannot write; test lets the stale process send HTTP and verifies automatic void remains prohibited. |
| Capture/void race | Concurrent queueing serializes on the deal; conflicting financial POSTs do not both dispatch. |
| Evidence invalidation | Same operation blocked and rebound before dispatch; decision frozen afterward; new human interpretation invalidates older PASS. |
| Authorization deadline | Provider creation timestamp controls 48 hours; six-hour authorization key window stops POST retry; pending capture may settle later. |
| Known payment state | Late events trigger canonical reads; failed refresh does not erase confirmed capture. |
| Evidence semantics | Three supported layouts; required-versus-delivered, mixed lots, negation, contradictions, instructions, missing grade, wrong PO, and wrong field/version/quote. |
| Evidence controls payment | Full evaluation of passing/failing document fixtures either permits capture or blocks it. |
| Provenance | Failed extraction preserves parser/schema/prompt/model versions in immutable extraction history. |
| Browser ingress | Cookie flags, Origin/CSRF, login throttling, role checks, known-deal nonmember denial, upload body limit. |
| Webhooks | Verification failure/outage, durable inbox deduplication, malformed input, raw event forwarding, SUCCESS check. Cryptography at PayPal is mocked. |
| HTTP adapter | OAuth reuse, 429 handling, no hidden financial POST retry. |
| Parser isolation | Actual PDF parse in constrained Windows subprocess; malformed input, wall timeout, and failed 700 MB allocation under the 512 MB bound. |
| Database permissions | Restricted role can queue operations but cannot rewrite audit, delete decisions/extractions, truncate audit, modify users, or create tables. |

Python bytecode compilation passed. Compose configuration validation passed using `.env.example`; Docker emitted a local configuration-file access warning. Containers were not built or run because the Docker daemon was unavailable. No claim of a deployed service follows from configuration validation.

## Outstanding acceptance

- Actual supplier app ownership, buyer PayPal approval, authorization/capture responses and their field shapes.
- A real transaction's webhook signature verified by PayPal's postback endpoint; simulator events are insufficient for this chosen method.
- Merchant-specific idempotency behavior and recovery visibility. The local five-minute capture retry budget is a conservative application limit, not proof of a provider retention guarantee.
- Real model extraction on varied/adversarial PDFs; fixture correctness is not a model accuracy score.
- Linux resource-limit behavior, image build, service isolation in Compose, public HTTPS/cookie behavior, and load/rate-limit behavior.
- React checkout and evidence-review UI, including buyer SDK approval of the backend's existing order.

The backend restricts automatic admission to bounded native-text layouts. Unknown semantics require explicit buyer review. Resource limits do not constitute a complete malicious-code sandbox. No live-money call or public deployment was performed. Phase 3 payment-foundation acceptance should remain conditional until the outstanding tests in `README.md` are recorded.
