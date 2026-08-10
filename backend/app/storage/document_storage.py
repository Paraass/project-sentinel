"""Raw document storage.

Content-addressable storage on the local filesystem: a document's storage
key is the hex SHA-256 of its bytes, so identical content always maps to
the same key (deterministic — the requirement, not an accident), and a
write is atomic (write to a temp file, then rename into place) so a
process killed mid-write can never leave a partially-written file at the
final path for a later read to find.

This module owns filesystem I/O only. It never touches the database —
persisting a Document row (filename, storage_key, content_hash, size,
content_type) is workflow_service's job, exactly as it already owns every
other piece of durable state. Keeping that boundary is what keeps this
"Orchestration Module -> Document Storage Module" rather than two things
both claiming to persist the same fact.
"""
import hashlib
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings


@dataclass(frozen=True)
class DocumentInput:
    """Raw content to be stored, before it has a database identity."""

    filename: str
    content: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class StoredDocument:
    """What storage durably recorded, handed back so the caller can persist
    a Document row that references it.
    """

    storage_key: str
    content_hash: str
    size: int
    content_type: str


def _storage_root() -> Path:
    return Path(get_settings().document_storage_path)


def save_document(document: DocumentInput) -> StoredDocument:
    """Write content to disk and return what was actually stored.

    If content with this exact hash already exists, the existing file is
    left untouched and its metadata is returned as-is — content-addressing
    makes this safe: two calls with identical bytes always describe the
    same stored object, never two.
    """
    content_hash = hashlib.sha256(document.content).hexdigest()
    root = _storage_root()
    root.mkdir(parents=True, exist_ok=True)

    final_path = root / content_hash
    content_type = (
        document.content_type
        or mimetypes.guess_type(document.filename)[0]
        or "application/octet-stream"
    )

    if not final_path.exists():
        tmp_path = root / f".{content_hash}.{uuid.uuid4().hex}.tmp"
        tmp_path.write_bytes(document.content)
        os.replace(tmp_path, final_path)  # atomic on the same filesystem

    return StoredDocument(
        storage_key=content_hash,
        content_hash=content_hash,
        size=len(document.content),
        content_type=content_type,
    )


def read_document(storage_key: str) -> bytes:
    """Read back previously stored content by its storage key.

    Pure filesystem read — no in-memory cache, no reliance on anything a
    running process might remember, so this works identically whether it's
    called a second later or after a full restart.
    """
    path = _storage_root() / storage_key
    return path.read_bytes()
