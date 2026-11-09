"""PDF upload validation and conservative text extraction."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .storage import get_setting


MAX_UPLOAD_BYTES = int(get_setting("VENDORFLOW_MAX_UPLOAD_MB", "10")) * 1024 * 1024
ALLOWED_MIME_TYPES = {"application/pdf", "application/x-pdf", ""}


class DocumentError(ValueError):
    pass


def validate_pdf_upload(name: str, data: bytes, mime_type: str = "application/pdf") -> None:
    safe_name = Path(name or "upload.pdf").name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise DocumentError(f"{safe_name}: only PDF files are accepted.")
    if mime_type not in ALLOWED_MIME_TYPES:
        raise DocumentError(f"{safe_name}: unsupported content type {mime_type!r}; upload a PDF.")
    if not data:
        raise DocumentError(f"{safe_name}: the uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError(f"{safe_name}: file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")
    if not data.lstrip().startswith(b"%PDF-"):
        raise DocumentError(f"{safe_name}: the file does not have a valid PDF signature.")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_pdf_text(path: str | Path | bytes) -> tuple[str, str | None]:
    """Return extracted text and an optional processing limitation."""
    try:
        import pymupdf as fitz
    except ImportError:
        return "", "PyMuPDF is not installed; install the project dependencies to process PDFs."
    try:
        document = fitz.open(stream=path, filetype="pdf") if isinstance(path, bytes) else fitz.open(str(path))
        with document as pdf:
            text = "\n".join(page.get_text("text") for page in pdf).strip()
    except Exception as exc:
        return "", f"PDF could not be opened ({type(exc).__name__})."
    if len(re.sub(r"\s+", "", text)) < 12:
        return text, "No readable text was found. Scanned PDFs need OCR or manual review."
    return text, None


def deterministic_extract(text: str) -> dict[str, str]:
    """Extract only literal, recognizable values present in source text."""
    results: dict[str, str] = {}
    flat = re.sub(r"\s+", " ", text)
    gstin = re.search(r"(?<![A-Z0-9])[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9](?![A-Z0-9])", flat.upper())
    pan = re.search(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])", flat.upper())
    if gstin:
        results["gstin"] = gstin.group(0)
    if pan:
        results["pan"] = pan.group(0)

    labels = {
        "registered_company_name": r"(?:registered\s+company\s+name|legal\s+name|company\s+name|name\s+of\s+business)\s*[:\-]\s*([^\r\n|;]{2,120})",
        "account_holder_name": r"(?:account\s+holder(?:\s+name)?|account\s+name|beneficiary\s+name)\s*[:\-]\s*([^\r\n|;]{2,120})",
        "bank_name": r"(?:bank\s+name)\s*[:\-]\s*([^\r\n|;]{2,100})",
        "ifsc": r"(?:ifsc(?:\s+code)?)\s*[:\-]\s*([A-Z]{4}0[A-Z0-9]{6})",
    }
    for field, pattern in labels.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            results[field] = match.group(1).strip(" .,:-\t") if field not in {"ifsc"} else match.group(1).upper()
    return {key: value for key, value in results.items() if value}


def extract_document(path: str | Path | bytes, ai_enabled: bool = False) -> dict[str, Any]:
    text, limitation = extract_pdf_text(path)
    fields = deterministic_extract(text) if text else {}
    methods = {field: "local_pdf_text" for field in fields}
    ai_state = "not configured"
    if ai_enabled and text:
        try:
            from .ai import extract_with_ai

            proposed, ai_state = extract_with_ai(text)
            normalized_source = re.sub(r"\s+", " ", text).casefold()
            for key, value in proposed.items():
                value = str(value).strip()
                # AI proposals are accepted only when the exact value is in the source.
                if key not in fields and value and re.sub(r"\s+", " ", value).casefold() in normalized_source:
                    fields[key] = value
                    methods[key] = "ai_source_matched"
        except Exception:
            ai_state = "unavailable"
    return {"text": text, "fields": fields, "methods": methods, "limitation": limitation, "ai_state": ai_state}
