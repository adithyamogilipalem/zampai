from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vendorflow.db import database, initialize_database, query_all, query_one
from vendorflow.demo import correct_missing_certificate, demo_documents, demo_form, make_pdf, run_demo_scenario
from vendorflow.workflow import get_application, list_applications, process_submission, recover_interrupted_runs, reviewer_decision


def setup(tmp_path):
    db_path = tmp_path / "vendorflow_test.db"
    uploads = tmp_path / "uploads"
    initialize_database(db_path)
    return db_path, uploads


def test_complete_consistent_vendor_is_approved_and_extraction_is_persisted(tmp_path):
    db_path, uploads = setup(tmp_path)
    result = run_demo_scenario("complete", db_path, uploads)

    assert result["status"] == "Approved"
    assert result["risk_category"] == "Low"
    detail = get_application(result["application_id"], db_path)
    assert detail["status"] == "Approved"
    assert {item["document_type"] for item in detail["documents"]} == {"gst", "pan", "bank"}
    assert {item["field_name"] for item in detail["extracted_fields"]} >= {"registered_company_name", "gstin", "pan", "account_holder_name"}
    assert all(stage["status"] == "Completed" for stage in detail["checks"] if stage["check_name"] == "__stage__")
    assert query_one("SELECT COUNT(*) n FROM decisions WHERE application_id=?", (result["application_id"],), db_path)["n"] == 1


def test_missing_certificate_is_pending_then_correction_creates_approved_revision(tmp_path):
    db_path, uploads = setup(tmp_path)
    result = run_demo_scenario("missing_certificate", db_path, uploads)
    assert result["status"] == "Pending"
    assert any("GST registration certificate is missing" in issue for issue in result["issues"])

    corrected = correct_missing_certificate(result["application_id"], db_path, uploads)
    assert corrected["status"] == "Approved"
    assert corrected["revision_no"] == 2
    detail = get_application(result["application_id"], db_path)
    assert len(detail["revisions"]) == 2
    assert len(query_all("SELECT run_id FROM verification_runs WHERE application_id=?", (result["application_id"],), db_path)) == 2
    assert len(query_all("SELECT event_id FROM audit_events WHERE application_id=? AND event_type='application_revision_submitted'", (result["application_id"],), db_path)) == 1


def test_significant_bank_holder_mismatch_requires_human_review(tmp_path):
    db_path, uploads = setup(tmp_path)
    result = run_demo_scenario("bank_mismatch", db_path, uploads)
    assert result["status"] == "Pending"
    assert result["risk_category"] == "High"
    assert any("Account holder differs" in issue for issue in result["issues"])
    detail = get_application(result["application_id"], db_path)
    assert any(row["check_name"] == "Bank account holder vs company" and row["status"] == "Warning" for row in detail["checks"])


def test_duplicate_tax_identifier_is_flagged_with_related_application(tmp_path):
    db_path, uploads = setup(tmp_path)
    result = run_demo_scenario("duplicate_tax_id", db_path, uploads)
    assert result["status"] == "Pending"
    assert result["risk_category"] == "High"
    related_checks = [row for row in get_application(result["application_id"], db_path)["checks"] if row["check_name"].startswith("Potential duplicate")]
    assert related_checks
    assert "VF-" in related_checks[0]["findings"]["related_application_id"]
    assert result["application_id"] in {row["application_id"] for row in list_applications(db_path)}


def test_sensitive_account_number_is_not_stored_in_application_snapshot(tmp_path):
    db_path, uploads = setup(tmp_path)
    result = run_demo_scenario("complete", db_path, uploads)
    row = query_one("SELECT snapshot_json FROM application_revisions WHERE application_id=?", (result["application_id"],), db_path)
    assert "987654320011" not in row["snapshot_json"]
    assert "••••0011" in row["snapshot_json"]


def test_application_persists_after_a_fresh_python_process(tmp_path):
    db_path, uploads = setup(tmp_path)
    result = run_demo_scenario("complete", db_path, uploads)
    script = "from vendorflow.workflow import get_application; import sys; print(get_application(sys.argv[1], sys.argv[2])['status'])"
    reopened = subprocess.run(
        [sys.executable, "-c", script, result["application_id"], str(db_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert reopened.stdout.strip() == "Approved"


def test_repeated_identical_submission_returns_existing_application(tmp_path):
    db_path, uploads = setup(tmp_path)
    form = demo_form(email="dedupe@aaravprecision.example")
    documents = demo_documents()
    first = process_submission(form, documents, db_path, uploads)
    second = process_submission(form, documents, db_path, uploads)
    assert first["application_id"] == second["application_id"]
    assert second["duplicate_request"] is True
    assert len(list_applications(db_path)) == 1


def test_non_indian_submission_does_not_apply_indian_tax_id_formats(tmp_path):
    db_path, uploads_root = setup(tmp_path)
    form = demo_form(
        registered_name="Cedar Analytics LLC", trading_name="Cedar Analytics", country="United States",
        email="alex@cedaranalytics.example", contact_name="Alex Morgan", gstin="not-an-indian-gstin",
        pan="not-an-indian-pan", ifsc="invalid-routing-code", account_holder_name="Cedar Analytics LLC",
        bank_name="Sample Credit Union", account_number="108765432109",
    )
    docs = {
        "registration": ("business_registration.pdf", make_pdf("BUSINESS REGISTRATION\nRegistered Company Name: Cedar Analytics LLC"), "application/pdf"),
        "bank": ("bank_confirmation.pdf", make_pdf("BANK CONFIRMATION\nAccount Holder Name: Cedar Analytics LLC\nBank Name: Sample Credit Union"), "application/pdf"),
    }
    result = process_submission(form, docs, db_path, uploads_root)
    assert result["status"] == "Approved"
    checks = get_application(result["application_id"], db_path)["checks"]
    names = {row["check_name"] for row in checks}
    assert "GSTIN format" not in names and "PAN format" not in names and "IFSC format" not in names


def test_unreadable_pdf_is_pending_for_manual_review(tmp_path):
    db_path, uploads_root = setup(tmp_path)
    form = demo_form(email="scan@aaravprecision.example")
    docs = demo_documents()
    docs["bank"] = ("scanned_bank.pdf", make_pdf(""), "application/pdf")
    result = process_submission(form, docs, db_path, uploads_root)
    assert result["status"] == "Pending"
    detail = get_application(result["application_id"], db_path)
    extraction_stage = next(row for row in detail["checks"] if row["check_name"] == "__stage__" and row["stage_name"] == "Document extraction")
    assert extraction_stage["status"] == "Needs Review"


def test_human_reviewer_decision_is_appended_and_automated_recommendation_preserved(tmp_path):
    db_path, uploads_root = setup(tmp_path)
    pending = run_demo_scenario("missing_certificate", db_path, uploads_root)
    reviewer_decision(pending["application_id"], "reject", "Priya Reviewer", "Unable to validate the supplied registration evidence.", db_path)
    detail = get_application(pending["application_id"], db_path)
    assert detail["status"] == "Rejected"
    assert detail["automated_recommendation"] == "Pending"
    assert detail["decisions"][-1]["decision_type"] == "human_review"
    assert detail["reviewer_actions"][-1]["reviewer"] == "Priya Reviewer"


def test_workflow_failure_marks_the_active_stage_failed(tmp_path):
    db_path, uploads_root = setup(tmp_path)

    def stop_validation(stage, status, timestamp, details):
        if stage == "Form validation" and status == "Running":
            raise RuntimeError("test interruption")

    result = process_submission(demo_form(email="failed@aaravprecision.example"), demo_documents(), db_path, uploads_root, on_progress=stop_validation)
    assert result["status"] == "Pending"
    detail = get_application(result["application_id"], db_path)
    stage = next(row for row in detail["checks"] if row["stage_name"] == "Form validation" and row["check_name"] == "__stage__")
    assert stage["status"] == "Failed"
    assert stage["error"]


def test_stale_in_flight_run_is_recovered_as_pending(tmp_path):
    db_path, uploads_root = setup(tmp_path)
    result = process_submission(demo_form(email="restart@aaravprecision.example"), demo_documents(), db_path, uploads_root)
    with database(db_path) as conn:
        conn.execute("UPDATE verification_runs SET status='Running',completed_at=NULL,started_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (result["run_id"],))
        conn.execute("UPDATE applications SET status='Processing' WHERE application_id=?", (result["application_id"],))
        conn.execute("UPDATE verification_checks SET status='Running',completed_at=NULL WHERE run_id=? AND stage_name='Decision and follow-up' AND check_name='__stage__'", (result["run_id"],))
    assert recover_interrupted_runs(db_path, stale_after_seconds=1) == 1
    detail = get_application(result["application_id"], db_path)
    assert detail["status"] == "Pending"
    assert next(row for row in detail["checks"] if row["stage_name"] == "Decision and follow-up" and row["check_name"] == "__stage__")["status"] == "Failed"
