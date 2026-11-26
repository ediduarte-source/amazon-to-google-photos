"""Testes para escaneamento de pastas e extração segura de arquivos ZIP do Amazon Photos."""

import zipfile
from pathlib import Path

import pytest

from app.amazon_source import safe_extract_zip, scan_amazon_export


def test_scan_folder_and_nested_zip(tmp_path: Path) -> None:
    src_dir = tmp_path / "amazon_export"
    src_dir.mkdir()

    # Foto solta
    (src_dir / "ferias.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-1")
    # Arquivo não suportado (deve ser ignorado)
    (src_dir / "notas.txt").write_text("ignorar", encoding="utf-8")

    # ZIP baixado do Amazon Photos contendo outra foto e vídeo
    zip_file = src_dir / "lote1.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        zf.writestr("familia/aniversario.png", b"\x89PNGfake-png-data")
        zf.writestr("familia/video.mp4", b"fake-mp4-bytes")
        zf.writestr("__MACOSX/._aniversario.png", b"mac-junk")

    report = scan_amazon_export(src_dir, extract_dir=tmp_path / "extracted")
    assert report.extracted_zips == 1
    assert report.total_files_found == 3
    assert report.skipped_unsupported == 1
    names = {item["file_name"] for item in report.items}
    assert names == {"ferias.jpg", "aniversario.png", "video.mp4"}


def test_safe_extract_zip_blocks_zip_slip(tmp_path: Path) -> None:
    malicious_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../../escape.jpg", b"evil")

    with pytest.raises(ValueError, match="Entrada insegura"):
        safe_extract_zip(malicious_zip, tmp_path / "out")
