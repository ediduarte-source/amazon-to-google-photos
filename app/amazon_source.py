"""Descoberta e preparação de fotos/vídeos exportados do Amazon Photos (pastas e arquivos ZIP)."""

from __future__ import annotations

import hashlib
import mimetypes
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import (
    DEFAULT_EXTRACT_DIR,
    MIME_TYPE_OVERRIDES,
    SUPPORTED_MEDIA_EXTENSIONS,
)


@dataclass
class ScanReport:
    source_path: str
    total_files_found: int
    total_bytes: int
    extracted_zips: int
    skipped_unsupported: int
    items: list[dict]


def detect_mime_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_TYPE_OVERRIDES:
        return MIME_TYPE_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Calcula SHA-256 em streaming para evitar alto consumo de RAM em vídeos grandes."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract_zip(zip_path: Path, destination_root: Path) -> Path:
    """Extrai um arquivo ZIP do Amazon Photos com proteção contra path traversal (Zip Slip)."""
    target_dir = destination_root / zip_path.stem
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_target = target_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            member_dest = (target_dir / member.filename).resolve()
            if not str(member_dest).startswith(str(resolved_target)):
                raise ValueError(f"Entrada insegura detectada no ZIP: {member.filename}")
            member_dest.parent.mkdir(parents=True, exist_ok=True)
            if not member_dest.exists() or member_dest.stat().st_size != member.file_size:
                with zf.open(member, "r") as src, member_dest.open("wb") as dst:
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        dst.write(buf)

    return target_dir


def _is_ignored_path(path: Path) -> bool:
    parts = set(path.parts)
    if "__MACOSX" in parts:
        return True
    if path.name.startswith("._") or path.name.startswith("."):
        return True
    return False


def scan_amazon_export(
    source: str | Path,
    *,
    extract_dir: Path = DEFAULT_EXTRACT_DIR,
    on_progress: Callable[[str], None] | None = None,
) -> ScanReport:
    """
    Escaneia uma pasta local ou arquivo .zip baixado do Amazon Photos.

    - Se `source` for um `.zip`, extrai com segurança para `extract_dir` e escaneia o conteúdo.
    - Se `source` for uma pasta contendo arquivos `.zip` do Amazon Photos, extrai cada `.zip`
      automaticamente além de incluir as mídias soltas na pasta.
    """
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Caminho não encontrado: {source_path}")

    roots_to_scan: list[Path] = []
    extracted_zips = 0

    if source_path.is_file():
        if source_path.suffix.lower() != ".zip":
            raise ValueError("O arquivo informado precisa ser um .zip ou uma pasta contendo fotos.")
        if on_progress:
            on_progress(f"Extraindo arquivo ZIP: {source_path.name}...")
        extracted = safe_extract_zip(source_path, extract_dir)
        roots_to_scan.append(extracted)
        extracted_zips += 1
    else:
        roots_to_scan.append(source_path)
        for zip_candidate in sorted(source_path.rglob("*.zip")):
            if _is_ignored_path(zip_candidate):
                continue
            if on_progress:
                on_progress(f"Extraindo ZIP encontrado: {zip_candidate.name}...")
            extracted = safe_extract_zip(zip_candidate, extract_dir)
            roots_to_scan.append(extracted)
            extracted_zips += 1

    items: list[dict] = []
    seen_paths: set[Path] = set()
    skipped_unsupported = 0
    total_bytes = 0

    for root in roots_to_scan:
        for candidate in sorted(root.rglob("*")):
            if not candidate.is_file() or _is_ignored_path(candidate):
                continue
            resolved = candidate.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            ext = candidate.suffix.lower()
            if ext == ".zip":
                continue
            if ext not in SUPPORTED_MEDIA_EXTENSIONS:
                skipped_unsupported += 1
                continue

            size = candidate.stat().st_size
            if size == 0:
                skipped_unsupported += 1
                continue

            file_hash = compute_sha256(candidate)
            items.append(
                {
                    "file_hash": file_hash,
                    "file_path": str(resolved),
                    "file_name": candidate.name,
                    "file_size": size,
                    "mime_type": detect_mime_type(candidate),
                }
            )
            total_bytes += size
            if on_progress and len(items) % 25 == 0:
                on_progress(f"{len(items)} mídias indexadas...")

    return ScanReport(
        source_path=str(source_path),
        total_files_found=len(items),
        total_bytes=total_bytes,
        extracted_zips=extracted_zips,
        skipped_unsupported=skipped_unsupported,
        items=items,
    )
