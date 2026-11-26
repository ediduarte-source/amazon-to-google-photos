"""Cliente OAuth2 e REST para a Google Photos Library API."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_TOKEN_PATH,
    GOOGLE_PHOTOS_ALBUMS_URL,
    GOOGLE_PHOTOS_BATCH_CREATE_URL,
    GOOGLE_PHOTOS_SCOPES,
    GOOGLE_PHOTOS_UPLOAD_URL,
    MAX_BATCH_CREATE_SIZE,
)


class GooglePhotosError(RuntimeError):
    """Erro retornado pela Google Photos Library API."""


class GooglePhotosClient:
    """Gerencia autenticação OAuth 2.0 e operações de álbum/upload no Google Fotos."""

    def __init__(
        self,
        credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
        token_path: Path = DEFAULT_TOKEN_PATH,
        session: requests.Session | None = None,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.session = session or requests.Session()
        self._creds: Credentials | None = None

    def has_client_secrets(self) -> bool:
        return self.credentials_path.is_file()

    def is_authenticated(self) -> bool:
        creds = self._load_saved_credentials()
        if not creds:
            return False
        if creds.valid:
            return True
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_credentials(creds)
                return True
            except Exception:
                return False
        return False

    def _load_saved_credentials(self) -> Credentials | None:
        if self._creds and self._creds.valid:
            return self._creds
        if not self.token_path.is_file():
            return None
        try:
            self._creds = Credentials.from_authorized_user_file(
                str(self.token_path), GOOGLE_PHOTOS_SCOPES
            )
            return self._creds
        except Exception:
            return None

    def _save_credentials(self, creds: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        self._creds = creds

    def save_client_secrets(self, raw_json: str) -> None:
        parsed = json.loads(raw_json)
        if "installed" not in parsed and "web" not in parsed:
            raise ValueError(
                "JSON inválido: o arquivo credentials.json do Google Cloud deve conter a chave 'installed' ou 'web'."
            )
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials_path.write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def authenticate_interactive(self, port: int = 0) -> None:
        """Abre o fluxo OAuth 2.0 local no navegador do usuário e salva token.json."""
        if not self.has_client_secrets():
            raise FileNotFoundError(
                f"Arquivo credentials.json não encontrado em {self.credentials_path}."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path), GOOGLE_PHOTOS_SCOPES
        )
        creds = flow.run_local_server(port=port, prompt="consent")
        self._save_credentials(creds)

    def build_auth_url(self, redirect_uri: str = "http://localhost") -> str:
        """Gera a URL de autorização OAuth 2.0 para abrir em qualquer navegador."""
        if not self.has_client_secrets():
            raise FileNotFoundError(
                f"Arquivo credentials.json não encontrado em {self.credentials_path}."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            GOOGLE_PHOTOS_SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        self._pending_flow = flow
        return auth_url

    def exchange_auth_code(self, code_or_url: str, redirect_uri: str = "http://localhost") -> None:
        """Conclui o OAuth 2.0 a partir do código ou da URL completa de redirecionamento."""
        from urllib.parse import parse_qs, urlparse

        raw = code_or_url.strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            parsed = urlparse(raw)
            codes = parse_qs(parsed.query).get("code")
            if not codes:
                raise ValueError("A URL colada não contém o parâmetro 'code'.")
            code = codes[0]
        else:
            code = raw

        if not code:
            raise ValueError("Informe o código de autorização ou a URL de redirecionamento.")

        flow = getattr(self, "_pending_flow", None)
        if flow is None:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path),
                GOOGLE_PHOTOS_SCOPES,
                redirect_uri=redirect_uri,
            )
        flow.fetch_token(code=code)
        self._save_credentials(flow.credentials)
        self._pending_flow = None

    def disconnect(self) -> None:
        self._creds = None
        if self.token_path.exists():
            self.token_path.unlink()

    def _get_access_token(self) -> str:
        creds = self._load_saved_credentials()
        if not creds:
            raise GooglePhotosError(
                "Conta Google não conectada. Faça login via OAuth antes de iniciar a migração."
            )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_credentials(creds)
        if not creds.valid or not creds.token:
            raise GooglePhotosError("Token OAuth expirado ou inválido. Reconecte sua conta Google.")
        return creds.token

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: Any = None,
        max_retries: int = 4,
    ) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            token = self._get_access_token()
            req_headers = {"Authorization": f"Bearer {token}"}
            if headers:
                req_headers.update(headers)

            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=req_headers,
                    params=params,
                    json=json_body,
                    data=data,
                    timeout=DEFAULT_REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(min(2**attempt, 16))
                    continue
                raise GooglePhotosError(f"Erro de rede ao acessar Google Fotos: {exc}") from exc

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 20)
                time.sleep(wait_seconds)
                continue

            if resp.status_code >= 400:
                raise GooglePhotosError(
                    f"Google Photos API retornou HTTP {resp.status_code}: {resp.text[:400]}"
                )
            return resp

        raise GooglePhotosError(f"Falha após múltiplas tentativas: {last_error}")

    def list_app_albums(self) -> list[dict[str, Any]]:
        """Lista álbuns criados por esta aplicação (escopo readonly.appcreateddata)."""
        albums: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            resp = self._request_with_retry("GET", GOOGLE_PHOTOS_ALBUMS_URL, params=params)
            payload = resp.json()
            albums.extend(payload.get("albums", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return albums

    def get_or_create_album(self, title: str) -> dict[str, Any]:
        """Reutiliza um álbum existente criado pelo app com o mesmo título ou cria um novo."""
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("O nome do álbum não pode ser vazio.")

        for album in self.list_app_albums():
            if album.get("title", "").strip().lower() == clean_title.lower():
                return album

        resp = self._request_with_retry(
            "POST",
            GOOGLE_PHOTOS_ALBUMS_URL,
            json_body={"album": {"title": clean_title}},
        )
        return resp.json()

    def upload_media_bytes(self, file_path: Path, mime_type: str) -> str:
        """Etapa 1 da API: envia os bytes brutos e retorna o uploadToken."""
        path = Path(file_path)
        headers = {
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol": "raw",
        }
        with path.open("rb") as fh:
            resp = self._request_with_retry(
                "POST",
                GOOGLE_PHOTOS_UPLOAD_URL,
                headers=headers,
                data=fh,
            )
        upload_token = resp.text.strip()
        if not upload_token:
            raise GooglePhotosError(f"Upload de {path.name} não retornou uploadToken.")
        return upload_token

    def batch_create_media_items(
        self,
        album_id: str,
        uploaded_items: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """
        Etapa 2 da API: cria até 50 itens de mídia no álbum especificado em uma única chamada.

        Cada item em `uploaded_items` deve conter `upload_token` e `file_name` (e opcionalmente `description`).
        """
        if not uploaded_items:
            return []
        if len(uploaded_items) > MAX_BATCH_CREATE_SIZE:
            raise ValueError(
                f"batchCreate aceita no máximo {MAX_BATCH_CREATE_SIZE} itens por chamada."
            )

        body = {
            "albumId": album_id,
            "newMediaItems": [
                {
                    "description": item.get("description", ""),
                    "simpleMediaItem": {
                        "fileName": item["file_name"][:255],
                        "uploadToken": item["upload_token"],
                    },
                }
                for item in uploaded_items
            ],
        }
        resp = self._request_with_retry(
            "POST",
            GOOGLE_PHOTOS_BATCH_CREATE_URL,
            json_body=body,
        )
        return resp.json().get("newMediaItemResults", [])
