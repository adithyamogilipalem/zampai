"""End-to-end onboarding workflow and human-review operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .db import DEFAULT_DB_PATH, database, initialize_database, json_dump, json_load, query_all, query_one
from .documents import DocumentError, extract_document, sha256_bytes, validate_pdf_upload
from .validation import compare_documents, determine_risk_and_decision, normalized_identifier, validate_form


STAGES = ["Document extraction", "Form validation", "Cross-document comparison", "Duplicate search", "Risk assessment", "Decision and follow-up"]
UploadMap = dict[str, tuple[str, bytes, str]]
ProgressCallback = Callable[[str, str, str, dict[str, Any]], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _fingerprint(account_number: str) -> str:
    digits = re.sub(r"\s+", "", account_number or "").casefold()
    return hashlib.sha256(digits.encode("utf-8")).hexdigest() if digits else ""


def _normalize_form(form: dict[str, Any]) -> dict[str, str]:
    keys = (
        "registered_name", "trading_name", "business_type", "country", "registered_address", "state", "city", "postal_code",
        "contact_name", "designation", "email", "phone", "gstin", "pan", "account_holder_name", "bank_name", "account_number", "ifsc",
    )
    result = {key: str(form.get(key) or "").strip() for key in keys}
    result["email"] = result["email"].strip().lower()
    result["gstin"] = normalized_identifier(result["gstin"])
    result["pan"] = normalized_identifier(result["pan"])
    result["ifsc"] = normalized_identifier(result["ifsc"])
    result["account_fingerprint"] = _fingerprint(result.pop("account_number"))
    return result


def _upload_tuple(value: Any) -> tuple[str, bytes, str]:
    if isinstance(value, tuple) and len(value) == 3:
        name, data, mime = value
        return str(name), bytes(data), str(mime or "application/pdf")
    if isinstance(value, tuple) and len(value) == 2:
        name, data = value
        return str(name), bytes(data), "application/pdf"
    raise DocumentError("Each upload must include a filename and PDF bytes.")


def _write_doc(doc_root: Path, application_id: str, revision_no: int, doc_type: str, upload: Any) -> dict[str, Any]:
    name, data, mime = _upload_tuple(upload)
    validate_pdf_upload(name, data, mime)
    doc_id = new_id("DOC")
    folder = doc_root / application_id / f"revision_{revision_no}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{doc_id}.pdf"
    path.write_bytes(data)
    return {
        "document_id": doc_id, "document_type": doc_type, "original_name": Path(name).name,
        "stored_path": str(path.resolve()), "mime_type": "application/pdf", "size_bytes": len(data), "sha256": sha256_bytes(data),
    }


def _snapshot_for_storage(form: dict[str, Any]) -> dict[str, Any]:
    snapshot = _normalize_form(form)
    # Raw account numbers are never retained in application snapshots or audit events.
    digits = re.sub(r"\s+", "", str(form.get("account_number") or ""))
    snapshot["account_number_present"] = bool(digits)
    snapshot["account_number_masked"] = f"••••{digits[-4:]}" if digits else ""
    return snapshot


def _request_signature(snapshot: dict[str, Any], uploads: UploadMap) -> str:
    docs = []
    for doc_type, upload in sorted(uploads.items()):
        name, data, mime = _upload_tuple(upload)
        docs.append({"type": doc_type, "sha256": sha256_bytes(data)})
    content = json_dump({"snapshot": snapshot, "documents": docs})
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _duplicate_submission_result(conn: Any, request_row: Any) -> dict[str, Any]:
    app_id = request_row["application_id"]
    app = conn.execute("SELECT * FROM applications WHERE application_id=?", (app_id,)).fetchone()
    run = conn.execute("SELECT * FROM verification_runs WHERE application_id=? AND revision_no=? ORDER BY started_at DESC LIMIT 1", (app_id, request_row["revision_no"])).fetchone()
    decision = conn.execute("SELECT reason FROM decisions WHERE run_id=? AND decision_type LIKE 'automated%' ORDER BY created_at DESC LIMIT 1", (run["run_id"],)).fetchone() if run else None
    reason = decision["reason"] if decision else "The original request is still being processed."
    status = app["status"] if app else "Processing"
    return {
        "application_id": app_id,
        "run_id": run["run_id"] if run else None,
        "revision_no": request_row["revision_no"],
        "status": status,
        "risk_category": app["risk_category"] if app else "Medium",
        "reason": f"An identical submit request was already recorded. Showing the existing application. {reason}",
        "recommended_action": "Open the existing application to review its saved result.",
        "issues": [],
        "checks_passed": 0,
        "checks_total": 0,
        "duplicate_request": True,
    }


def _recent_request(conn: Any, signature: str) -> Any:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM submission_requests WHERE request_signature=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
        (signature, cutoff),
    ).fetchone()


def _audit(conn: Any, application_id: str | None, event_type: str, actor: str, details: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO audit_events(event_id,application_id,event_type,actor,details_json,created_at) VALUES(?,?,?,?,?,?)",
        (new_id("AUD"), application_id, event_type, actor, json_dump(details), utc_now()),
    )


def _insert_docs(conn: Any, application_id: str, revision_no: int, docs: list[dict[str, Any]]) -> None:
    for doc in docs:
        conn.execute(
            """INSERT INTO uploaded_documents(document_id,application_id,revision_no,document_type,original_name,stored_path,mime_type,size_bytes,sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (doc["document_id"], application_id, revision_no, doc["document_type"], doc["original_name"], doc["stored_path"], doc["mime_type"], doc["size_bytes"], doc["sha256"], utc_now()),
        )


def _stage_start(db_path: str | Path, run_id: str, application_id: str, stage: str, callback: ProgressCallback | None, details: dict[str, Any] | None = None) -> None:
    now = utc_now()
    with database(db_path) as conn:
        conn.execute("UPDATE verification_checks SET status='Running',started_at=? WHERE run_id=? AND stage_name=? AND check_name='__stage__'", (now, run_id, stage))
        _audit(conn, application_id, "workflow_stage_started", "system", {"stage": stage, "run_id": run_id})
    if callback:
        callback(stage, "Running", now, details or {})


def _stage_finish(db_path: str | Path, run_id: str, application_id: str, stage: str, status: str, findings: dict[str, Any], callback: ProgressCallback | None, error: str = "") -> None:
    now = utc_now()
    with database(db_path) as conn:
        conn.execute("UPDATE verification_checks SET status=?,completed_at=?,findings_json=?,error=? WHERE run_id=? AND stage_name=? AND check_name='__stage__'", (status, now, json_dump(findings), error, run_id, stage))
        _audit(conn, application_id, "workflow_stage_finished", "system", {"stage": stage, "run_id": run_id, "status": status, "findings": findings})
    if callback:
        callback(stage, status, now, findings)


def _insert_checks(db_path: str | Path, run_id: str, stage: str, checks: list[dict[str, Any]]) -> None:
    with database(db_path) as conn:
        for check in checks:
            conn.execute(
                """INSERT INTO verification_checks(check_id,run_id,stage_name,check_name,status,started_at,completed_at,findings_json,error)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("CHK"), run_id, stage, check["name"], check["status"], utc_now(), utc_now(), json_dump(check), ""),
            )


def _generated_message(snapshot: dict[str, Any], decision: dict[str, Any], app_id: str) -> tuple[str, str, str]:
    status = decision["status"]
    issues = decision.get("issues") or []
    recipient = str(snapshot.get("email") or "")
    name = str(snapshot.get("contact_name") or "Vendor")
    if status == "Approved":
        subject = f"Vendor onboarding approved — {app_id}"
        body = f"Hello {name},\n\nYour vendor onboarding application {app_id} has been approved under our demo verification policy. The checks completed and the submitted information is recorded in the application history. No live government, identity, or bank verification was performed.\n\nRegards,\nProcurement Team"
        kind = "approval_notice_draft"
    elif status == "Rejected":
        subject = f"Vendor onboarding decision — {app_id}"
        body = f"Hello {name},\n\nYour vendor onboarding application {app_id} was rejected after reviewer assessment.\n\nReason: {decision.get('reason', 'See the application history for details.')}\n\nPlease contact the procurement team if you need clarification.\n\nRegards,\nProcurement Team"
        kind = "rejection_notice_draft"
    else:
        subject = f"Action requested for vendor application {app_id}"
        issue_lines = "\n".join(f"• {item}" for item in issues) if issues else "• Procurement review is needed before this application can proceed."
        body = f"Hello {name},\n\nYour vendor onboarding application {app_id} is currently Pending. Please review the following items and submit corrected information or supporting evidence:\n\n{issue_lines}\n\nYou can update the application in the Vendor Portal using your application ID and registered email address. After resubmission, verification will run again and the full history will be retained. No external registry or bank verification has occurred.\n\nRegards,\nProcurement Team"
        kind = "correction_request_draft"
    return kind, subject, body


def _save_communication(db_path: str | Path, app_id: str, revision_no: int, snapshot: dict[str, Any], decision: dict[str, Any]) -> None:
    kind, subject, body = _generated_message(snapshot, decision, app_id)
    with database(db_path) as conn:
        conn.execute(
            "INSERT INTO communications(communication_id,application_id,revision_no,message_type,recipient,subject,body,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("MSG"), app_id, revision_no, kind, snapshot.get("email", ""), subject, body, utc_now()),
        )


def _copy_previous_docs(db_path: str | Path, app_id: str, previous_revision: int, revision: int, replacements: dict[str, Any], doc_root: Path) -> list[dict[str, Any]]:
    previous = query_all("SELECT * FROM uploaded_documents WHERE application_id=? AND revision_no=?", (app_id, previous_revision), db_path)
    docs: list[dict[str, Any]] = []
    replaced = set(replacements)
    for row in previous:
        if row["document_type"] in replaced:
            continue
        docs.append({
            "document_id": new_id("DOC"), "document_type": row["document_type"], "original_name": row["original_name"],
            "stored_path": row["stored_path"], "mime_type": row["mime_type"], "size_bytes": row["size_bytes"], "sha256": row["sha256"],
        })
    for doc_type, upload in replacements.items():
        docs.append(_write_doc(doc_root, app_id, revision, doc_type, upload))
    return docs


def process_submission(
    form: dict[str, Any],
    uploads: UploadMap,
    db_path: str | Path | None = None,
    upload_root: str | Path = "data/uploads",
    application_id: str | None = None,
    submitted_by: str = "vendor",
    change_summary: str = "Initial submission",
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Persist a submission/revision, run each check, and save an explainable result."""
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    initialize_database(db_path)
    doc_root = Path(upload_root)
    # Reject unsafe uploads before creating an application or revision.
    for name, value in uploads.items():
        filename, content, mime = _upload_tuple(value)
        validate_pdf_upload(filename, content, mime)

    snapshot = _snapshot_for_storage(form)
    request_signature = _request_signature(snapshot, uploads)
    with database(db_path) as conn:
        prior_request = _recent_request(conn, request_signature)
        if prior_request:
            return _duplicate_submission_result(conn, prior_request)
    now = utc_now()
    if application_id:
        with database(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior_request = _recent_request(conn, request_signature)
            if prior_request:
                return _duplicate_submission_result(conn, prior_request)
            app = conn.execute("SELECT * FROM applications WHERE application_id=?", (application_id,)).fetchone()
            if not app:
                raise ValueError("Application was not found.")
            revision_no = int(app["current_revision"]) + 1
            previous_snapshot_row = conn.execute("SELECT snapshot_json FROM application_revisions WHERE application_id=? AND revision_no=?", (application_id, app["current_revision"])).fetchone()
            previous_snapshot = json_load(previous_snapshot_row["snapshot_json"] if previous_snapshot_row else "{}")
            if not snapshot["account_number_present"] and previous_snapshot.get("account_number_present"):
                snapshot["account_fingerprint"] = previous_snapshot.get("account_fingerprint", "")
                snapshot["account_number_masked"] = previous_snapshot.get("account_number_masked", "")
                snapshot["account_number_present"] = True
            docs = _copy_previous_docs(db_path, application_id, int(app["current_revision"]), revision_no, uploads, doc_root)
            conn.execute("INSERT INTO application_revisions(application_id,revision_no,snapshot_json,submitted_at,submitted_by,change_summary) VALUES(?,?,?,?,?,?)", (application_id, revision_no, json_dump(snapshot), now, submitted_by, change_summary))
            _insert_docs(conn, application_id, revision_no, docs)
            conn.execute("UPDATE applications SET current_revision=?,status='Processing',updated_at=? WHERE application_id=?", (revision_no, now, application_id))
            conn.execute("UPDATE vendors SET registered_name=?,trading_name=?,email=?,country=? WHERE vendor_id=?", (snapshot["registered_name"], snapshot["trading_name"], snapshot["email"], snapshot["country"], app["vendor_id"]))
            conn.execute("INSERT INTO submission_requests(request_signature,application_id,revision_no,created_at) VALUES(?,?,?,?)", (request_signature, application_id, revision_no, now))
            _audit(conn, application_id, "application_revision_submitted", submitted_by, {"revision": revision_no, "change_summary": change_summary, "replaced_documents": sorted(uploads)})
    else:
        application_id = new_id("VF")
        vendor_id = new_id("VEN")
        revision_no = 1
        with database(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior_request = _recent_request(conn, request_signature)
            if prior_request:
                return _duplicate_submission_result(conn, prior_request)
            docs = [_write_doc(doc_root, application_id, revision_no, doc_type, upload) for doc_type, upload in uploads.items()]
            conn.execute("INSERT INTO vendors(vendor_id,registered_name,trading_name,email,country,created_at) VALUES(?,?,?,?,?,?)", (vendor_id, snapshot["registered_name"], snapshot["trading_name"], snapshot["email"], snapshot["country"], now))
            conn.execute("INSERT INTO applications(application_id,vendor_id,status,risk_category,current_revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (application_id, vendor_id, "Processing", "Medium", 1, now, now))
            conn.execute("INSERT INTO application_revisions(application_id,revision_no,snapshot_json,submitted_at,submitted_by,change_summary) VALUES(?,?,?,?,?,?)", (application_id, 1, json_dump(snapshot), now, submitted_by, "Initial submission"))
            _insert_docs(conn, application_id, revision_no, docs)
            conn.execute("INSERT INTO submission_requests(request_signature,application_id,revision_no,created_at) VALUES(?,?,?,?)", (request_signature, application_id, revision_no, now))
            _audit(conn, application_id, "application_submitted", submitted_by, {"revision": 1, "document_types": sorted(uploads)})

    run_id = new_id("RUN")
    with database(db_path) as conn:
        conn.execute("INSERT INTO verification_runs(run_id,application_id,revision_no,started_at,status) VALUES(?,?,?,?,?)", (run_id, application_id, revision_no, utc_now(), "Running"))
        for stage in STAGES:
            conn.execute("INSERT INTO verification_checks(check_id,run_id,stage_name,check_name,status,started_at,findings_json) VALUES(?,?,?,?,?,?,?)", (new_id("CHK"), run_id, stage, "__stage__", "Not Started", "", "{}"))
        conn.execute("UPDATE applications SET latest_run_id=?,updated_at=? WHERE application_id=?", (run_id, utc_now(), application_id))

    try:
        docs_rows = query_all("SELECT * FROM uploaded_documents WHERE application_id=? AND revision_no=?", (application_id, revision_no), db_path)
        doc_records = [dict(row) for row in docs_rows]
        extracted_by_type: dict[str, dict[str, str]] = {}
        unreadable: set[str] = set()
        _stage_start(db_path, run_id, application_id, STAGES[0], on_progress)
        extraction_findings = []
        for row in doc_records:
            outcome = extract_document(row["stored_path"], ai_enabled=os.getenv("VENDORFLOW_ENABLE_AI", "false").lower() in {"1", "true", "yes"})
            fields = outcome["fields"]
            extracted_by_type.setdefault(row["document_type"], {}).update(fields)
            if outcome["limitation"]:
                unreadable.add(row["document_type"])
            if not outcome["text"]:
                unreadable.add(row["document_type"])
            for field, value in fields.items():
                with database(db_path) as conn:
                    conn.execute("INSERT INTO extracted_fields(extracted_field_id,application_id,revision_no,run_id,document_id,field_name,field_value,extraction_method,confidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_id("EXT"), application_id, revision_no, run_id, row["document_id"], field, value, outcome["methods"].get(field, "local_pdf_text"), 1.0 if outcome["methods"].get(field) == "local_pdf_text" else None, utc_now()))
            extraction_findings.append({"document_type": row["document_type"], "filename": row["original_name"], "fields_found": sorted(fields), "limitation": outcome["limitation"], "ai_state": outcome["ai_state"]})
        missing_extractions = len(unreadable)
        _stage_finish(db_path, run_id, application_id, STAGES[0], "Needs Review" if missing_extractions else "Completed", {"documents_processed": len(doc_records), "documents_needing_review": missing_extractions, "documents": extraction_findings}, on_progress)

        _stage_start(db_path, run_id, application_id, STAGES[1], on_progress)
        form_checks = validate_form(snapshot, doc_records)
        _insert_checks(db_path, run_id, STAGES[1], form_checks)
        _stage_finish(db_path, run_id, application_id, STAGES[1], "Needs Review" if any(c["status"] != "Pass" for c in form_checks) else "Completed", {"checks": len(form_checks), "failed": sum(c["status"] == "Fail" for c in form_checks), "passed": sum(c["status"] == "Pass" for c in form_checks)}, on_progress)

        _stage_start(db_path, run_id, application_id, STAGES[2], on_progress)
        comparisons = compare_documents(snapshot, extracted_by_type, unreadable)
        _insert_checks(db_path, run_id, STAGES[2], comparisons)
        _stage_finish(db_path, run_id, application_id, STAGES[2], "Needs Review" if any(c["status"] != "Pass" for c in comparisons) else "Completed", {"comparisons": len(comparisons), "warnings": sum(c["status"] == "Warning" for c in comparisons)}, on_progress)

        _stage_start(db_path, run_id, application_id, STAGES[3], on_progress)
        duplicates = _find_duplicates(snapshot, application_id, db_path)
        duplicate_checks = [{"name": f"Potential duplicate — {d['field']}", "status": "Warning", "message": d["message"], "related_application_id": d["related_application_id"], "category": "duplicate"} for d in duplicates]
        _insert_checks(db_path, run_id, STAGES[3], duplicate_checks or [{"name": "Database duplicate search", "status": "Pass", "message": "No matching tax identifier or bank fingerprint was found in prior applications.", "category": "duplicate"}])
        _stage_finish(db_path, run_id, application_id, STAGES[3], "Needs Review" if duplicates else "Completed", {"potential_duplicates": duplicates}, on_progress)

        _stage_start(db_path, run_id, application_id, STAGES[4], on_progress)
        decision = determine_risk_and_decision(form_checks, comparisons, duplicates)
        _stage_finish(db_path, run_id, application_id, STAGES[4], "Needs Review" if decision["risk_category"] != "Low" else "Completed", {"risk_category": decision["risk_category"], "contributors": decision["issues"]}, on_progress)

        _stage_start(db_path, run_id, application_id, STAGES[5], on_progress)
        summary = f"{decision['status']} — {decision['reason']}"
        final_at = utc_now()
        with database(db_path) as conn:
            conn.execute("UPDATE verification_runs SET completed_at=?,status=?,risk_category=?,summary=? WHERE run_id=?", (final_at, decision["status"], decision["risk_category"], summary, run_id))
            conn.execute("UPDATE applications SET status=?,risk_category=?,automated_recommendation=?,updated_at=? WHERE application_id=?", (decision["status"], decision["risk_category"], decision["status"], final_at, application_id))
            conn.execute("INSERT INTO decisions(decision_id,application_id,run_id,decision_type,status,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (new_id("DEC"), application_id, run_id, "automated", decision["status"], "VendorFlow rules", decision["reason"], final_at))
            _audit(conn, application_id, "automated_decision_recorded", "system", {"run_id": run_id, "status": decision["status"], "risk_category": decision["risk_category"], "reason": decision["reason"]})
        _save_communication(db_path, application_id, revision_no, snapshot, decision)
        _stage_finish(db_path, run_id, application_id, STAGES[5], "Completed", {"decision": decision["status"], "message_draft_saved": True}, on_progress)
        decision.update({"application_id": application_id, "run_id": run_id, "revision_no": revision_no, "summary": summary})
        return decision
    except Exception as exc:
        safe_error = f"Workflow stopped safely ({type(exc).__name__}). Check the stored run and correct the submission before retrying."
        failed_at = utc_now()
        with database(db_path) as conn:
            active_stage = conn.execute("SELECT stage_name FROM verification_checks WHERE run_id=? AND check_name='__stage__' AND status='Running' ORDER BY rowid LIMIT 1", (run_id,)).fetchone()
            if active_stage:
                conn.execute("UPDATE verification_checks SET status='Failed',completed_at=?,error=?,findings_json=? WHERE run_id=? AND stage_name=? AND check_name='__stage__'", (failed_at, safe_error, json_dump({"error": safe_error}), run_id, active_stage["stage_name"]))
                _audit(conn, application_id, "workflow_stage_failed", "system", {"stage": active_stage["stage_name"], "run_id": run_id, "error_type": type(exc).__name__})
            conn.execute("UPDATE verification_runs SET completed_at=?,status='Pending',summary=? WHERE run_id=?", (failed_at, safe_error, run_id))
            conn.execute("UPDATE applications SET status='Pending',risk_category='Medium',automated_recommendation='Pending',updated_at=? WHERE application_id=?", (failed_at, application_id))
            conn.execute("INSERT INTO decisions(decision_id,application_id,run_id,decision_type,status,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (new_id("DEC"), application_id, run_id, "automated_error", "Pending", "VendorFlow rules", safe_error, failed_at))
            _audit(conn, application_id, "workflow_error", "system", {"run_id": run_id, "error_type": type(exc).__name__})
        if on_progress and active_stage:
            on_progress(active_stage["stage_name"], "Failed", failed_at, {"error": safe_error})
        if on_progress:
            on_progress("Workflow error", "Failed", failed_at, {"error": safe_error})
        return {"application_id": application_id, "run_id": run_id, "revision_no": revision_no, "status": "Pending", "risk_category": "Medium", "reason": safe_error, "recommended_action": "Review the application and restart verification after resolving the processing issue.", "issues": [safe_error], "checks_passed": 0, "checks_total": 0}


def recover_interrupted_runs(db_path: str | Path | None = None, stale_after_seconds: int = 300) -> int:
    """Turn stale in-flight runs into visible Pending records after a process restart."""
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat(timespec="seconds")
    recovered = 0
    now = utc_now()
    with database(db_path) as conn:
        rows = conn.execute("SELECT * FROM verification_runs WHERE status='Running' AND started_at<?", (cutoff,)).fetchall()
        for run in rows:
            reason = "Verification was interrupted before completion. The application was moved to Pending for a safe manual retry."
            conn.execute("UPDATE verification_checks SET status='Failed',completed_at=?,error=?,findings_json=? WHERE run_id=? AND check_name='__stage__' AND status='Running'", (now, reason, json_dump({"error": reason}), run["run_id"]))
            conn.execute("UPDATE verification_runs SET status='Pending',completed_at=?,summary=? WHERE run_id=?", (now, reason, run["run_id"]))
            conn.execute("UPDATE applications SET status='Pending',risk_category='Medium',automated_recommendation='Pending',updated_at=? WHERE application_id=? AND latest_run_id=?", (now, run["application_id"], run["run_id"]))
            exists = conn.execute("SELECT 1 FROM decisions WHERE run_id=? AND decision_type='automated_error'", (run["run_id"],)).fetchone()
            if not exists:
                conn.execute("INSERT INTO decisions(decision_id,application_id,run_id,decision_type,status,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (new_id("DEC"), run["application_id"], run["run_id"], "automated_error", "Pending", "VendorFlow recovery", reason, now))
            _audit(conn, run["application_id"], "workflow_interruption_recovered", "system", {"run_id": run["run_id"], "reason": reason})
            recovered += 1
    return recovered


def _find_duplicates(snapshot: dict[str, Any], application_id: str, db_path: str | Path) -> list[dict[str, Any]]:
    rows = query_all(
        """SELECT a.application_id,a.current_revision,r.snapshot_json FROM applications a
           JOIN application_revisions r ON r.application_id=a.application_id AND r.revision_no=a.current_revision
           WHERE a.application_id<>?""",
        (application_id,), db_path,
    )
    duplicates: list[dict[str, Any]] = []
    candidates = (("GSTIN", normalized_identifier(snapshot.get("gstin", "")), "gstin"), ("PAN", normalized_identifier(snapshot.get("pan", "")), "pan"), ("bank account", snapshot.get("account_fingerprint", ""), "account_fingerprint"))
    for label, value, key in candidates:
        if not value:
            continue
        for row in rows:
            other = json_load(row["snapshot_json"])
            other_value = normalized_identifier(other.get(key, "")) if key != "account_fingerprint" else other.get(key, "")
            if value == other_value:
                duplicates.append({"field": label, "related_application_id": row["application_id"], "message": f"This {label} already appears on application {row['application_id']}; this is a potential duplicate that needs human review."})
                break
    return duplicates


def get_application(application_id: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    app = query_one("SELECT * FROM applications WHERE application_id=?", (application_id.strip().upper(),), db_path)
    if not app:
        return None
    result = dict(app)
    result["snapshot"] = json_load(query_one("SELECT snapshot_json FROM application_revisions WHERE application_id=? AND revision_no=?", (application_id.strip().upper(), app["current_revision"]), db_path)["snapshot_json"])
    result["documents"] = [dict(row) for row in query_all("SELECT * FROM uploaded_documents WHERE application_id=? AND revision_no=? ORDER BY created_at", (application_id.strip().upper(), app["current_revision"]), db_path)]
    result["extracted_fields"] = [dict(row) for row in query_all("SELECT e.*,d.document_type,d.original_name FROM extracted_fields e JOIN uploaded_documents d ON e.document_id=d.document_id WHERE e.run_id=?", (app["latest_run_id"],), db_path)] if app["latest_run_id"] else []
    result["checks"] = [dict(row) | {"findings": json_load(row["findings_json"])} for row in query_all("SELECT * FROM verification_checks WHERE run_id=? ORDER BY rowid", (app["latest_run_id"],), db_path)] if app["latest_run_id"] else []
    result["decisions"] = [dict(row) for row in query_all("SELECT * FROM decisions WHERE application_id=? ORDER BY created_at", (application_id.strip().upper(),), db_path)]
    result["revisions"] = [dict(row) | {"snapshot": json_load(row["snapshot_json"])} for row in query_all("SELECT * FROM application_revisions WHERE application_id=? ORDER BY revision_no", (application_id.strip().upper(),), db_path)]
    result["reviewer_actions"] = [dict(row) for row in query_all("SELECT * FROM reviewer_actions WHERE application_id=? ORDER BY created_at", (application_id.strip().upper(),), db_path)]
    result["communications"] = [dict(row) for row in query_all("SELECT * FROM communications WHERE application_id=? ORDER BY created_at DESC", (application_id.strip().upper(),), db_path)]
    result["audit"] = [dict(row) | {"details": json_load(row["details_json"])} for row in query_all("SELECT * FROM audit_events WHERE application_id=? ORDER BY created_at", (application_id.strip().upper(),), db_path)]
    return result


def application_matches_vendor(application_id: str, email: str, db_path: str | Path | None = None) -> bool:
    app = get_application(application_id, db_path)
    return bool(app and str(app["snapshot"].get("email", "")).casefold() == (email or "").strip().casefold())


def list_applications(db_path: str | Path | None = None, search: str = "", status: str = "All", risk: str = "All") -> list[dict[str, Any]]:
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    rows = query_all("""SELECT a.*,v.registered_name,v.email,r.snapshot_json FROM applications a JOIN vendors v ON a.vendor_id=v.vendor_id JOIN application_revisions r ON r.application_id=a.application_id AND r.revision_no=a.current_revision ORDER BY a.created_at DESC""", (), db_path)
    result = []
    for row in rows:
        snapshot = json_load(row["snapshot_json"])
        term = search.casefold().strip()
        if term and term not in row["application_id"].casefold() and term not in row["registered_name"].casefold() and term not in row["email"].casefold():
            continue
        if status != "All" and row["status"] != status:
            continue
        if risk != "All" and row["risk_category"] != risk:
            continue
        issues = query_one("SELECT COUNT(*) AS n FROM verification_checks WHERE run_id=? AND check_name<>'__stage__' AND status IN ('Warning','Fail')", (row["latest_run_id"],), db_path)["n"] if row["latest_run_id"] else 0
        result.append({"application_id": row["application_id"], "vendor_name": row["registered_name"], "email": row["email"], "submitted_at": query_one("SELECT submitted_at FROM application_revisions WHERE application_id=? AND revision_no=1", (row["application_id"],), db_path)["submitted_at"], "status": row["status"], "risk_category": row["risk_category"], "issue_count": issues, "updated_at": row["updated_at"], "revision": row["current_revision"], "snapshot": snapshot})
    return result


def reviewer_decision(application_id: str, action: str, reviewer: str, reason: str, db_path: str | Path | None = None) -> None:
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    action = action.strip().lower()
    if action not in {"approve", "reject", "request information"}:
        raise ValueError("Choose approve, reject, or request information.")
    if not reviewer.strip() or len(reason.strip()) < 8:
        raise ValueError("Reviewer name and a reason of at least 8 characters are required.")
    app = query_one("SELECT * FROM applications WHERE application_id=?", (application_id,), db_path)
    if not app:
        raise ValueError("Application was not found.")
    run_id = app["latest_run_id"]
    status = {"approve": "Approved", "reject": "Rejected", "request information": "Pending"}[action]
    now = utc_now()
    with database(db_path) as conn:
        conn.execute("INSERT INTO reviewer_actions(action_id,application_id,run_id,reviewer,action,reason,created_at) VALUES(?,?,?,?,?,?,?)", (new_id("REV"), application_id, run_id, reviewer.strip(), action, reason.strip(), now))
        conn.execute("INSERT INTO decisions(decision_id,application_id,run_id,decision_type,status,actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (new_id("DEC"), application_id, run_id, "human_review", status, reviewer.strip(), reason.strip(), now))
        conn.execute("UPDATE applications SET status=?,updated_at=? WHERE application_id=?", (status, now, application_id))
        _audit(conn, application_id, "reviewer_decision_recorded", reviewer.strip(), {"action": action, "status": status, "reason": reason.strip(), "automated_recommendation_preserved": app["automated_recommendation"]})
    loaded = get_application(application_id, db_path)
    snap = loaded["snapshot"]
    msg_decision = {"status": status, "reason": reason.strip(), "issues": [reason.strip()]}
    _save_communication(db_path, application_id, int(app["current_revision"]), snap, msg_decision)


def dashboard_counts(db_path: str | Path | None = None) -> dict[str, int]:
    result = {key: 0 for key in ("Total applications", "Approved", "Pending", "Rejected", "Awaiting review")}
    rows = query_all("SELECT status,risk_category,latest_run_id FROM applications", (), db_path)
    result["Total applications"] = len(rows)
    for row in rows:
        if row["status"] in result:
            result[row["status"]] += 1
        if row["status"] == "Pending" or row["risk_category"] == "High":
            result["Awaiting review"] += 1
    return result
