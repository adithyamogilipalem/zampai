# VendorFlow AI

VendorFlow AI is a runnable Streamlit demonstration of intelligent vendor onboarding and verification. It collects vendor information and PDF evidence, extracts literal document text, applies transparent rules, searches prior applications for possible duplicates, generates a reasoned recommendation, and keeps revisions and decisions in SQLite locally or Supabase Postgres when hosted.

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
vendorflow/storage.py      Local/Supabase settings and private PDF Storage access
vendorflow/migrate_supabase.py Read-only SQLite inventory and one-way Supabase migration
vendorflow/documents.py    PDF validation, PyMuPDF text extraction, field patterns
vendorflow/validation.py   Country policy, format rules, comparisons, risk and decisions
vendorflow/workflow.py     Persistence, staged execution, duplicate search, audit, review
vendorflow/demo.py         Fictional seed documents and executable demo scenarios
vendorflow/ai.py           Optional, opt-in OpenAI JSON extraction with source matching
vendorflow/seed.py         Database initialization and seed CLI
tests/test_workflow.py     Four required scenarios plus sensitive-data persistence check
```

The database contains vendors, applications, revisions, uploaded document metadata, extracted fields with document/run sources, verification runs and checks, decisions, reviewer actions, communication drafts, and audit events. Local runs use SQLite and the local `data/` directory. Hosted runs use Postgres for records and a private Supabase Storage bucket for PDFs. Revisions are append-only; an automated recommendation remains recorded after a reviewer acts. The live progress table is updated by callbacks from the actual synchronous workflow and reflects persisted stage statuses and timestamps.

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

## Free hosting: Streamlit Community Cloud + Supabase Free

This setup uses Streamlit Community Cloud for the Streamlit app and Supabase Free for durable Postgres records and private PDF objects. It does not need Render, a Render disk, Docker, or a paid hosting service. Community Cloud's local filesystem is not used for application records or uploaded PDFs when `VENDORFLOW_STORAGE_BACKEND = "supabase"` is configured. AI remains disabled unless explicitly enabled.

### 1. Create a free Supabase project

1. Create a Supabase account and choose the **Free** plan for the organization/project. Do not add paid compute, storage, or other upgrades for this walkthrough.
2. In the project dashboard, copy the project URL and the server-side `service_role` key. Keep both private.
3. Open **Connect** and select the **Session pooler** connection string. Use session mode on port `5432`; it works on IPv4-only hosting. Replace the password placeholder with the database password you chose. Percent-encode reserved characters in the password before inserting it in the URL.
4. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and replace the placeholders. Keep `VENDORFLOW_STORAGE_BACKEND = "supabase"`. The real secrets file is ignored by Git. Set a demo admin password of your choice.

On first startup, VendorFlow creates the schema and a private PDF-only bucket named `vendor-documents` (or the bucket named in the secrets). It enables Postgres row-level security and revokes table grants from public/anonymous roles; no Data API policies are added. The app reads and writes through its server-side database connection. It uses the Storage service-role key only on the Streamlit server, never in the browser. The app stops with a setup message if Supabase is selected but a required credential is missing. Do not change the bucket to public.

### 2. Optionally migrate the existing local database and PDFs

Keep a backup of your local `data/vendorflow.db` and `data/uploads/`. The migration command itself opens the SQLite source read-only, checks every referenced document against its stored SHA-256 hash, and leaves the SQLite file and source PDFs in place. It copies to Supabase; it does not delete or move local data. Run migration **before the first Community Cloud launch** if you want only your existing records in the hosted database; an empty app otherwise seeds its fictional demo vendor.

In PowerShell from the repository folder:

```powershell
python -m pip install -r requirements.txt
Copy-Item .streamlit/secrets.toml.example .streamlit/secrets.toml
# Edit .streamlit/secrets.toml with the private values from your Supabase project.
python -m vendorflow.migrate_supabase --db data/vendorflow.db
python -m vendorflow.migrate_supabase --db data/vendorflow.db --apply
```

The first command is a dry run. It reports counts and verifies the source PDFs without writing anything to Supabase. The second performs the copy. It is safe to retry: record IDs are preserved, inserts use conflict-ignore behavior, and document object paths are deterministic. If a target ID already exists, the migration leaves that target record untouched. Supabase may contain uploaded file objects if a database write is interrupted; re-running reuses the same paths. Review the row counts printed after migration. Do not paste secrets into chat or commit `.streamlit/secrets.toml`.

If the project has no existing local applications, skip the migration; VendorFlow will seed one fictional Approved demo vendor on first launch.

### 3. Deploy from GitHub to Streamlit Community Cloud

1. Sign in to Streamlit Community Cloud with the GitHub account that can access `adithyamogilipalem/zampai`.
2. Choose **Create app** → **Yup, I have an app**.
3. Enter repository `adithyamogilipalem/zampai`, branch `master`, and main file path `app.py`.
4. Open **Advanced settings**, choose Python `3.12`, and paste the populated contents of your local `.streamlit/secrets.toml` into **Secrets**. Do not paste the example placeholders.
5. Deploy. Community Cloud will build the requirements and show the generated `*.streamlit.app` URL when the app is ready. Save that URL for your submission.

Future commits pushed to `master` update the Community Cloud app. The root `render.yaml` is not used by this deployment path. The prior Render blueprint was left with auto-sync disabled; no paid Render deployment is part of these instructions.

### Free-tier limits and demonstration scope

Supabase Free currently includes a 500 MB database, 1 GB of file storage and 5 GB of egress; Free projects can pause after a week of low activity. The app enforces a smaller 10 MB PDF upload limit by default. Keep usage within the Free quotas and do not upgrade the project if you want to keep hosting free. Review current [Supabase Free plan limits](https://supabase.com/pricing) before deploying.

Streamlit secrets are stored in the app's server-side settings and must never be committed. The Supabase bucket is private; its service-role key bypasses Storage RLS, so it must remain server-side. The admin login and vendor access flow are demonstration controls, not production-grade authentication. Use only fictional vendor records and documents. Demo Verification is simulated policy checking only; no live government, tax, identity, sanctions, or bank service is contacted. Supabase Free does not provide paid-plan backup assurances; keep your local database and documents as a backup.

`Dockerfile`, `docker-compose.yml`, and `render.yaml` remain available for existing local/container work, but the free deployment walkthrough above does not use them.

## Quality checks

The tests run all four required scenarios through `process_submission`, inspect their stored evidence and decisions, verify correction creates a second revision and run, and assert that raw bank account numbers do not appear in application snapshots. They use temporary SQLite databases and do not modify the live demo database. Hosted Supabase connectivity cannot be tested until you create a project and configure your private secrets; the PostgreSQL and Storage paths are exercised by the app after that configuration.
