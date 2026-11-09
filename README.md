# VendorFlow AI

VendorFlow AI is a runnable Streamlit demonstration of intelligent vendor onboarding and verification. It collects vendor information and PDF evidence, extracts literal document text, applies transparent rules, searches prior applications for possible duplicates, generates a reasoned recommendation, and keeps revisions and decisions in SQLite.

## Run locally

Python 3.10 or later is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -m vendorflow.seed
streamlit run app.py
```

Open the local URL printed by Streamlit. The default demonstration admin password is `demo-admin` when no `.env` setting is loaded. Set `VENDORFLOW_ADMIN_PASSWORD` before sharing a private demo. This password gate is explicitly a demonstration feature, not production authentication. The app seeds one fictional approved application only when the database has no applications. The seed PDFs are generated from fictional text and written under `sample_documents/`.

To run the automated workflow scenarios:

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

To initialize or seed a chosen database:

```powershell
python -m vendorflow.seed --db data/vendorflow.db --uploads data/uploads
```

## Five-minute walkthrough

1. Open **Demo Scenarios** and run **Complete submission**. Point out that actual PDFs are generated and processed, all six persisted workflow stages complete, and the decision is Approved only because the checks pass.
2. Open **Missing certificate**. The application is Pending with the missing GST certificate finding and a correction email draft. Click **Add missing certificate and re-verify** to create revision 2 and a new run; the original decision and timeline remain.
3. Run **Bank holder mismatch**. Show the submitted name, extracted bank holder, high risk contributor, and human-review path.
4. Run **Duplicate tax ID**. Show the related application ID found in SQLite and the blocked automatic approval.
5. Open **Procurement Admin**, sign in with the configured demo password, open a Pending record, and record a reasoned review action. Then inspect **Track / Correct** or the application history for revisions, checks, messages, and audit events.

## Architecture

```text
app.py                     Streamlit vendor and procurement experiences
vendorflow/db.py           SQLite schema, connections and JSON helpers
vendorflow/documents.py    PDF validation, PyMuPDF text extraction, field patterns
vendorflow/validation.py   Country policy, format rules, comparisons, risk and decisions
vendorflow/workflow.py     Persistence, staged execution, duplicate search, audit, review
vendorflow/demo.py         Fictional seed documents and executable demo scenarios
vendorflow/ai.py           Optional, opt-in OpenAI JSON extraction with source matching
vendorflow/seed.py         Database initialization and seed CLI
tests/test_workflow.py     Four required scenarios plus sensitive-data persistence check
```

SQLite contains vendors, applications, revisions, uploaded document metadata, extracted fields with document/run sources, verification runs and checks, decisions, reviewer actions, communication drafts, and audit events. Revisions are append-only; an automated recommendation remains recorded after a reviewer acts. The live progress table is updated by callbacks from the actual synchronous workflow and reflects persisted stage statuses and timestamps.

## Decision and verification design

- Required information, plausible email and phone syntax, required documents, and country-specific formats are checked independently. Indian GSTIN, PAN and IFSC patterns run only when country is India.
- PyMuPDF reads text-based PDFs. The local extractor records only values it finds in source text. Scanned or unreadable PDFs are flagged; OCR is not implemented.
- Company names are compared after case, punctuation, whitespace and common legal suffix normalization. Tax IDs compare as normalized identifiers. Name conflicts, unreadable documents and missing extracted values need correction or human review.
- Duplicate GSTIN, PAN, or bank fingerprints are potential duplicates, never an automatic fraud finding. Matching application IDs are shown.
- Risk categories list their contributing checks. Approved means all configured demo checks passed. Missing, invalid, unreadable, inconsistent or duplicate evidence yields Pending. Automated rejection is intentionally not inferred from a format failure or a possible duplicate; a human reviewer can reject with a required reason.
- All external checks are **Demo Verification**. No government registry, bank ownership, tax authority, identity, or live sanctions service is contacted.
- Account numbers are not stored in application snapshots or audit events. The app retains a masked last-four display and a SHA-256 fingerprint for duplicate matching. Uploaded PDFs remain local files, so bank documents themselves may contain sensitive details.
- Follow-up messages are stored as drafts and are never sent.

## Optional AI

AI is disabled by default. To opt into OpenAI-assisted document interpretation, set `VENDORFLOW_ENABLE_AI=true`, `OPENAI_API_KEY`, and optionally `OPENAI_MODEL` in `.env`. Document text is sent to the configured OpenAI API only in this mode. Model candidates are accepted only if their exact text is present in the source PDF; deterministic extraction runs regardless. The app shows an unavailable/disabled label when the service is not configured or fails. Follow-up messages currently use explicit templates rather than an AI model. API keys are read from environment variables and are not written to SQLite.

## Configuration

Copy `.env.example` to `.env` and configure:

- `VENDORFLOW_DB_PATH`: SQLite file path (default `data/vendorflow.db`)
- `VENDORFLOW_DATA_DIR`: uploaded file directory (default `data`)
- `VENDORFLOW_MAX_UPLOAD_MB`: PDF size limit (default 10)
- `VENDORFLOW_ADMIN_PASSWORD`: demonstration admin password
- `VENDORFLOW_ENABLE_AI`, `OPENAI_API_KEY`, `OPENAI_MODEL`: optional AI extraction

PDF files over the configured size, non-PDF extensions/MIME types, empty uploads, and invalid PDF signatures are rejected before an application is created. Uploaded file names are basename-sanitized before storage and long digit sequences are masked in the interface.

## Deployment and limitations

The app can run in a container or any host that supports Streamlit and writable persistent storage. `Dockerfile` and `docker-compose.yml` provide a local container launch with a mounted `data/` volume. For a hosted interviewer demo, use a persistent volume for both SQLite and uploaded files. Ephemeral serverless filesystems can lose records on restart, so they do not satisfy the persistence requirement. The demo login, local SQLite database, and upload storage are not production-grade security controls; do not put real vendor or bank data into this case study.

### Render

The root `render.yaml` defines a Singapore Docker web service with a 1 GB persistent disk. The database and uploads are both stored beneath `/var/lib/vendorflow`, so submissions survive service restarts and deployments. The container uses Render's `PORT` environment variable. Because Render only supports persistent disks on paid web services, this configuration is not a free, always-on deployment.

Set `VENDORFLOW_ADMIN_PASSWORD` as a secret environment variable in Render before using Procurement Admin. The hosted app locks admin sign-in while this value is missing. Never commit the password or use real vendor information in this demonstration. AI extraction remains disabled unless explicitly configured.

## Quality checks

The tests run all four required scenarios through `process_submission`, inspect their stored evidence and decisions, verify correction creates a second revision and run, and assert that raw bank account numbers do not appear in application snapshots. They use temporary SQLite databases and do not modify the live demo database.
