"""Testes de integração do orquestrador PhotoMigrator com cliente Google Fotos simulado."""

from pathlib import Path
from typing import Any

from app.migrator import PhotoMigrator
from app.state import MigrationStateStore


class FakeGooglePhotosClient:
    def __init__(self, fail_names: set[str] | None = None) -> None:
        self.fail_names = fail_names or set()
        self.albums: dict[str, dict[str, Any]] = {}
        self.uploaded_files: list[str] = []
        self.batch_calls: list[dict[str, Any]] = []

    def is_authenticated(self) -> bool:
        return True

    def get_or_create_album(self, title: str) -> dict[str, Any]:
        if title not in self.albums:
            self.albums[title] = {"id": f"album-{len(self.albums) + 1}", "title": title}
        return self.albums[title]

    def upload_media_bytes(self, file_path: Path, mime_type: str) -> str:
        if file_path.name in self.fail_names:
            raise RuntimeError("Erro simulado de rede")
        self.uploaded_files.append(file_path.name)
        return f"token-{file_path.name}"

    def batch_create_media_items(
        self, album_id: str, uploaded_items: list[dict[str, str]]
    ) -> list[dict[str, Any]]:
        self.batch_calls.append({"album_id": album_id, "items": uploaded_items})
        return [
            {
                "uploadToken": item["upload_token"],
                "status": {"code": 0, "message": "Success"},
                "mediaItem": {"id": f"media-{item['file_name']}"},
            }
            for item in uploaded_items
        ]


def test_full_migration_and_resume_without_duplicates(tmp_path: Path) -> None:
    src = tmp_path / "photos"
    src.mkdir()
    (src / "foto1.jpg").write_bytes(b"img-1")
    (src / "foto2.jpg").write_bytes(b"img-2")
    (src / "foto3.png").write_bytes(b"img-3")

    store = MigrationStateStore(tmp_path / "state.db")
    fake_client = FakeGooglePhotosClient(fail_names={"foto2.jpg"})

    migrator = PhotoMigrator(
        store,
        fake_client,  # type: ignore[arg-type]
        extract_dir=tmp_path / "ext",
        workers=2,
        batch_size=2,
    )

    # 1ª execução: foto2 falha
    migrator.start(str(src), "Porta Retrato")
    migrator.wait(timeout=10)

    status = migrator.get_status("Porta Retrato")
    assert status["state"] == "completed"
    assert status["album_id"] == "album-1"
    assert status["summary"]["uploaded"] == 2
    assert status["summary"]["failed"] == 1

    # 2ª execução com retry_failed=True: apenas foto2 é enviada, foto1 e foto3 não duplicam
    fake_client.fail_names.clear()
    fake_client.uploaded_files.clear()

    migrator.start(str(src), "Porta Retrato", retry_failed=True)
    migrator.wait(timeout=10)

    status2 = migrator.get_status("Porta Retrato")
    assert status2["state"] == "completed"
    assert status2["summary"]["uploaded"] == 3
    assert status2["summary"]["failed"] == 0
    assert fake_client.uploaded_files == ["foto2.jpg"]
