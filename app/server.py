"""Servidor FastAPI local para controlar a migração via navegador."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.amazon_downloader import (
    DEFAULT_AMAZON_DOWNLOAD_DIR,
    AmazonLibraryDownloader,
    normalize_cookies,
)
from app.amazon_login import AmazonLoginManager
from app.config import (
    DATA_DIR,
    DEFAULT_ALBUM_NAME,
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_EXTRACT_DIR,
    DEFAULT_TOKEN_PATH,
)
from app.google_photos import GooglePhotosClient
from app.migrator import PhotoMigrator
from app.state import MigrationStateStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ScanRequest(BaseModel):
    source_path: str = Field(..., min_length=1)
    album_name: str = Field(default=DEFAULT_ALBUM_NAME, min_length=1)


class MigrateStartRequest(BaseModel):
    source_path: str = Field(..., min_length=1)
    album_name: str = Field(default=DEFAULT_ALBUM_NAME, min_length=1)
    dry_run: bool = False
    retry_failed: bool = False


class AuthCodeRequest(BaseModel):
    code_or_url: str = Field(..., min_length=1)


class AmazonDownloadStartRequest(BaseModel):
    domain: str | None = None
    session_id: str | None = None
    ubid: str | None = None
    at_token: str | None = None
    cookies_json: str | None = None
    output_dir: str | None = None
    album_name: str = Field(default=DEFAULT_ALBUM_NAME)
    workers: int = Field(default=8, ge=1, le=16)
    limit: int | None = Field(default=None, ge=1)
    dry_run: bool = False


class AmazonLoginRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)
    domain: str = Field(default="com.br")
    show_browser: bool = False


class AmazonOtpRequest(BaseModel):
    code: str = Field(..., min_length=1)


def create_app(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
    extract_dir: Path = DEFAULT_EXTRACT_DIR,
    uploads_dir: Path | None = None,
    amazon_download_dir: Path = DEFAULT_AMAZON_DOWNLOAD_DIR,
    amazon_login: AmazonLoginManager | None = None,
) -> FastAPI:
    app = FastAPI(title="Amazon Photos → Google Fotos (Porta Retrato)", version="1.3.0")

    upload_root = uploads_dir or (DATA_DIR / "uploads")
    upload_root.mkdir(parents=True, exist_ok=True)

    state_store = MigrationStateStore(db_path)
    google_client = GooglePhotosClient(credentials_path=credentials_path, token_path=token_path)
    migrator = PhotoMigrator(state_store, google_client, extract_dir=extract_dir)
    amazon_downloader = AmazonLibraryDownloader(default_output_dir=amazon_download_dir)
    amazon_login = amazon_login or AmazonLoginManager()

    oauth_lock = threading.Lock()
    oauth_state = {"in_progress": False, "last_error": None}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/auth/status")
    def auth_status() -> dict:
        return {
            "has_credentials": google_client.has_client_secrets(),
            "authenticated": google_client.is_authenticated(),
            "oauth_in_progress": oauth_state["in_progress"],
            "last_error": oauth_state["last_error"],
        }

    @app.post("/api/auth/credentials")
    async def upload_credentials(file: UploadFile = File(...)) -> dict:
        raw = (await file.read()).decode("utf-8", errors="strict")
        try:
            google_client.save_client_secrets(raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": "Arquivo credentials.json salvo com sucesso."}

    @app.post("/api/auth/start-url")
    def start_auth_url() -> dict:
        if not google_client.has_client_secrets():
            raise HTTPException(
                status_code=400,
                detail="Envie primeiro o arquivo credentials.json do Google Cloud.",
            )
        try:
            url = google_client.build_auth_url()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "auth_url": url}

    @app.post("/api/auth/exchange-code")
    def exchange_auth_code(req: AuthCodeRequest) -> dict:
        try:
            google_client.exchange_auth_code(req.code_or_url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": "Conta Google conectada com sucesso!"}

    @app.post("/api/auth/connect")
    def connect_google() -> dict:
        if not google_client.has_client_secrets():
            raise HTTPException(
                status_code=400,
                detail="Envie primeiro o arquivo credentials.json do Google Cloud.",
            )
        if oauth_state["in_progress"]:
            return {"ok": True, "message": "Autenticação OAuth já está aberta no navegador."}

        def _run_oauth() -> None:
            with oauth_lock:
                oauth_state["in_progress"] = True
                oauth_state["last_error"] = None
                try:
                    google_client.authenticate_interactive(port=0)
                except Exception as exc:
                    oauth_state["last_error"] = str(exc)
                finally:
                    oauth_state["in_progress"] = False

        threading.Thread(target=_run_oauth, daemon=True).start()
        return {
            "ok": True,
            "message": "Janela de login do Google aberta no navegador. Conclua a autorização lá.",
        }

    @app.post("/api/auth/disconnect")
    def disconnect_google() -> dict:
        google_client.disconnect()
        return {"ok": True, "message": "Conta Google desconectada."}

    @app.post("/api/upload-source")
    async def upload_source_files(
        files: list[UploadFile] = File(...),
        album_name: str = DEFAULT_ALBUM_NAME,
    ) -> dict:
        """Recebe fotos ou arquivos .zip enviados pelo navegador e os indexa para migração."""
        if not files:
            raise HTTPException(status_code=400, detail="Nenhum arquivo enviado.")

        saved_count = 0
        for upload in files:
            safe_name = Path(upload.filename or f"arquivo_{saved_count}").name
            dest = upload_root / safe_name
            with dest.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
            saved_count += 1

        report = migrator.scan_only(str(upload_root), album_name)
        report["uploaded_files"] = saved_count
        return report

    @app.post("/api/scan")
    def scan_source(req: ScanRequest) -> dict:
        try:
            return migrator.scan_only(req.source_path, req.album_name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/migrate/start")
    def start_migration(req: MigrateStartRequest) -> dict:
        if not req.dry_run and not google_client.is_authenticated():
            raise HTTPException(
                status_code=400,
                detail="Conecte sua conta Google antes de iniciar a migração real (ou use o modo Simulação).",
            )
        try:
            migrator.start(
                req.source_path,
                req.album_name,
                dry_run=req.dry_run,
                retry_failed=req.retry_failed,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": migrator.get_status(req.album_name)}

    @app.post("/api/migrate/stop")
    def stop_migration() -> dict:
        migrator.stop()
        return {"ok": True, "status": migrator.get_status()}

    @app.post("/api/migrate/retry")
    def retry_failed(album_name: str = DEFAULT_ALBUM_NAME) -> dict:
        count = state_store.reset_failed_to_pending(album_name)
        return {"ok": True, "reset_count": count, "status": migrator.get_status(album_name)}

    @app.get("/api/migrate/status")
    def migration_status(album_name: str | None = None) -> dict:
        return migrator.get_status(album_name)

    @app.post("/api/amazon/login")
    def amazon_login_start(req: AmazonLoginRequest) -> dict:
        try:
            amazon_login.start(
                req.email, req.password, domain=req.domain, show_browser=req.show_browser
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": amazon_login.get_status()}

    @app.post("/api/amazon/login/otp")
    def amazon_login_otp(req: AmazonOtpRequest) -> dict:
        try:
            amazon_login.submit_otp(req.code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": amazon_login.get_status()}

    @app.get("/api/amazon/login/status")
    def amazon_login_status() -> dict:
        return amazon_login.get_status()

    @app.post("/api/amazon/logout")
    def amazon_logout() -> dict:
        amazon_login.logout()
        return {"ok": True, "status": amazon_login.get_status()}

    @app.post("/api/amazon/download/start")
    def start_amazon_download(req: AmazonDownloadStartRequest) -> dict:
        manual = any([req.cookies_json, req.session_id, req.ubid, req.at_token])
        try:
            if manual:
                domain = req.domain
                cookies = normalize_cookies(
                    cookies_json=req.cookies_json,
                    session_id=req.session_id,
                    ubid=req.ubid,
                    at_token=req.at_token,
                    domain=domain or "com.br",
                )
            else:
                session = amazon_login.get_session()
                if not session:
                    raise HTTPException(
                        status_code=400,
                        detail="Entre na sua conta Amazon (e-mail e senha) antes de baixar as fotos.",
                    )
                cookies, domain = session
            amazon_downloader.start(
                cookies,
                domain=domain,
                output_dir=req.output_dir,
                workers=req.workers,
                limit=req.limit,
                dry_run=req.dry_run,
                on_complete=lambda out_dir: migrator.scan_only(str(out_dir), req.album_name),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "status": amazon_downloader.get_status()}

    @app.post("/api/amazon/download/stop")
    def stop_amazon_download() -> dict:
        amazon_downloader.stop()
        return {"ok": True, "status": amazon_downloader.get_status()}

    @app.get("/api/amazon/download/status")
    def amazon_download_status() -> dict:
        return amazon_downloader.get_status()

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
