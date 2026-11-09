"""Storage configuration and private Supabase Storage helpers.

The service-role key is used only by this server-side module. Uploaded PDFs live
in a private bucket and are never exposed through public object URLs.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


class StorageError(RuntimeError):
    """A storage configuration or operation failed without exposing secrets."""


def get_setting(name: str, default: Any = None) -> Any:
    """Read local environment values first, then Streamlit's server-side secrets."""
    value = os.getenv(name)
    if value is not None:
        return value
    try:
        import streamlit as st

        value = st.secrets.get(name)
        if value is not None:
            return value
    except Exception:
        # Streamlit secrets are optional in local CLI/tests and may be absent.
        pass
    return default


def storage_backend(db_path: str | Path | None = None, default_db_path: str | Path | None = None) -> str:
    """Return the configured backend; custom database paths remain SQLite for tests/tools."""
    if db_path is not None and default_db_path is not None:
        try:
            if Path(db_path).resolve() != Path(default_db_path).resolve():
                return "local"
        except (OSError, TypeError, ValueError):
            return "local"
    backend = str(get_setting("VENDORFLOW_STORAGE_BACKEND", "local")).strip().lower()
    if backend not in {"local", "supabase"}:
        raise StorageError("VENDORFLOW_STORAGE_BACKEND must be either 'local' or 'supabase'.")
    return backend


def uses_supabase_database(db_path: str | Path | None, default_db_path: str | Path) -> bool:
    return storage_backend(db_path, default_db_path) == "supabase"


def supabase_config() -> dict[str, str]:
    required = {
        "SUPABASE_URL": get_setting("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": get_setting("SUPABASE_SERVICE_ROLE_KEY"),
        "SUPABASE_DB_URL": get_setting("SUPABASE_DB_URL"),
    }
    missing = [key for key, value in required.items() if not str(value or "").strip()]
    if missing:
        raise StorageError(
            "Supabase storage is selected, but required Streamlit secrets are missing: "
            + ", ".join(missing)
            + ". Add them in Streamlit Community Cloud → App settings → Secrets."
        )
    url = str(required["SUPABASE_URL"]).rstrip("/")
    db_url = str(required["SUPABASE_DB_URL"]).strip()
    if not url.startswith("https://") or not db_url.startswith(("postgres://", "postgresql://")):
        raise StorageError("SUPABASE_URL or SUPABASE_DB_URL has an invalid format.")
    return {
        "url": url,
        "service_role_key": str(required["SUPABASE_SERVICE_ROLE_KEY"]).strip(),
        "db_url": db_url,
        "bucket": str(get_setting("SUPABASE_STORAGE_BUCKET", "vendor-documents")).strip() or "vendor-documents",
    }


def _request_storage(config: dict[str, str], method: str, path: str, body: bytes | None = None, content_type: str = "application/json") -> bytes:
    request = Request(
        f"{config['url']}/storage/v1/{path.lstrip('/')}",
        data=body,
        method=method,
        headers={
            "apikey": config["service_role_key"],
            "Authorization": f"Bearer {config['service_role_key']}",
            "Content-Type": content_type,
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.read()
    except HTTPError as exc:
        # Keep response bodies and request headers (which can contain credentials) out of logs/UI.
        if exc.code == 409:
            raise StorageError("Supabase Storage reported a conflicting bucket or object.") from None
        raise StorageError(f"Supabase Storage returned HTTP {exc.code} for a {method} request.") from None
    except (URLError, TimeoutError, OSError):
        raise StorageError("Could not reach Supabase Storage. Check the project URL and network status.") from None


@lru_cache(maxsize=4)
def ensure_private_bucket(url: str, service_role_key: str, bucket: str, max_file_bytes: int) -> None:
    """Create a private PDF bucket when absent and reject an unsafe existing bucket."""
    config = {"url": url, "service_role_key": service_role_key, "bucket": bucket}
    listing = _request_storage(config, "GET", "bucket")
    try:
        buckets = json.loads(listing or b"[]")
    except (TypeError, json.JSONDecodeError):
        raise StorageError("Supabase Storage returned an unreadable bucket listing.") from None
    current = next((item for item in buckets if item.get("id") == bucket or item.get("name") == bucket), None)
    if current is None:
        payload = json.dumps({
            "id": bucket,
            "name": bucket,
            "public": False,
            "file_size_limit": max_file_bytes,
            "allowed_mime_types": ["application/pdf"],
        }).encode("utf-8")
        try:
            _request_storage(config, "POST", "bucket", payload)
        except StorageError as exc:
            # A concurrent app startup may have created it after our listing.
            if "conflicting" not in str(exc).lower():
                raise
            refreshed = json.loads(_request_storage(config, "GET", "bucket") or b"[]")
            current = next((item for item in refreshed if item.get("id") == bucket or item.get("name") == bucket), None)
            if current is None:
                raise
        else:
            return
    if current.get("public"):
        raise StorageError(f"Supabase bucket '{bucket}' is public. Vendor documents require a private bucket.")
    size_limit = current.get("file_size_limit")
    if size_limit is not None and int(size_limit) < max_file_bytes:
        raise StorageError(f"Supabase bucket '{bucket}' has a file-size limit below this app's upload limit.")
    allowed_types = current.get("allowed_mime_types")
    if allowed_types and "application/pdf" not in allowed_types:
        raise StorageError(f"Supabase bucket '{bucket}' does not allow PDF files.")


def initialize_supabase_storage(max_file_bytes: int) -> None:
    config = supabase_config()
    ensure_private_bucket(config["url"], config["service_role_key"], config["bucket"], int(max_file_bytes))


def document_key(application_id: str, revision_no: int, document_id: str) -> str:
    return f"{application_id}/revision_{revision_no}/{document_id}.pdf"


def supabase_document_uri(bucket: str, key: str) -> str:
    return f"supabase://{bucket}/{key}"


def upload_document(bucket: str, key: str, content: bytes, mime_type: str = "application/pdf") -> str:
    config = supabase_config()
    max_file_bytes = int(get_setting("VENDORFLOW_MAX_UPLOAD_MB", "10")) * 1024 * 1024
    ensure_private_bucket(config["url"], config["service_role_key"], bucket, max_file_bytes)
    encoded_bucket = quote(bucket, safe="")
    encoded_key = quote(key, safe="/")
    request = Request(
        f"{config['url']}/storage/v1/object/{encoded_bucket}/{encoded_key}",
        data=content,
        method="POST",
        headers={
            "apikey": config["service_role_key"],
            "Authorization": f"Bearer {config['service_role_key']}",
            "Content-Type": mime_type,
            "x-upsert": "true",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            response.read()
    except HTTPError as exc:
        raise StorageError(f"Supabase Storage could not save the PDF (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise StorageError("Could not upload the PDF to Supabase Storage. Check connectivity and retry.") from None
    return supabase_document_uri(bucket, key)


def read_document(stored_path: str | Path) -> bytes | str:
    """Return remote PDF bytes, or a local path for PyMuPDF to open."""
    value = str(stored_path)
    if not value.startswith("supabase://"):
        return value
    parsed = urlsplit(value)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    if not bucket or not key:
        raise StorageError("The stored Supabase document reference is invalid.")
    config = supabase_config()
    encoded_bucket = quote(bucket, safe="")
    encoded_key = quote(key, safe="/")
    data = _request_storage(config, "GET", f"object/{encoded_bucket}/{encoded_key}", content_type="application/pdf")
    if not data:
        raise StorageError("Supabase returned an empty document.")
    return data
