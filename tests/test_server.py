"""Testes dos endpoints da API FastAPI."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.server import create_app


def test_server_endpoints_and_scan(tmp_path: Path) -> None:
    src = tmp_path / "export"
    src.mkdir()
    (src / "retrato1.jpg").write_bytes(b"photo-bytes-1")
    (src / "retrato2.jpg").write_bytes(b"photo-bytes-2")

    app = create_app(
        db_path=tmp_path / "state.db",
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json",
        extract_dir=tmp_path / "extracted",
    )
    client = TestClient(app)

    # Página inicial
    resp_home = client.get("/")
    assert resp_home.status_code == 200
    assert "Porta Retrato" in resp_home.text

    # Status de auth inicial
    auth = client.get("/api/auth/status").json()
    assert auth["has_credentials"] is False
    assert auth["authenticated"] is False

    # Prévia (scan) da pasta
    scan = client.post(
        "/api/scan",
        json={"source_path": str(src), "album_name": "Porta Retrato"},
    )
    assert scan.status_code == 200
    data = scan.json()
    assert data["total_files_found"] == 2
    assert data["newly_queued"] == 2

    # Bloqueia migração real sem OAuth conectado
    start_err = client.post(
        "/api/migrate/start",
        json={"source_path": str(src), "album_name": "Porta Retrato", "dry_run": False},
    )
    assert start_err.status_code == 400


def test_upload_source_endpoint(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "state.db",
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json",
        extract_dir=tmp_path / "extracted",
        uploads_dir=tmp_path / "uploads",
    )
    client = TestClient(app)

    resp = client.post(
        "/api/upload-source?album_name=Porta%20Retrato",
        files=[
            ("files", ("foto_praia.jpg", b"\xff\xd8\xff\xe0beach-bytes", "image/jpeg")),
            ("files", ("foto_familia.png", b"\x89PNGfamily-bytes", "image/png")),
        ],
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["uploaded_files"] == 2
    assert payload["total_files_found"] == 2
    assert payload["newly_queued"] == 2

