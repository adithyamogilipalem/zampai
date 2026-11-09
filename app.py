"""VendorFlow AI — local vendor onboarding and verification demonstration."""

from __future__ import annotations

import hmac
import re
from pathlib import Path

import streamlit as st

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from vendorflow.ai import ai_capability_label
from vendorflow.db import DEFAULT_DB_PATH, initialize_database
from vendorflow.demo import correct_missing_certificate, run_demo_scenario, seed_demo_data
from vendorflow.documents import DocumentError
from vendorflow.storage import StorageError, get_setting, uses_supabase_database
from vendorflow.workflow import (
    STAGES,
    application_matches_vendor,
    dashboard_counts,
    get_application,
    list_applications,
    process_submission,
    recover_interrupted_runs,
    reviewer_decision,
)


APP_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(str(get_setting("VENDORFLOW_DATA_DIR", "data")))
UPLOAD_ROOT = DATA_ROOT / "uploads"


st.set_page_config(page_title="VendorFlow AI | Vendor onboarding", page_icon="◈", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap');
    :root { --navy:#17324d; --blue:#315f91; --ink:#172b3b; --muted:#66788a; --line:#e6ebf0; --soft:#f5f8fb; --green:#16845b; --amber:#a3660b; --red:#bd3942; }
    html, body, [class*="css"] { font-family:'DM Sans',sans-serif; color:var(--ink); }
    .stApp { background:#f7f9fc; }
    h1,h2,h3,h4 { font-family:'Manrope',sans-serif; letter-spacing:-.025em; color:var(--navy); }
    h1 { font-weight:800; }
    [data-testid="stSidebar"] { background:#fff; border-right:1px solid var(--line); }
    [data-testid="stSidebar"] > div:first-child { padding-top:1.15rem; }
    .brand { font-family:'Manrope',sans-serif; color:var(--navy); font-size:1.15rem; font-weight:800; letter-spacing:-.03em; padding:.2rem .35rem .7rem; }
    .brand-mark { display:inline-flex; width:28px;height:28px;align-items:center;justify-content:center;background:#eaf1f8;color:#315f91;border-radius:9px;margin-right:8px; }
    .eyebrow { color:#718398; font-size:.72rem; letter-spacing:.12em; text-transform:uppercase; font-weight:700; }
    .subtitle { color:var(--muted); margin-top:-.65rem; margin-bottom:1.2rem; }
    .topline { padding:1.25rem 1.45rem; border:1px solid var(--line); border-radius:16px; background:white; margin:.25rem 0 1.2rem; box-shadow:0 4px 18px rgba(25,53,82,.035); }
    .metric-card { padding:1rem 1.1rem; border:1px solid var(--line); background:white; border-radius:14px; min-height:100px; box-shadow:0 4px 18px rgba(25,53,82,.025); }
    .metric-label { color:#6d7e90; font-size:.82rem; font-weight:600; }
    .metric-value { color:var(--navy); font-family:'Manrope',sans-serif; font-weight:800; font-size:1.65rem; margin-top:.35rem; }
    .vf-card { border:1px solid var(--line); background:#fff; border-radius:14px; padding:1rem 1.15rem; margin:.45rem 0; box-shadow:0 4px 16px rgba(25,53,82,.025); }
    .status { display:inline-block; border-radius:999px; padding:3px 10px; font-size:.77rem; font-weight:700; }
    .status-approved,.status-pass { color:#146c4c;background:#e8f6ef; }
    .status-pending,.status-warning,.status-needs-review { color:#8a5909;background:#fff3d8; }
    .status-rejected,.status-fail,.status-failed { color:#a72d37;background:#fdebed; }
    .status-running { color:#265e9b;background:#e9f2fc; }
    .status-not-started { color:#607285;background:#eef2f5; }
    .note { border-left:3px solid #7a9ec3; padding:.55rem .85rem; background:#f1f6fb; border-radius:0 9px 9px 0; color:#476177; font-size:.9rem; }
    .demo-label { display:inline-block; color:#7d5a13; background:#fff5dc; border:1px solid #f1dfa9; border-radius:999px; padding:3px 9px; font-size:.74rem; font-weight:700; }
    .muted { color:var(--muted); }
    div[data-testid="stForm"] { background:#fff; border:1px solid var(--line); border-radius:14px; padding:1rem 1.1rem; }
    [data-testid="stTextInputRootElement"] { border:1px solid #cbd5e1 !important; border-radius:8px !important; background:#fff !important; box-shadow:none !important; }
    [data-testid="stTextInputRootElement"]:focus-within { border-color:#527ba3 !important; box-shadow:0 0 0 3px rgba(49,95,145,.12) !important; }
    [data-testid="stTextInputField"] { color:var(--ink) !important; background:transparent !important; }
    [data-testid="stSelectbox"] [role="group"] { border:1px solid #cbd5e1 !important; border-radius:8px !important; background:#fff !important; }
    [data-testid="stSelectbox"] [role="group"] input { color:var(--ink) !important; background:transparent !important; }
    [data-testid="stTextArea"] textarea { color:var(--ink) !important; background:#fff !important; border:1px solid #cbd5e1 !important; border-radius:8px !important; }
    div.stButton > button, div[data-testid="stFormSubmitButton"] > button { border-radius:9px; font-weight:650; min-height:2.6rem; }
    div.stButton > button[kind="primary"], div[data-testid="stFormSubmitButton"] > button[kind="primary"] { background:#244e78; border-color:#244e78; }
    [data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


def initialize_app() -> None:
    initialize_database(DEFAULT_DB_PATH)
    recover_interrupted_runs(DEFAULT_DB_PATH)
    if not uses_supabase_database(DEFAULT_DB_PATH, DEFAULT_DB_PATH):
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        seed_demo_data(DEFAULT_DB_PATH, UPLOAD_ROOT)
    except Exception as exc:
        st.sidebar.warning(f"Demo seed could not load ({type(exc).__name__}). You can still submit applications.")


def badge(status: str) -> str:
    cls = status.lower().replace(" ", "-")
    return f'<span class="status status-{cls}">{status}</span>'


def page_header(title: str, subtitle: str, eyebrow: str = "VENDORFLOW AI") -> None:
    st.markdown(f'<div class="eyebrow">{eyebrow}</div><h1 style="margin:.15rem 0 .75rem">{title}</h1><div class="subtitle">{subtitle}</div>', unsafe_allow_html=True)


def masked_filename(name: str) -> str:
    return re.sub(r"\d{6,}", lambda m: "••••" + m.group(0)[-4:], name or "document.pdf")


def render_progress() -> tuple[dict[str, dict[str, str]], callable]:
    state = {stage: {"status": "Not Started", "started_at": "—", "completed_at": "—", "findings": ""} for stage in STAGES}
    placeholder = st.empty()

    def update(stage: str, status: str, timestamp: str, details: dict) -> None:
        if stage not in state:
            return
        row = state[stage]
        row["status"] = status
        if status == "Running":
            row["started_at"] = timestamp
        else:
            row["completed_at"] = timestamp
            row["findings"] = str(details.get("decision") or details.get("warnings") or details.get("documents_needing_review") or details.get("failed") or "Stage complete")
        complete = sum(v["status"] in {"Completed", "Needs Review", "Failed"} for v in state.values())
        with placeholder.container():
            st.progress(complete / len(state), text=f"Verification workflow · {complete} of {len(state)} stages complete")
            rows = [{"Stage": key, "Status": value["status"], "Started": value["started_at"], "Completed": value["completed_at"], "Finding": value["findings"]} for key, value in state.items()]
            st.dataframe(rows, use_container_width=True, hide_index=True)

    with placeholder.container():
        st.progress(0, text="Verification workflow · 0 of 6 stages complete")
        st.dataframe([{"Stage": key, "Status": "Not Started", "Started": "—", "Completed": "—", "Finding": ""} for key in STAGES], use_container_width=True, hide_index=True)
    return state, update


def render_result(result: dict) -> None:
    status = result.get("status", "Pending")
    st.markdown(f"### Decision {badge(status)}", unsafe_allow_html=True)
    st.write(result.get("reason", "Review the application record for the saved decision."))
    st.caption(f"Checks passed: {result.get('checks_passed', 0)} of {result.get('checks_total', 0)} · Issues detected: {len(result.get('issues') or [])}")
    st.caption(f"Risk: {result.get('risk_category', 'Medium')} · Application: {result.get('application_id', '—')} · Verification run: {result.get('run_id', '—')}")
    if result.get("issues"):
        st.markdown("**Findings**")
        for item in result["issues"]:
            st.write(f"• {item}")
    if result.get("recommended_action"):
        st.info(result["recommended_action"])


def file_upload(label: str, key: str, help_text: str) -> Any:
    return st.file_uploader(label, type=["pdf"], accept_multiple_files=False, key=key, help=help_text)


def upload_values(items: dict[str, Any]) -> dict[str, tuple[str, bytes, str]]:
    result = {}
    for kind, file in items.items():
        if file is not None:
            result[kind] = (file.name, file.getvalue(), file.type or "application/pdf")
    return result


def render_vendor_form(existing: dict | None = None, application_id: str | None = None) -> None:
    data = existing or {}
    prefix = "correction" if application_id else "new"
    st.markdown("#### Company information")
    with st.form(f"vendor_form_{prefix}", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            registered_name = st.text_input("Registered company name *", value=data.get("registered_name", ""))
            trading_name = st.text_input("Trading name", value=data.get("trading_name", ""))
            business_types = ["Private Limited Company", "Public Limited Company", "Partnership", "Sole Proprietorship", "LLP", "Non-profit", "Other"]
            selected_business_type = data.get("business_type", business_types[0])
            business_type = st.selectbox("Business type *", business_types, index=business_types.index(selected_business_type) if selected_business_type in business_types else 0)
            country = st.selectbox("Country *", ["India", "United States", "United Kingdom", "Singapore", "Other"], index=["India", "United States", "United Kingdom", "Singapore", "Other"].index(data.get("country", "India")) if data.get("country", "India") in ["India", "United States", "United Kingdom", "Singapore", "Other"] else 0)
            registered_address = st.text_input("Registered address *", value=data.get("registered_address", ""))
        with col2:
            state = st.text_input("State / region *", value=data.get("state", ""))
            city = st.text_input("City *", value=data.get("city", ""))
            postal_code = st.text_input("Postal code *", value=data.get("postal_code", ""))
            contact_name = st.text_input("Contact person name *", value=data.get("contact_name", ""))
            designation = st.text_input("Designation *", value=data.get("designation", ""))
        st.markdown("#### Contact and tax information")
        col3, col4 = st.columns(2)
        with col3:
            email = st.text_input("Email address *", value=data.get("email", ""))
            phone = st.text_input("Phone number *", value=data.get("phone", ""))
            if country == "India":
                gstin = st.text_input("GSTIN *", value=data.get("gstin", ""), max_chars=15)
                pan = st.text_input("PAN *", value=data.get("pan", ""), max_chars=10)
            else:
                gstin = st.text_input("Local tax ID (optional)", value=data.get("gstin", ""))
                pan = st.text_input("Additional tax ID (optional)", value=data.get("pan", ""))
        with col4:
            account_holder_name = st.text_input("Bank account holder name *", value=data.get("account_holder_name", ""))
            bank_name = st.text_input("Bank name *", value=data.get("bank_name", ""))
            if application_id:
                masked = data.get("account_number_masked", "")
                st.caption(f"Account number on file: {masked or 'not provided'}. Leave blank to retain it; enter a new value to replace it.")
                account_number = st.text_input("Bank account number *", value="", type="password", help="Stored as a masked value and one-way fingerprint.")
            else:
                account_number = st.text_input("Bank account number *", value="", type="password", help="Stored as a masked value and one-way fingerprint.")
            if country == "India":
                ifsc = st.text_input("IFSC code *", value=data.get("ifsc", ""), max_chars=11)
            else:
                ifsc = st.text_input("IFSC / bank routing code", value=data.get("ifsc", ""))
        st.markdown("#### Supporting documents")
        st.caption("PDF only · maximum 10 MB per document. Text extraction runs locally. Image files and scanned PDFs are not treated as reliably readable.")
        required_doc_hint = "India: GST certificate, PAN document, bank proof. Other countries: registration certificate and bank proof."
        st.markdown(f'<div class="note">{required_doc_hint}</div>', unsafe_allow_html=True)
        d1, d2 = st.columns(2)
        with d1:
            gst_file = file_upload("GST registration certificate", f"{prefix}_gst", "Indian applicants must attach this PDF.")
            pan_file = file_upload("PAN document", f"{prefix}_pan", "Indian applicants must attach this PDF.")
        with d2:
            bank_file = file_upload("Supporting bank document", f"{prefix}_bank", "Attach a bank letter or statement PDF. Sensitive number details are masked in the interface.")
            registration_file = file_upload("Business registration certificate", f"{prefix}_registration", "Required for non-Indian applicants; optional for India.")
        submitted = st.form_submit_button("Save correction and re-run checks" if application_id else "Submit vendor application", type="primary", use_container_width=True)

    if submitted:
        form = {
            "registered_name": registered_name, "trading_name": trading_name, "business_type": business_type, "country": country,
            "registered_address": registered_address, "state": state, "city": city, "postal_code": postal_code,
            "contact_name": contact_name, "designation": designation, "email": email, "phone": phone,
            "gstin": gstin, "pan": pan, "account_holder_name": account_holder_name, "bank_name": bank_name,
            "account_number": account_number, "ifsc": ifsc,
        }
        uploads = upload_values({"gst": gst_file, "pan": pan_file, "bank": bank_file, "registration": registration_file})
        if application_id and not account_number:
            st.caption("The current account fingerprint is retained because no replacement number was entered.")
        _, progress_callback = render_progress()
        try:
            with st.status("VendorFlow is executing document and policy checks…", expanded=True) as status_box:
                result = process_submission(
                    form, uploads, DEFAULT_DB_PATH, UPLOAD_ROOT, application_id=application_id,
                    submitted_by="vendor correction" if application_id else "vendor portal",
                    change_summary="Vendor correction and resubmission" if application_id else "Initial vendor submission",
                    on_progress=progress_callback,
                )
                status_box.update(label=f"Verification complete · {result['status']}", state="complete", expanded=True)
        except DocumentError as exc:
            st.error(f"Document upload was rejected: {exc}")
            return
        except (ValueError, OSError) as exc:
            st.error(f"The submission could not be saved: {exc}")
            return
        except Exception as exc:
            st.error(f"The submission stopped unexpectedly ({type(exc).__name__}). No verification result was produced.")
            return
        st.session_state["last_result"] = result
        st.session_state["last_application_id"] = result["application_id"]
        st.session_state.pop("tracking_application", None)
        st.rerun()


def render_activity_detail(app: dict) -> None:
    snap = app["snapshot"]
    c1, c2 = st.columns([1.2, 1])
    with c1:
        st.markdown("#### Application overview")
        st.markdown(f"**Application:** `{app['application_id']}` &nbsp; {badge(app['status'])}", unsafe_allow_html=True)
        st.write(f"**Risk:** {app['risk_category']} · **Revision:** {app['current_revision']} · **Automated recommendation:** {app.get('automated_recommendation') or '—'}")
        st.write(f"**Registered company:** {snap.get('registered_name') or '—'}")
        st.write(f"**Trading name:** {snap.get('trading_name') or '—'}")
        st.write(f"**Business type / country:** {snap.get('business_type') or '—'} / {snap.get('country') or '—'}")
        st.write(f"**Address:** {snap.get('registered_address') or '—'}, {snap.get('city') or '—'}, {snap.get('state') or '—'} {snap.get('postal_code') or ''}")
        st.write(f"**Contact:** {snap.get('contact_name') or '—'} · {snap.get('designation') or '—'} · {snap.get('email') or '—'} · {snap.get('phone') or '—'}")
        st.write(f"**Tax IDs:** GSTIN `{snap.get('gstin') or '—'}` · PAN `{snap.get('pan') or '—'}` · IFSC `{snap.get('ifsc') or '—'}`")
        st.write(f"**Bank details:** {snap.get('account_holder_name') or '—'} · {snap.get('bank_name') or '—'} · Account {snap.get('account_number_masked') or 'not provided'}")
    with c2:
        st.markdown("#### Risk and policy decision")
        st.markdown(f'<div class="vf-card"><div>{badge(app["status"])}</div><p style="margin:.7rem 0 .15rem"><b>{app["risk_category"]} risk</b></p><span class="muted">{app["decisions"][-1]["reason"] if app["decisions"] else "No decision recorded yet."}</span></div>', unsafe_allow_html=True)
        st.markdown('<span class="demo-label">DEMO VERIFICATION</span>', unsafe_allow_html=True)
        st.caption("Checks use local document text and a demonstration database. No live government registry, tax, identity, or bank ownership lookup occurs.")

    st.markdown("#### Documents attached")
    if app["documents"]:
        st.dataframe([{"Type": d["document_type"].upper(), "File": masked_filename(d["original_name"]), "Size": f"{d['size_bytes'] / 1024:.1f} KB", "Added": d["created_at"]} for d in app["documents"]], use_container_width=True, hide_index=True)
    else:
        st.caption("No documents attached in this revision.")

    st.markdown("#### Workflow execution")
    stages = [c for c in app["checks"] if c["check_name"] == "__stage__"]
    if stages:
        st.dataframe([{"Stage": x["stage_name"], "Status": x["status"], "Started": x["started_at"] or "—", "Completed": x["completed_at"] or "—", "Findings": x["findings"].get("findings") or x["findings"].get("summary") or x["findings"].get("decision") or ""} for x in stages], use_container_width=True, hide_index=True)
    st.markdown("#### Field-by-field verification results")
    check_rows = []
    for c in app["checks"]:
        if c["check_name"] == "__stage__":
            continue
        item = c["findings"]
        if not item:
            continue
        check_rows.append({"Stage": c["stage_name"], "Check": item.get("name", c["check_name"]), "Result": item.get("status", c["status"]), "Submitted": item.get("submitted") or "—", "Document / related value": item.get("extracted") or item.get("related_application_id") or "—", "Finding": item.get("message", "")})
    if check_rows:
        st.dataframe(check_rows, use_container_width=True, hide_index=True)
    st.markdown("#### Extracted document fields")
    if app["extracted_fields"]:
        st.dataframe([{"Document": f"{x['document_type'].upper()} · {masked_filename(x['original_name'])}", "Field": x["field_name"], "Extracted value": x["field_value"], "Method": x["extraction_method"], "Source": "Uploaded PDF"} for x in app["extracted_fields"]], use_container_width=True, hide_index=True)
    else:
        st.caption("No fields were extracted. Unreadable PDFs are flagged for manual review; no values are invented.")

    with st.expander("Generated communication drafts", expanded=True):
        if app["communications"]:
            for msg in app["communications"]:
                st.caption(f"{msg['created_at']} · {msg['message_type']} · To: {msg['recipient']} · Draft only")
                st.text_input("Subject", value=msg["subject"], key=f"subj_{msg['communication_id']}", disabled=True)
                st.text_area("Message", value=msg["body"], key=f"body_{msg['communication_id']}", height=180, disabled=True)
        else:
            st.caption("No draft has been generated.")

    with st.expander("Previous submission versions", expanded=False):
        for version in app["revisions"]:
            st.markdown(f"**Revision {version['revision_no']}** · {version['submitted_at']} · {version['submitted_by']} · {version['change_summary']}")
            saved = version["snapshot"]
            st.caption(f"Company: {saved.get('registered_name') or '—'} · GSTIN: {saved.get('gstin') or '—'} · PAN: {saved.get('pan') or '—'} · Account: {saved.get('account_number_masked') or '—'}")
    with st.expander("Decision and reviewer history", expanded=False):
        for decision in app["decisions"]:
            st.write(f"{decision['created_at']} · **{decision['status']}** ({decision['decision_type']}) by {decision['actor']} — {decision['reason']}")
        for action in app["reviewer_actions"]:
            st.write(f"{action['created_at']} · **{action['action']}** by {action['reviewer']} — {action['reason']}")
    with st.expander("Audit activity timeline", expanded=False):
        for event in app["audit"]:
            details = event.get("details") or {}
            st.write(f"{event['created_at']} · **{event['event_type'].replace('_',' ').title()}** · {event['actor']} · {details.get('stage') or details.get('status') or details.get('change_summary') or ''}")


def vendor_portal() -> None:
    page_header("Vendor Portal", "Submit a supplier application, attach supporting PDFs, and follow every verification step.")
    if st.session_state.get("last_result"):
        result = st.session_state["last_result"]
        st.markdown('<div class="topline"><span class="eyebrow">LATEST SUBMISSION</span>', unsafe_allow_html=True)
        render_result(result)
        detail = get_application(result.get("application_id", ""), DEFAULT_DB_PATH)
        if detail and detail["documents"]:
            st.success(f"{len(detail['documents'])} supporting PDF(s) are attached to revision {detail['current_revision']}.")
            st.dataframe([{"Document type": d["document_type"].upper(), "File": masked_filename(d["original_name"]), "Size": f"{d['size_bytes'] / 1024:.1f} KB"} for d in detail["documents"]], use_container_width=True, hide_index=True)
        st.markdown("</div>", unsafe_allow_html=True)
    st.markdown('<span class="demo-label">DEMO VERIFICATION · No live registry or bank checks</span>', unsafe_allow_html=True)
    st.markdown("### New vendor application")
    render_vendor_form()


def track_application() -> None:
    page_header("Track and correct", "Use the application ID and its registered contact email to view or revise a submission.")
    with st.form("track_application_form"):
        col1, col2 = st.columns([1, 1])
        app_id = col1.text_input("Application ID", value=st.session_state.get("last_application_id", ""))
        email = col2.text_input("Registered email address")
        open_record = st.form_submit_button("Open application", type="primary")
    if open_record:
        if application_matches_vendor(app_id, email, DEFAULT_DB_PATH):
            st.session_state["tracking_application"] = app_id.strip().upper()
        else:
            st.error("Application ID and registered email did not match. Check both values and try again.")
    selected = st.session_state.get("tracking_application")
    if selected:
        app = get_application(selected, DEFAULT_DB_PATH)
        if not app:
            st.error("Application could not be loaded.")
            return
        render_activity_detail(app)
        st.markdown("### Submit a correction")
        st.caption("Edit the fields that need correction and replace any document by uploading a new PDF in that category. Unchanged documents and prior revisions remain in the audit history.")
        render_vendor_form(app["snapshot"], selected)


def admin_portal() -> None:
    page_header("Procurement workspace", "Review applications, inspect evidence, record human decisions, and preserve the full audit history.")
    if not st.session_state.get("admin_authenticated"):
        st.warning("Demonstration access only. This sample password gate is not production authentication.")
        with st.form("admin_login"):
            username = st.text_input("Reviewer name", value="Procurement Reviewer")
            password = st.text_input("Demo admin password", type="password")
            submit = st.form_submit_button("Sign in", type="primary")
        expected = get_setting("VENDORFLOW_ADMIN_PASSWORD")
        if not expected and uses_supabase_database(DEFAULT_DB_PATH, DEFAULT_DB_PATH):
            st.error("Admin access is locked until VENDORFLOW_ADMIN_PASSWORD is set in Streamlit secrets.")
            return
        expected = expected or "demo-admin"
        if submit:
            if hmac.compare_digest(password, expected):
                st.session_state["admin_authenticated"] = True
                st.session_state["reviewer_name"] = username.strip() or "Procurement Reviewer"
                st.rerun()
            st.error("The demo password did not match.")
        return

    counts = dashboard_counts(DEFAULT_DB_PATH)
    metrics = list(counts.items())
    columns = st.columns(len(metrics))
    for column, (label, value) in zip(columns, metrics):
        column.markdown(f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value}</div></div>', unsafe_allow_html=True)
    st.markdown("### Application queue")
    filters = st.columns([1.5, 1, 1, 1])
    search = filters[0].text_input("Search company or application ID", key="admin_search")
    status_filter = filters[1].selectbox("Status", ["All", "Approved", "Pending", "Rejected"])
    risk_filter = filters[2].selectbox("Risk", ["All", "Low", "Medium", "High"])
    if filters[3].button("Sign out", use_container_width=True):
        st.session_state["admin_authenticated"] = False
        st.rerun()
    records = list_applications(DEFAULT_DB_PATH, search, status_filter, risk_filter)
    st.dataframe([{"Application ID": r["application_id"], "Vendor name": r["vendor_name"], "Submission date": r["submitted_at"], "Status": r["status"], "Risk": r["risk_category"], "Issues": r["issue_count"], "Last updated": r["updated_at"], "Revision": r["revision"]} for r in records], use_container_width=True, hide_index=True)
    if not records:
        st.info("No applications match the current filters.")
        return
    choices = [f"{r['application_id']} · {r['vendor_name']} · {r['status']}" for r in records]
    selected_label = st.selectbox("Open application", choices)
    selected_id = selected_label.split(" · ")[0]
    app = get_application(selected_id, DEFAULT_DB_PATH)
    if app:
        render_activity_detail(app)
        st.markdown("### Human review action")
        if app["status"] == "Pending":
            with st.form(f"review_{selected_id}"):
                action = st.selectbox("Action", ["Approve", "Reject", "Request information"])
                reason = st.text_area("Decision reason (required)", placeholder="Describe evidence reviewed and why this action is appropriate.")
                confirm = st.form_submit_button("Record reviewer decision", type="primary")
            if confirm:
                try:
                    reviewer_decision(selected_id, action, st.session_state.get("reviewer_name", "Procurement Reviewer"), reason, DEFAULT_DB_PATH)
                    st.success("Decision recorded. The automated recommendation remains in the application history.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        else:
            st.info("Manual review actions are available when an application is Pending. A future correction can create a new verification run.")


def demo_scenarios() -> None:
    page_header("Demo scenarios", "Run real submissions through the same validation and persistence pipeline used by the Vendor Portal.")
    st.markdown('<span class="demo-label">FICTIONAL DATA · DEMO VERIFICATION</span>', unsafe_allow_html=True)
    st.write("Each scenario creates an application, extracts generated text PDFs, checks the actual SQLite application history, applies the policy, and saves the execution trace. No status is hard-coded.")
    cards = [
        ("complete", "Complete submission", "Expected: Approved · all required Indian documents and values align."),
        ("missing_certificate", "Missing certificate", "Expected: Pending · GST certificate required. You can add it to a new revision."),
        ("bank_mismatch", "Bank holder mismatch", "Expected: Pending · significant account holder/company conflict; manual review."),
        ("duplicate_tax_id", "Duplicate tax ID", "Expected: Pending · matching GSTIN points to a related application."),
    ]
    for name, title, desc in cards:
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            col1.markdown(f"#### {title}")
            col1.caption(desc)
            if col2.button(f"Run scenario", key=f"run_{name}", type="primary", use_container_width=True):
                _, callback = render_progress()
                with st.status(f"Running {title.lower()}…", expanded=True):
                    result = run_demo_scenario(name, DEFAULT_DB_PATH, UPLOAD_ROOT, callback)
                st.session_state["last_result"] = result
                st.session_state["last_application_id"] = result["application_id"]
                st.success(f"Scenario finished: {result['status']} · {result['application_id']}")
                render_result(result)
                if name == "missing_certificate":
                    st.session_state["correct_missing_app"] = result["application_id"]
    missing_id = st.session_state.get("correct_missing_app")
    if missing_id:
        st.markdown("### Continue the correction journey")
        st.caption(f"Application `{missing_id}` is Pending with the missing certificate finding. Add the certificate as revision 2 and run verification again.")
        if st.button("Add missing certificate and re-verify", type="primary", key="complete_missing_scenario"):
            _, callback = render_progress()
            with st.status("Saving correction and re-running checks…", expanded=True):
                result = correct_missing_certificate(missing_id, DEFAULT_DB_PATH, UPLOAD_ROOT, callback)
            st.session_state["last_result"] = result
            st.session_state["last_application_id"] = result["application_id"]
            st.success(f"Correction run finished: {result['status']} · revision {result['revision_no']}")
            render_result(result)


def about_page() -> None:
    page_header("How VendorFlow works", "A local-first onboarding workflow with transparent rules and a reviewable audit trail.")
    st.markdown("""
    <div class="topline"><span class="demo-label">DEMO VERIFICATION</span><p style="margin:.8rem 0 0">VendorFlow checks document text and application consistency. It does not connect to government registries, banks, tax authorities, email providers, or identity services.</p></div>
    """, unsafe_allow_html=True)
    st.markdown(f"**AI capability:** {ai_capability_label()}")
    st.markdown("### Workflow")
    st.write("1. Collect vendor form data and PDF evidence.  2. Extract literal values from readable PDF text.  3. Apply country-specific format and required-field rules.  4. Compare submitted and extracted values.  5. Search prior applications for possible duplicates.  6. Assign explainable risk and an Approved/Pending recommendation.  7. Save a communication draft and audit events.")
    st.markdown("### Decision policy")
    st.write("Approved means the configured demo checks passed. Missing, invalid, unreadable, mismatched, or potentially duplicate data stays Pending for correction or human review. The automated rules do not reject vendors; a reviewer can record a reasoned rejection. Human decisions append to the decision history without replacing the automated recommendation.")
    st.markdown("### Limitations")
    st.write("Scanned PDFs do not run OCR and are flagged for review. Indian GSTIN, PAN, and IFSC checks validate format only. The bank account number is stored as a one-way fingerprint plus a masked suffix, while uploaded files remain in the local uploads directory. The demonstration login is not production authentication or a compliance control. Email drafts are never sent automatically.")
    st.markdown("### AI extraction")
    st.write("Optional OpenAI-assisted extraction is disabled by default. When explicitly enabled and configured, the model may suggest fields, but a candidate is stored only if the exact text appears in its source PDF. Deterministic local extraction remains active. Follow-up messages are template-generated and never sent.")


try:
    initialize_app()
except StorageError as exc:
    st.error(f"VendorFlow could not initialize persistent storage. {exc}")
    st.stop()
with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">◈</span>VendorFlow AI</div>', unsafe_allow_html=True)
    st.caption("Intelligent vendor onboarding")
    st.markdown('<span class="demo-label">DEMO VERIFICATION</span>', unsafe_allow_html=True)
    selection = st.radio("Workspace", ["Vendor Portal", "Track / Correct", "Procurement Admin", "Demo Scenarios", "How it works"], label_visibility="collapsed")
    st.markdown("---")
    active_storage = "Supabase Postgres · private PDF storage" if uses_supabase_database(DEFAULT_DB_PATH, DEFAULT_DB_PATH) else "SQLite · local PDF uploads"
    st.caption(f"{active_storage} · explainable policy")
    st.caption(ai_capability_label())

if selection == "Vendor Portal":
    vendor_portal()
elif selection == "Track / Correct":
    track_application()
elif selection == "Procurement Admin":
    admin_portal()
elif selection == "Demo Scenarios":
    demo_scenarios()
else:
    about_page()
