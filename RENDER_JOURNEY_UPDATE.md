# Deploy the reviewed ACCORD journey

The application database is already at migration **0003**. Buyer and supplier accounts and one unapproved USD 750 demo agreement have been provisioned. No payment operation or approval was created by provisioning. Preserve the existing database and restricted runtime account.

## 1. Update GitHub

Extract `accord-render-upload.zip`. Upload the **contents** to the root of `Anvay-Sanil/ACCORD`, replacing matching files. At the repository root you must see `Dockerfile`, `requirements.lock`, `alembic.ini`, and the folders `accord/`, `migrations/`, `acceptance/`, and `tests/`. Keep each folder's contents inside that folder. Include `.gitignore` and `.dockerignore`.

The upload ZIP contains `.env.example` with placeholders. It excludes `.env`, owner credentials, account passwords, database files, caches and acceptance recordings. Keep the actual `.env` local.

## 2. Redeploy the web service

Use the existing Render web service, repository and `main` branch. These paths assume the files above are at the repository root.

| Render setting | Value |
| --- | --- |
| Language | Docker |
| Region | Oregon, matching the database |
| Root Directory | Leave empty |
| Docker Build Context Directory | `.` |
| Dockerfile Path | `./Dockerfile` |
| Docker Command | `python -m uvicorn accord.api:create_app --factory --host 0.0.0.0 --port 10000` |
| Health Check Path | `/health` |
| Pre-Deploy Command | Leave empty; migration 0003 is already applied |

Keep the existing private runtime `DATABASE_URL` for **accord_app**, supplier Sandbox app credentials and webhook ID. Check these values without publishing them:

```dotenv
APP_ORIGIN=https://accord-api-fyux.onrender.com
PUBLIC_DEPLOYMENT=true
SESSION_SECURE=true
FAULT_DROP_CAPTURE_RESPONSE=false
PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com
```

The API also needs `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_MERCHANT_ID`, and `PAYPAL_WEBHOOK_ID`. Keep `ACCORD_OWNER_DATABASE_URL`, `BUYER_PAYER_ID`, and both application passwords out of deployed service environment variables. They are provisioning inputs, not runtime credentials.

Choose **Manual Deploy → Deploy latest commit** if the GitHub update did not trigger deployment. Render supports the command override in its [Docker settings](https://render.com/docs/docker).

## 3. Run both workers

The web service serves the interface and records work. It does not run either worker. Deploy the same source commit as two continuous [background workers](https://render.com/docs/background-workers), or run these two processes on an already available host during acceptance:

| Process | Docker Command | Required environment |
| --- | --- | --- |
| Payments | `python -m accord.worker` | Restricted `DATABASE_URL`, supplier Sandbox credentials/merchant/webhook IDs; public configuration above with fault injection off |
| Extraction | `python -m accord.extraction_worker` | Restricted `DATABASE_URL`, funded `OPENAI_API_KEY`, `OPENAI_MODEL=gpt-4.1-2025-04-14` |

Render services in the same workspace and region can use the database's internal hostname; a local process needs the external hostname. Use the restricted **accord_app** login in either case. See Render's [private network reference](https://render.com/docs/private-network). No new paid service has been created by this audit.

Give extraction at least the 768 MB memory budget specified in `compose.yaml`. Keep both workers running through the acceptance journey. Do not place them in a startup shell command alongside Uvicorn.

## 4. Verify the update

Open [the application](https://accord-api-fyux.onrender.com/) and [health check](https://accord-api-fyux.onrender.com/health). The root must display the ACCORD sign-in page. `/api/browser-config` must return sandbox configuration with only a public client ID. A 404 at either journey route means the old source is still deployed.

Use separate browser profiles for `buyer` and `supplier`. Read their distinct application passwords from the local `.env`; these are separate from the PayPal Sandbox buyer login. Follow `JOURNEY.md` for approvals, Sandbox checkout, evidence and replacement review.

The registered supplier webhook URL remains `https://accord-api-fyux.onrender.com/api/webhooks/paypal`. Its existing ID stays in `PAYPAL_WEBHOOK_ID`. A healthy API does not establish a real payment, signed-event, model or Linux acceptance pass. The OpenAI credit gate must be cleared before treating extraction as ready.
