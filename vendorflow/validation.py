"""Country-aware form checks, document comparisons, risk and decision policy."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.IGNORECASE)

REQUIRED_COMMON = {
    "registered_name": "Registered company name",
    "business_type": "Business type",
    "country": "Country",
    "registered_address": "Registered address",
    "state": "State / region",
    "city": "City",
    "postal_code": "Postal code",
    "contact_name": "Contact person name",
    "designation": "Designation",
    "email": "Email address",
    "phone": "Phone number",
    "account_holder_name": "Bank account holder name",
    "bank_name": "Bank name",
}


def country_policy(country: str) -> dict[str, Any]:
    """Country-specific rules are isolated here for future jurisdictions."""
    if (country or "").strip().casefold() == "india":
        return {"required_fields": ("gstin", "pan", "ifsc"), "required_documents": ("gst", "pan", "bank"), "tax_label": "GSTIN and PAN"}
    return {"required_fields": (), "required_documents": ("registration", "bank"), "tax_label": "local tax identifiers (optional)"}


def normalized_identifier(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalized_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9 ]", " ", value.casefold())
    words = [w for w in value.split() if w not in {"private", "limited", "pvt", "ltd", "incorporated", "inc", "llc", "company", "co"}]
    return " ".join(words)


def name_similarity(left: str, right: str) -> float:
    a, b = normalized_name(left), normalized_name(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def validate_form(form: dict[str, Any], docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, message: str, category: str = "form") -> None:
        checks.append({"name": name, "status": status, "message": message, "category": category})

    for key, label in REQUIRED_COMMON.items():
        value = str(form.get(key) or "").strip()
        add(f"Required field — {label}", "Pass" if value else "Fail", f"{label} is provided." if value else f"{label} is missing.")
    account_ok = bool(form.get("account_number_present"))
    add("Required field — Bank account number", "Pass" if account_ok else "Fail", "Bank account number is recorded securely as a masked value and fingerprint." if account_ok else "Bank account number is missing.")

    email = str(form.get("email") or "").strip()
    add("Email syntax", "Pass" if EMAIL_RE.fullmatch(email) else "Fail", "Email has a plausible format." if EMAIL_RE.fullmatch(email) else "Enter an email address in name@example.com format.")
    phone = re.sub(r"[\s().+-]", "", str(form.get("phone") or ""))
    phone_ok = 7 <= len(phone) <= 15 and phone.isdigit()
    add("Phone syntax", "Pass" if phone_ok else "Fail", "Phone number has 7–15 digits." if phone_ok else "Enter a phone number with 7–15 digits.")

    policy = country_policy(str(form.get("country") or ""))
    country = str(form.get("country") or "").strip().casefold()
    if country == "india":
        gstin = normalized_identifier(str(form.get("gstin") or ""))
        pan = normalized_identifier(str(form.get("pan") or ""))
        ifsc = normalized_identifier(str(form.get("ifsc") or ""))
        for label, value, pattern in (("GSTIN format", gstin, GSTIN_RE), ("PAN format", pan, PAN_RE), ("IFSC format", ifsc, IFSC_RE)):
            good = bool(pattern.fullmatch(value))
            add(label, "Pass" if good else "Fail", f"{label.removesuffix(' format')} matches its expected structure." if good else f"{label.removesuffix(' format')} does not match the expected Indian format.", "format")
    else:
        add("Country tax validation", "Pass", f"Indian GSTIN/PAN rules were not applied to {form.get('country') or 'this country'}.", "policy")

    doc_types = {d.get("document_type") for d in docs}
    for doc_type in policy["required_documents"]:
        present = doc_type in doc_types
        label = {"gst": "GST registration certificate", "pan": "PAN document", "bank": "Supporting bank document", "registration": "Business registration certificate"}[doc_type]
        add(f"Required document — {label}", "Pass" if present else "Fail", f"{label} is attached." if present else f"{label} is missing.", "document")
    return checks


def compare_documents(form: dict[str, Any], extracted_by_type: dict[str, dict[str, str]], unreadable_types: set[str]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []

    def compare_text(label: str, submitted: str, found: str | None, kind: str = "name") -> None:
        if not found:
            comparisons.append({"name": label, "status": "Warning", "message": f"No matching value was extracted from the supporting document; manual confirmation is needed.", "submitted": submitted, "extracted": None, "category": "comparison"})
            return
        if kind == "identifier":
            equal = normalized_identifier(submitted) == normalized_identifier(found)
            comparisons.append({"name": label, "status": "Pass" if equal else "Warning", "message": "Submitted and document values match." if equal else "Submitted and document values differ; review the source document.", "submitted": submitted, "extracted": found, "category": "comparison"})
            return
        similarity = name_similarity(submitted, found)
        status = "Pass" if similarity >= 0.84 else "Warning"
        explanation = "Names match after case, punctuation and common company suffix normalization." if status == "Pass" else f"Names differ (normalized similarity {similarity:.0%}); confirm the legal relationship before approval."
        comparisons.append({"name": label, "status": status, "message": explanation, "submitted": submitted, "extracted": found, "similarity": round(similarity, 3), "category": "comparison"})

    name_field = next((d.get("registered_company_name") for d in (extracted_by_type.get("gst", {}), extracted_by_type.get("registration", {}), extracted_by_type.get("pan", {})) if d.get("registered_company_name")), None)
    compare_text("Company name — form vs certificate", str(form.get("registered_name") or ""), name_field)
    if str(form.get("country") or "").casefold() == "india":
        compare_text("GSTIN — form vs certificate", str(form.get("gstin") or ""), extracted_by_type.get("gst", {}).get("gstin"), "identifier")
        compare_text("PAN — form vs document", str(form.get("pan") or ""), extracted_by_type.get("pan", {}).get("pan"), "identifier")
        compare_text("IFSC — form vs bank document", str(form.get("ifsc") or ""), extracted_by_type.get("bank", {}).get("ifsc"), "identifier")
    bank_name = extracted_by_type.get("bank", {}).get("bank_name")
    if bank_name:
        compare_text("Bank name — form vs supporting document", str(form.get("bank_name") or ""), bank_name)
    bank_holder = extracted_by_type.get("bank", {}).get("account_holder_name")
    compare_text("Account holder — form vs bank document", str(form.get("account_holder_name") or ""), bank_holder)

    holder = str(form.get("account_holder_name") or "")
    company = str(form.get("registered_name") or "")
    similarity = name_similarity(holder, company)
    status = "Pass" if similarity >= 0.84 else "Warning"
    msg = "Account holder name aligns with the registered company name after normalization." if status == "Pass" else f"Account holder differs from the registered company name (normalized similarity {similarity:.0%}); procurement review is required. No bank ownership verification was performed."
    comparisons.append({"name": "Bank account holder vs company", "status": status, "message": msg, "submitted": company, "extracted": holder, "similarity": round(similarity, 3), "category": "comparison"})

    for doc_type in sorted(unreadable_types):
        comparisons.append({"name": f"Document readability — {doc_type.upper()}", "status": "Warning", "message": "Document text could not be read reliably; request a text-based PDF or manual review.", "submitted": None, "extracted": None, "category": "readability"})
    return comparisons


def determine_risk_and_decision(checks: list[dict[str, Any]], comparisons: list[dict[str, Any]], duplicates: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[str] = []
    failures = [c for c in checks if c["status"] == "Fail"]
    warnings = [c for c in checks + comparisons if c["status"] == "Warning"]
    for item in failures + warnings:
        if item.get("message") and item["message"] not in issues:
            issues.append(item["message"])
    for duplicate in duplicates:
        issues.append(duplicate["message"])

    high_conflict = any("Bank account holder vs company" == c["name"] and c.get("similarity", 1) < 0.55 for c in comparisons)
    if duplicates or high_conflict:
        risk = "High"
    elif failures or warnings:
        risk = "Medium"
    else:
        risk = "Low"

    # Auto rejection is intentionally not inferred from format or possible duplicate matches.
    # The only rejection in this demo is a reasoned human reviewer action.
    approved = not failures and not warnings and not duplicates
    status = "Approved" if approved else "Pending"
    reason = (
        "All required form, document, format and cross-document checks passed under the configured demo policy."
        if approved else
        "One or more required checks need correction or human review. No live registry, tax, identity, or bank verification was performed."
    )
    if duplicates:
        reason = "A potentially duplicate identifier or bank account is already associated with another application; automatic onboarding is paused for human review."
    elif failures:
        reason = "Required information, format checks, or documents need correction before onboarding can proceed."
    elif warnings:
        reason = "Supporting values could not be confirmed from documents or names differ; procurement review is required."
    action = "No further action is required under the demo policy." if approved else "Review the listed issues, correct the submission or request clarification, then re-run verification."
    passed = sum(c["status"] == "Pass" for c in checks + comparisons) + (1 if not duplicates else 0)
    total = len(checks) + len(comparisons) + max(1, len(duplicates))
    return {"status": status, "risk_category": risk, "reason": reason, "recommended_action": action, "issues": issues, "checks_passed": passed, "checks_total": total, "checks": checks, "comparisons": comparisons, "duplicates": duplicates}
