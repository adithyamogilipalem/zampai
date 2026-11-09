"""Fictional demonstration records and reproducible workflow scenarios."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH, initialize_database, query_one
from .workflow import process_submission


SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_documents"


def make_pdf(text: str) -> bytes:
    try:
        import pymupdf as fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to create the demonstration PDFs. Install requirements.txt.") from exc
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox((54, 64, 540, 740), text, fontsize=12, lineheight=1.6)
    data = doc.tobytes()
    doc.close()
    return data


def demo_documents(
    company: str = "Aarav Precision Components Private Limited",
    gstin: str = "27AARCA1234F1Z5",
    pan: str = "AARCA1234F",
    bank_holder: str | None = None,
    ifsc: str = "SBIN0001234",
    account: str = "987654321012",
) -> dict[str, tuple[str, bytes, str]]:
    holder = bank_holder or company
    return {
        "gst": ("gst_registration_certificate.pdf", make_pdf(f"GST REGISTRATION CERTIFICATE\nRegistered Company Name: {company}\nGSTIN: {gstin}"), "application/pdf"),
        "pan": ("pan_document.pdf", make_pdf(f"PERMANENT ACCOUNT NUMBER\nRegistered Company Name: {company}\nPAN: {pan}"), "application/pdf"),
        "bank": ("supporting_bank_document.pdf", make_pdf(f"BANK ACCOUNT CONFIRMATION\nAccount Holder Name: {holder}\nBank Name: State Bank of India\nAccount Number: {account}\nIFSC: {ifsc}"), "application/pdf"),
    }


def demo_form(**overrides: str) -> dict[str, str]:
    form = {
        "registered_name": "Aarav Precision Components Private Limited",
        "trading_name": "Aarav Precision",
        "business_type": "Private Limited Company",
        "country": "India",
        "registered_address": "42 Industrial Estate, Phase II",
        "state": "Maharashtra",
        "city": "Pune",
        "postal_code": "411019",
        "contact_name": "Mira Deshpande",
        "designation": "Procurement Operations Lead",
        "email": "mira@aaravprecision.example",
        "phone": "+91 98765 43210",
        "gstin": "27AARCA1234F1Z5",
        "pan": "AARCA1234F",
        "account_holder_name": "Aarav Precision Components Private Limited",
        "bank_name": "State Bank of India",
        "account_number": "987654321012",
        "ifsc": "SBIN0001234",
    }
    form.update(overrides)
    return form


def write_sample_documents() -> list[str]:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    docs = demo_documents()
    paths = []
    for doc_type, (name, content, _) in docs.items():
        path = SAMPLE_DIR / name
        if not path.exists():
            path.write_bytes(content)
        paths.append(str(path))
    return paths


def seed_demo_data(db_path: str | Path | None = None, upload_root: str | Path = "data/uploads") -> str | None:
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    initialize_database(db_path)
    exists = query_one("SELECT application_id FROM applications LIMIT 1", (), db_path)
    if exists:
        return None
    result = process_submission(demo_form(), demo_documents(), db_path, upload_root, submitted_by="demo seed", change_summary="Seeded fictional approved vendor")
    write_sample_documents()
    return result["application_id"]


def run_demo_scenario(name: str, db_path: str | Path | None = None, upload_root: str | Path = "data/uploads", on_progress=None) -> dict[str, Any]:
    """Run real onboarding logic for one of four interview scenarios."""
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    seed_demo_data(db_path, upload_root)
    if name == "complete":
        form = demo_form(email="nina@bluepeak.example", registered_name="BluePeak Industrial Systems Private Limited", trading_name="BluePeak Systems", contact_name="Nina Rao", gstin="27BLPCA5678H1Z2", pan="BLPCA5678H", account_holder_name="BluePeak Industrial Systems Private Limited", account_number="987654320011")
        uploads = demo_documents(company=form["registered_name"], gstin=form["gstin"], pan=form["pan"], bank_holder=form["account_holder_name"], account="987654320011")
        label = "Complete, consistent submission"
    elif name == "missing_certificate":
        form = demo_form(email="rohan@orbit.example", registered_name="Orbit Components Private Limited", trading_name="Orbit Components", contact_name="Rohan Shah", gstin="27ORCCA6789J1Z3", pan="ORCCA6789J", account_holder_name="Orbit Components Private Limited", account_number="987650001122")
        uploads = demo_documents(company=form["registered_name"], gstin=form["gstin"], pan=form["pan"], bank_holder=form["account_holder_name"], account="987650001122")
        uploads.pop("gst")
        label = "Missing GST certificate"
    elif name == "bank_mismatch":
        form = demo_form(email="tara@northstar.example", registered_name="Northstar Precision Manufacturing Private Limited", trading_name="Northstar Precision", contact_name="Tara Menon", gstin="27NSPCA9876K1Z7", pan="NSPCA9876K", account_holder_name="Bluewave Maritime Holdings Limited", account_number="987650009988")
        uploads = demo_documents(company=form["registered_name"], gstin=form["gstin"], pan=form["pan"], bank_holder=form["account_holder_name"], account="987650009988")
        label = "Significant bank-holder mismatch"
    elif name == "duplicate_tax_id":
        form = demo_form(email="dev@redwood.example", registered_name="Redwood Industrial Products Private Limited", trading_name="Redwood Products", contact_name="Dev Shah", gstin="27AARCA1234F1Z5", pan="REDCA4321D", account_holder_name="Redwood Industrial Products Private Limited", account_number="987650007766")
        uploads = demo_documents(company=form["registered_name"], gstin=form["gstin"], pan=form["pan"], bank_holder=form["account_holder_name"], account="987650007766")
        label = "Duplicate tax identifier"
    else:
        raise ValueError("Unknown demo scenario.")
    result = process_submission(form, uploads, db_path, upload_root, submitted_by="demo scenario", change_summary=label, on_progress=on_progress)
    result["scenario"] = label
    return result


def correct_missing_certificate(application_id: str, db_path: str | Path | None = None, upload_root: str | Path = "data/uploads", on_progress=None) -> dict[str, Any]:
    """Re-submit the same vendor with the missing GST PDF as a new revision."""
    form = demo_form(email="rohan@orbit.example", registered_name="Orbit Components Private Limited", trading_name="Orbit Components", contact_name="Rohan Shah", gstin="27ORCCA6789J1Z3", pan="ORCCA6789J", account_holder_name="Orbit Components Private Limited", account_number="987650001122")
    docs = demo_documents(company=form["registered_name"], gstin=form["gstin"], pan=form["pan"], bank_holder=form["account_holder_name"], account="987650001122")
    return process_submission(form, {"gst": docs["gst"]}, db_path, upload_root, application_id=application_id, submitted_by="demo vendor correction", change_summary="Added missing GST registration certificate", on_progress=on_progress)
