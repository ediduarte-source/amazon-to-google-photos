"""Orquestrador da migração com upload paralelo de bytes, batchCreate serializado e retomada."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.amazon_source import ScanReport, scan_amazon_export
from app.config import (
    DEFAULT_ALBUM_NAME,
    DEFAULT_EXTRACT_DIR,
    DEFAULT_UPLOAD_WORKERS,
    MAX_BATCH_CREATE_SIZE,
)
from app.google_photos import GooglePhotosClient
from app.state import MigrationItem, MigrationStateStore


@dataclass
class MigrationRuntimeStatus:
    state: str = "idle"  # idle | scanning | running | paused | completed | error
    album_name: str = DEFAULT_ALBUM_NAME
    album_id: str | None = None
    source_path: str | None = None
    dry_run: bool = False
    message: str = "Pronto para iniciar."
    started_at: float | None = None
    finished_at: float | None = None
    logs: list[str] = field(default_factory=list)


class PhotoMigrator:
    """Coordena escaneamento, deduplicação e envio para o álbum desejado no Google Fotos."""

    def __init__(
        self,
        state_store: MigrationStateStore,
        google_client: GooglePhotosClient,
        *,
        extract_dir: Path = DEFAULT_EXTRACT_DIR,
        workers: int = DEFAULT_UPLOAD_WORKERS,
        batch_size: int = 25,
    ) -> None:
        self.state_store = state_store
        self.google_client = google_client
        self.extract_dir = extract_dir
        self.workers = max(1, min(workers, 8))
        self.batch_size = max(1, min(batch_size, MAX_BATCH_CREATE_SIZE))

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self.runtime = MigrationRuntimeStatus()

    def _log(self, message: str) -> None:
        with self._lock:
            timestamp = time.strftime("%H:%M:%S")
            self.runtime.message = message
            self.runtime.logs.append(f"[{timestamp}] {message}")
            if len(self.runtime.logs) > 200:
                self.runtime.logs = self.runtime.logs[-200:]

    def is_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def scan_only(self, source_path: str, album_name: str = DEFAULT_ALBUM_NAME) -> dict[str, Any]:
        """Escaneia a pasta/ZIP e registra itens pendentes sem enviar nada (útil para pré-visualização)."""
        report: ScanReport = scan_amazon_export(
            source_path,
            extract_dir=self.extract_dir,
            on_progress=self._log,
        )
        upsert_stats = self.state_store.upsert_discovered_items(report.items, album_name)
        summary = self.state_store.get_summary(album_name)
        return {
            "source_path": report.source_path,
            "album_name": album_name,
            "total_files_found": report.total_files_found,
            "total_bytes": report.total_bytes,
            "extracted_zips": report.extracted_zips,
            "skipped_unsupported": report.skipped_unsupported,
            "newly_queued": upsert_stats["added"],
            "already_known": upsert_stats["duplicates"],
            "summary": summary,
        }

    def start(
        self,
        source_path: str,
        album_name: str = DEFAULT_ALBUM_NAME,
        *,
        dry_run: bool = False,
        retry_failed: bool = False,
    ) -> None:
        """Inicia a migração em segundo plano."""
        with self._lock:
            if self.is_running():
                raise RuntimeError("Já existe uma migração em andamento.")
            self._stop_event.clear()
            self.runtime = MigrationRuntimeStatus(
                state="scanning",
                album_name=album_name.strip() or DEFAULT_ALBUM_NAME,
                source_path=source_path,
                dry_run=dry_run,
                started_at=time.time(),
            )
            self._log(
                f"Iniciando {'simulação (dry-run)' if dry_run else 'migração'} para o álbum '{self.runtime.album_name}'..."
            )
            self._worker_thread = threading.Thread(
                target=self._run_pipeline,
                args=(source_path, self.runtime.album_name, dry_run, retry_failed),
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self) -> None:
        """Solicita pausa segura após concluir o lote atual."""
        self._stop_event.set()
        self._log("Pausa solicitada. Aguardando finalização segura dos uploads atuais...")

    def wait(self, timeout: float | None = None) -> None:
        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)

    def get_status(self, album_name: str | None = None) -> dict[str, Any]:
        with self._lock:
            target_album = album_name or self.runtime.album_name
            summary = self.state_store.get_summary(target_album)
            recent = self.state_store.get_recent_items(target_album, limit=40)
            elapsed = None
            if self.runtime.started_at:
                end = self.runtime.finished_at or time.time()
                elapsed = round(end - self.runtime.started_at, 1)
            return {
                "state": self.runtime.state,
                "album_name": target_album,
                "album_id": self.runtime.album_id or self.state_store.get_album_id(target_album),
                "source_path": self.runtime.source_path,
                "dry_run": self.runtime.dry_run,
                "message": self.runtime.message,
                "elapsed_seconds": elapsed,
                "summary": summary,
                "recent_items": recent,
                "logs": list(self.runtime.logs[-60:]),
            }

    def _run_pipeline(
        self,
        source_path: str,
        album_name: str,
        dry_run: bool,
        retry_failed: bool,
    ) -> None:
        try:
            if retry_failed:
                reset_count = self.state_store.reset_failed_to_pending(album_name)
                if reset_count:
                    self._log(f"{reset_count} item(ns) com falha foram recolocados na fila.")

            self._log(f"Escaneando origem: {source_path}")
            report = scan_amazon_export(
                source_path,
                extract_dir=self.extract_dir,
                on_progress=self._log,
            )
            stats = self.state_store.upsert_discovered_items(report.items, album_name)
            self._log(
                f"Descoberta concluída: {report.total_files_found} mídias encontradas "
                f"({stats['added']} novas, {stats['duplicates']} já registradas)."
            )

            pending_items = self.state_store.get_items_by_status(album_name, ("pending",))
            if not pending_items:
                with self._lock:
                    self.runtime.state = "completed"
                    self.runtime.finished_at = time.time()
                self._log("Nenhum item pendente! Todas as fotos já foram migradas.")
                return

            if dry_run:
                with self._lock:
                    self.runtime.state = "completed"
                    self.runtime.finished_at = time.time()
                self._log(
                    f"Simulação concluída: {len(pending_items)} mídias prontas para envio ao álbum '{album_name}'."
                )
                return

            with self._lock:
                self.runtime.state = "running"

            album_id = self._resolve_album_id(album_name)
            with self._lock:
                self.runtime.album_id = album_id

            self._process_batches(album_id, pending_items)

            with self._lock:
                self.runtime.finished_at = time.time()
                if self._stop_event.is_set():
                    self.runtime.state = "paused"
                    self._log("Migração pausada. Você pode retomar depois sem duplicar fotos.")
                else:
                    self.runtime.state = "completed"
                    summary = self.state_store.get_summary(album_name)
                    self._log(
                        f"Migração finalizada! Enviadas: {summary['uploaded']} | Falhas: {summary['failed']}."
                    )
        except Exception as exc:
            with self._lock:
                self.runtime.state = "error"
                self.runtime.finished_at = time.time()
            self._log(f"Erro na migração: {exc}")

    def _resolve_album_id(self, album_name: str) -> str:
        cached = self.state_store.get_album_id(album_name)
        if cached:
            self._log(f"Reutilizando álbum '{album_name}' (ID em cache).")
            return cached

        self._log(f"Verificando ou criando álbum '{album_name}' no Google Fotos...")
        album = self.google_client.get_or_create_album(album_name)
        album_id = album["id"]
        self.state_store.save_album_id(album_name, album_id)
        self._log(f"Álbum '{album_name}' pronto no Google Fotos.")
        return album_id

    def _upload_single_bytes(self, item: MigrationItem) -> tuple[MigrationItem, str | None, str | None]:
        if self._stop_event.is_set():
            return item, None, "Pausado pelo usuário"
        self.state_store.mark_status(item.id, "uploading")
        try:
            token = self.google_client.upload_media_bytes(Path(item.file_path), item.mime_type)
            return item, token, None
        except Exception as exc:
            return item, None, str(exc)

    def _process_batches(self, album_id: str, items: list[MigrationItem]) -> None:
        total = len(items)
        processed = 0

        for start_idx in range(0, total, self.batch_size):
            if self._stop_event.is_set():
                break

            chunk = items[start_idx : start_idx + self.batch_size]
            uploaded_batch: list[tuple[MigrationItem, str]] = []

            self._log(
                f"Enviando lote {start_idx // self.batch_size + 1} "
                f"({start_idx + 1}-{min(start_idx + len(chunk), total)} de {total})..."
            )

            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = [executor.submit(self._upload_single_bytes, item) for item in chunk]
                for future in as_completed(futures):
                    item, upload_token, error = future.result()
                    if error:
                        if self._stop_event.is_set() and error == "Pausado pelo usuário":
                            self.state_store.mark_status(item.id, "pending")
                        else:
                            self.state_store.mark_status(item.id, "failed", error_message=error)
                            self._log(f"Falha no upload de {item.file_name}: {error}")
                    elif upload_token:
                        uploaded_batch.append((item, upload_token))

            if not uploaded_batch:
                continue

            # Etapa 2: batchCreate serializado (boa prática oficial da Google Photos API)
            payload = [
                {
                    "upload_token": token,
                    "file_name": item.file_name,
                    "description": "Migrado do Amazon Photos",
                }
                for item, token in uploaded_batch
            ]

            try:
                results = self.google_client.batch_create_media_items(album_id, payload)
                token_to_item = {token: item for item, token in uploaded_batch}
                handled_ids: set[int] = set()

                for res in results:
                    token = res.get("uploadToken")
                    item = token_to_item.get(token)
                    if not item:
                        continue
                    handled_ids.add(item.id)
                    status_obj = res.get("status", {})
                    code = status_obj.get("code", 0)
                    if code == 0:
                        media_id = res.get("mediaItem", {}).get("id")
                        self.state_store.mark_status(
                            item.id, "uploaded", google_media_id=media_id, error_message=None
                        )
                        processed += 1
                    else:
                        msg = status_obj.get("message", f"Erro código {code}")
                        self.state_store.mark_status(item.id, "failed", error_message=msg)
                        self._log(f"Erro ao registrar {item.file_name} no álbum: {msg}")

                for item, _ in uploaded_batch:
                    if item.id not in handled_ids:
                        self.state_store.mark_status(
                            item.id,
                            "failed",
                            error_message="Resposta do batchCreate não incluiu este item.",
                        )
            except Exception as exc:
                for item, _ in uploaded_batch:
                    self.state_store.mark_status(item.id, "failed", error_message=str(exc))
                self._log(f"Falha no batchCreate do lote: {exc}")

            self._log(f"Progresso: {processed}/{total} mídias adicionadas ao álbum.")
