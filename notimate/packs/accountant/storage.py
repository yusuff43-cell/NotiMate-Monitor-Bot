"""Originals on disk (docs/21 Этап 7: «том с бэкапом; S3 — когда понадобится»).

Content-addressed per tenant: ``<root>/<tenant>/<sha256>.<ext>``. Addressing by hash makes
re-uploads idempotent, keeps the path free of user input (no traversal) and lets the
package builder name files by document number at export time without renaming anything.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

EXTENSIONS = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'application/pdf': 'pdf'}


def storage_root() -> Path:
    return Path(os.environ.get('DOCUMENT_STORAGE_DIR', '/data/documents'))


def _safe_tenant(tenant_id: str) -> str:
    return re.sub(r'[^A-Za-z0-9_-]', '_', tenant_id) or '_'


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_original(tenant_id: str, data: bytes, mime: str, root: Path | None = None) -> tuple[str, str]:
    """Write the file if new; returns ``(relative_ref, sha256)``."""
    sha = sha256_of(data)
    ext = EXTENSIONS.get(mime, 'bin')
    ref = f'{_safe_tenant(tenant_id)}/{sha}.{ext}'
    path = (root or storage_root()) / ref
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return ref, sha


def read_original(ref: str, root: Path | None = None) -> bytes:
    base = (root or storage_root()).resolve()
    path = (base / ref).resolve()
    if base not in path.parents:
        raise ValueError('storage ref escapes the storage root')
    return path.read_bytes()


def extension_of(ref: str) -> str:
    return ref.rsplit('.', 1)[-1] if '.' in ref else 'bin'
