"""Tests for raw document storage.

Real filesystem I/O throughout — every test gets its own temp directory via
the `storage_root` fixture below, and nothing here mocks the filesystem or
the storage module's functions.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.storage.document_storage import DocumentInput, read_document, save_document


@pytest.fixture(autouse=True)
def storage_root():
    """Point DOCUMENT_STORAGE_PATH at a fresh temp directory for every test
    in this file, and clear get_settings' cache so the change actually
    takes effect (Settings is @lru_cache'd — without clearing it, the first
    test to call get_settings() would pin the path for the rest of the
    process).
    """
    tmp_dir = tempfile.mkdtemp(prefix="sentinel-storage-test-")
    previous = os.environ.get("DOCUMENT_STORAGE_PATH")
    os.environ["DOCUMENT_STORAGE_PATH"] = tmp_dir
    get_settings.cache_clear()

    yield Path(tmp_dir)

    if previous is None:
        os.environ.pop("DOCUMENT_STORAGE_PATH", None)
    else:
        os.environ["DOCUMENT_STORAGE_PATH"] = previous
    get_settings.cache_clear()
    shutil.rmtree(tmp_dir, ignore_errors=True)


# --- bytes are actually written -------------------------------------------


def test_save_document_writes_bytes_to_disk(storage_root):
    stored = save_document(DocumentInput(filename="report.txt", content=b"hello world"))

    on_disk_path = storage_root / stored.storage_key
    assert on_disk_path.exists()
    assert on_disk_path.read_bytes() == b"hello world"


# --- stored bytes can be read back -----------------------------------------


def test_read_document_returns_original_bytes(storage_root):
    content = b"the quick brown fox jumps over the lazy dog"
    stored = save_document(DocumentInput(filename="fox.txt", content=content))

    assert read_document(stored.storage_key) == content


# --- content hash is stable/correct ----------------------------------------


def test_content_hash_is_correct_sha256(storage_root):
    content = b"deterministic content"
    stored = save_document(DocumentInput(filename="a.txt", content=content))

    assert stored.content_hash == hashlib.sha256(content).hexdigest()


def test_identical_content_produces_identical_storage_key(storage_root):
    content = b"same bytes, different filenames"
    first = save_document(DocumentInput(filename="one.txt", content=content))
    second = save_document(DocumentInput(filename="two.txt", content=content))

    assert first.storage_key == second.storage_key
    assert first.content_hash == second.content_hash

    # And only one file was actually written for this content — no
    # duplicate, no leftover temp file from either write.
    matching_files = list(storage_root.iterdir())
    assert matching_files == [storage_root / first.storage_key]


# --- metadata is correct ----------------------------------------------------


def test_metadata_fields_are_correct(storage_root):
    content = b"%PDF-1.4 fake pdf content"
    stored = save_document(DocumentInput(filename="contract.pdf", content=content))

    assert stored.size == len(content)
    assert stored.content_type == "application/pdf"


def test_content_type_defaults_when_unguessable(storage_root):
    stored = save_document(DocumentInput(filename="no_extension", content=b"data"))
    assert stored.content_type == "application/octet-stream"


def test_explicit_content_type_is_respected(storage_root):
    stored = save_document(
        DocumentInput(filename="data.bin", content=b"data", content_type="application/custom")
    )
    assert stored.content_type == "application/custom"


# --- content survives a restart ---------------------------------------------


def test_content_survives_new_process(storage_root):
    """Write in this process, then read back in a completely separate
    Python process that shares no memory with this one — the only thing
    connecting them is the file actually sitting on disk. This is a
    stronger proof of restart-safety than calling save/read from the same
    pytest process, which could in principle pass on shared in-memory state
    without anyone noticing.
    """
    content = b"this must still be here after a restart"
    stored = save_document(DocumentInput(filename="durable.txt", content=content))

    backend_root = Path(__file__).resolve().parent.parent  # backend/, so `app` is importable
    script = (
        "from app.storage.document_storage import read_document\n"
        "import sys\n"
        f"sys.stdout.buffer.write(read_document({stored.storage_key!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        cwd=str(backend_root),
        env={
            **os.environ,
            "DOCUMENT_STORAGE_PATH": str(storage_root),
            "DATABASE_URL": "postgresql+asyncpg://placeholder:placeholder@localhost/placeholder",
        },
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == content
