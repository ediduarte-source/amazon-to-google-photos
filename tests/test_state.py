"""Testes para o armazenamento SQLite e deduplicação por SHA-256."""

from pathlib import Path

from app.state import MigrationStateStore


def test_upsert_deduplicates_by_sha256_and_tracks_status(tmp_path: Path) -> None:
    store = MigrationStateStore(tmp_path / "state.db")

    items = [
        {
            "file_hash": "hash1",
            "file_path": "/tmp/a.jpg",
            "file_name": "a.jpg",
            "file_size": 1000,
            "mime_type": "image/jpeg",
        },
        {
            "file_hash": "hash2",
            "file_path": "/tmp/b.png",
            "file_name": "b.png",
            "file_size": 2500,
            "mime_type": "image/png",
        },
    ]

    first = store.upsert_discovered_items(items, "Porta Retrato")
    assert first == {"added": 2, "duplicates": 0}

    second = store.upsert_discovered_items(items, "Porta Retrato")
    assert second == {"added": 0, "duplicates": 2}

    pending = store.get_items_by_status("Porta Retrato", ("pending",))
    assert len(pending) == 2

    store.mark_status(pending[0].id, "uploaded", google_media_id="gmedia-1")
    store.mark_status(pending[1].id, "failed", error_message="HTTP 503")

    summary = store.get_summary("Porta Retrato")
    assert summary["total"] == 2
    assert summary["uploaded"] == 1
    assert summary["failed"] == 1
    assert summary["uploaded_bytes"] == 1000

    reset = store.reset_failed_to_pending("Porta Retrato")
    assert reset == 1
    assert len(store.get_items_by_status("Porta Retrato", ("pending",))) == 1


def test_album_id_cache(tmp_path: Path) -> None:
    store = MigrationStateStore(tmp_path / "state.db")
    assert store.get_album_id("Porta Retrato") is None
    store.save_album_id("Porta Retrato", "album-xyz")
    assert store.get_album_id("Porta Retrato") == "album-xyz"
