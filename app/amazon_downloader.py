"""Download em massa de todas as fotos e vídeos do Amazon Photos usando cookies da sessão do navegador."""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import DATA_DIR

DEFAULT_AMAZON_DOWNLOAD_DIR = DATA_DIR / "amazon_downloads"

PAGE_SIZE = 200
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
MAX_RETRIES = 6

BASE_PARAMS = {
    "asset": "ALL",
    "tempLink": "false",
    "resourceVersion": "V2",
    "ContentType": "JSON",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Mapeamento do sufixo de cookie (at-acbXX) para o domínio da Amazon
DOMAIN_SUFFIX_MAP = {
    "br": "com.br",
    "uk": "co.uk",
    "jp": "co.jp",
    "au": "com.au",
    "mx": "com.mx",
    "de": "de",
    "es": "es",
    "fr": "fr",
    "it": "it",
    "ca": "ca",
}

# Domínio -> sufixo de cookie esperado para campos manuais na UI
DOMAIN_COOKIE_NAMES = {
    "com": ("ubid-main", "at-main"),
    "com.br": ("ubid-acbbr", "at-acbbr"),
    "co.uk": ("ubid-acbuk", "at-acbuk"),
    "de": ("ubid-acbde", "at-acbde"),
    "es": ("ubid-acbes", "at-acbes"),
    "fr": ("ubid-acbfr", "at-acbfr"),
    "it": ("ubid-acbit", "at-acbit"),
    "ca": ("ubid-acbca", "at-acbca"),
    "co.jp": ("ubid-acbjp", "at-acbjp"),
    "com.au": ("ubid-acbau", "at-acbau"),
    "com.mx": ("ubid-acbmx", "at-acbmx"),
}


class AmazonAuthError(RuntimeError):
    """Erro de autenticação quando a Amazon rejeita os cookies da sessão."""


@dataclass
class AmazonMediaItem:
    id: str
    name: str
    md5: str
    size: int
    created: str
    taken: float | None = None
    target: str = ""


@dataclass
class AmazonDownloadStatus:
    state: str = "idle"  # idle | listing | downloading | paused | completed | error
    domain: str = "com.br"
    output_dir: str = str(DEFAULT_AMAZON_DOWNLOAD_DIR)
    dry_run: bool = False
    total_found: int = 0
    already_downloaded: int = 0
    to_download: int = 0
    downloaded: int = 0
    failed: int = 0
    total_bytes: int = 0
    downloaded_bytes: int = 0
    message: str = "Pronto para baixar da Amazon Photos."
    started_at: float | None = None
    finished_at: float | None = None
    logs: list[str] = field(default_factory=list)


def normalize_cookies(
    *,
    cookies_dict: dict[str, str] | None = None,
    cookies_json: str | None = None,
    session_id: str | None = None,
    ubid: str | None = None,
    at_token: str | None = None,
    domain: str = "com.br",
) -> dict[str, str]:
    """Constrói e valida o dicionário de cookies a partir de JSON ou campos individuais."""
    if cookies_dict is not None:
        parsed = {str(k).strip(): str(v).strip() for k, v in cookies_dict.items() if str(v).strip()}
    elif cookies_json and cookies_json.strip():
        try:
            raw = json.loads(cookies_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON de cookies inválido: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("O JSON de cookies precisa ser um objeto chave-valor.")
        parsed = {str(k).strip(): str(v).strip() for k, v in raw.items() if str(v).strip()}
    else:
        clean_domain = domain.strip().lower().removeprefix("www.").removeprefix("amazon.")
        ubid_key, at_key = DOMAIN_COOKIE_NAMES.get(clean_domain, ("ubid-acbbr", "at-acbbr"))
        parsed = {}
        if session_id and session_id.strip():
            parsed["session-id"] = session_id.strip()
        if ubid and ubid.strip():
            parsed[ubid_key] = ubid.strip()
        if at_token and at_token.strip():
            parsed[at_key] = at_token.strip()

    if "session-id" not in parsed:
        raise ValueError("Cookie obrigatório 'session-id' não informado.")
    has_at = any(k in ("at-main", "at_main") or k.startswith("at-acb") for k in parsed)
    if not has_at:
        raise ValueError(
            "Cookie de autenticação ('at-acbbr', 'at-main' ou 'at-acbXX') não informado."
        )
    return parsed


def detect_amazon_domain(cookies: dict[str, str], override_domain: str | None = None) -> str:
    """Detecta o domínio da Amazon a partir dos nomes dos cookies ou de um override explícito."""
    if override_domain and override_domain.strip():
        return override_domain.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").removeprefix("amazon.").strip("/")

    for key in cookies:
        if key in ("at-main", "at_main", "ubid-main", "ubid_main"):
            return "com"
        if key.startswith("at-acb"):
            suffix = key.removeprefix("at-acb")
            return DOMAIN_SUFFIX_MAP.get(suffix, suffix)
        if key.startswith("ubid-acb"):
            suffix = key.removeprefix("ubid-acb")
            return DOMAIN_SUFFIX_MAP.get(suffix, suffix)

    return "com.br"


def _parse_iso_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _node_to_item(node: dict[str, Any]) -> AmazonMediaItem | None:
    cp = node.get("contentProperties") or {}
    md5 = cp.get("md5")
    if not md5:
        return None
    meta = cp.get("image") or cp.get("video") or {}
    taken = (
        _parse_iso_timestamp(meta.get("dateTimeOriginal"))
        or _parse_iso_timestamp(meta.get("dateTime"))
        or _parse_iso_timestamp(meta.get("creationDate"))
        or _parse_iso_timestamp(cp.get("contentDate"))
        or _parse_iso_timestamp(node.get("createdDate"))
    )
    return AmazonMediaItem(
        id=node["id"],
        name=node.get("name") or node["id"],
        md5=md5,
        size=int(cp.get("size", 0)),
        created=node.get("createdDate", ""),
        taken=taken,
    )


def assign_unique_filenames(items: list[AmazonMediaItem]) -> list[AmazonMediaItem]:
    """Remove duplicatas por MD5 e garante nomes de arquivo únicos e estáveis entre execuções."""
    unique: dict[str, AmazonMediaItem] = {}
    for item in sorted(items, key=lambda i: (i.created, i.id)):
        unique.setdefault(item.md5, item)

    used_lower: set[str] = set()
    for item in unique.values():
        clean = item.name.replace("/", "_").replace("\\", "_").strip() or item.id
        if clean.lower() in used_lower:
            stem, dot, ext = clean.rpartition(".")
            clean = f"{stem}_{item.md5[:8]}.{ext}" if dot else f"{clean}_{item.md5[:8]}"
        used_lower.add(clean.lower())
        item.target = clean
    return list(unique.values())


class AmazonLibraryDownloader:
    """Baixa todas as fotos e vídeos do Amazon Photos em paralelo, com validação MD5 e retomada."""

    def __init__(
        self,
        *,
        default_output_dir: Path = DEFAULT_AMAZON_DOWNLOAD_DIR,
        client_factory: Callable[[dict[str, str], int], httpx.Client] | None = None,
    ) -> None:
        self.default_output_dir = Path(default_output_dir)
        self._client_factory = client_factory or self._default_client_factory
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self.status = AmazonDownloadStatus(output_dir=str(self.default_output_dir))

    @staticmethod
    def _default_client_factory(cookies: dict[str, str], workers: int) -> httpx.Client:
        return httpx.Client(
            cookies=cookies,
            headers={
                "user-agent": USER_AGENT,
                "x-amzn-sessionid": cookies["session-id"],
            },
            follow_redirects=True,
            timeout=httpx.Timeout(120.0, connect=30.0),
            limits=httpx.Limits(
                max_connections=max(workers * 2, 16),
                max_keepalive_connections=max(workers * 2, 16),
            ),
        )

    def _log(self, msg: str) -> None:
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self.status.message = msg
            self.status.logs.append(f"[{ts}] {msg}")
            if len(self.status.logs) > 200:
                self.status.logs = self.status.logs[-200:]

    def is_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def stop(self) -> None:
        self._stop_event.set()
        self._log("Pausa solicitada. Encerrando downloads em andamento com segurança...")

    def wait(self, timeout: float | None = None) -> None:
        if self._worker_thread:
            self._worker_thread.join(timeout=timeout)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            elapsed = None
            if self.status.started_at:
                end = self.status.finished_at or time.time()
                elapsed = round(end - self.status.started_at, 1)
            return {
                "state": self.status.state,
                "domain": self.status.domain,
                "output_dir": self.status.output_dir,
                "dry_run": self.status.dry_run,
                "total_found": self.status.total_found,
                "already_downloaded": self.status.already_downloaded,
                "to_download": self.status.to_download,
                "downloaded": self.status.downloaded,
                "failed": self.status.failed,
                "total_bytes": self.status.total_bytes,
                "downloaded_bytes": self.status.downloaded_bytes,
                "elapsed_seconds": elapsed,
                "message": self.status.message,
                "logs": list(self.status.logs[-80:]),
            }

    def start(
        self,
        cookies: dict[str, str],
        *,
        domain: str | None = None,
        output_dir: Path | str | None = None,
        workers: int = 8,
        limit: int | None = None,
        dry_run: bool = False,
        on_complete: Callable[[Path], None] | None = None,
    ) -> None:
        with self._lock:
            if self.is_running():
                raise RuntimeError("Já existe um download do Amazon Photos em andamento.")
            resolved_domain = detect_amazon_domain(cookies, domain)
            resolved_out = Path(output_dir).expanduser().resolve() if output_dir else self.default_output_dir.resolve()
            self._stop_event.clear()
            self.status = AmazonDownloadStatus(
                state="listing",
                domain=resolved_domain,
                output_dir=str(resolved_out),
                dry_run=dry_run,
                started_at=time.time(),
            )
            self._log(
                f"Conectando ao Amazon Photos (amazon.{resolved_domain})..."
            )
            self._worker_thread = threading.Thread(
                target=self._run,
                args=(cookies, resolved_domain, resolved_out, max(1, min(workers, 16)), limit, dry_run, on_complete),
                daemon=True,
            )
            self._worker_thread.start()

    def _get_json(self, client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES):
            try:
                resp = client.get(url, params=BASE_PARAMS | params)
                if resp.status_code in (401, 403):
                    raise AmazonAuthError(
                        f"A Amazon recusou a sessão (HTTP {resp.status_code}). "
                        "Sua sessão expirou: clique em 'Sair' e entre novamente com e-mail e senha."
                    )
                resp.raise_for_status()
                return resp.json()
            except AmazonAuthError:
                raise
            except Exception as exc:
                if attempt == MAX_RETRIES - 1:
                    raise
                wait = min(2**attempt, 20) + random.random()
                self._log(f"Erro temporário ({exc}), tentando novamente em {wait:.0f}s...")
                if self._stop_event.wait(wait):
                    raise InterruptedError("Download pausado pelo usuário.")
        raise RuntimeError("Falha ao consultar API do Amazon Photos.")

    def _list_library(
        self,
        client: httpx.Client,
        drive_base: str,
        max_items: int | None = None,
    ) -> list[AmazonMediaItem]:
        items: dict[str, AmazonMediaItem] = {}
        offset = 0
        total_reported: int | None = None

        while not self._stop_event.is_set():
            payload = self._get_json(
                client,
                f"{drive_base}/search",
                {
                    "filters": "type:(PHOTOS OR VIDEOS)",
                    "sort": "['createdDate DESC']",
                    "searchContext": "customer",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
            )
            total_reported = payload.get("count", total_reported)
            nodes = payload.get("data") or []
            for node in nodes:
                parsed = _node_to_item(node)
                if parsed:
                    items[parsed.id] = parsed

            offset += PAGE_SIZE
            self._log(
                f"Listando biblioteca Amazon Photos: {len(items)}"
                + (f"/{total_reported}" if total_reported else "")
                + " itens..."
            )
            if not nodes or (total_reported is not None and offset >= total_reported):
                break
            if max_items and len(items) >= max_items:
                break

        result = list(items.values())
        if max_items:
            result = sorted(result, key=lambda i: i.created, reverse=True)[:max_items]
        return result

    def _download_one(
        self,
        client: httpx.Client,
        drive_base: str,
        owner_id: str,
        output_dir: Path,
        item: AmazonMediaItem,
    ) -> str | None:
        dest = output_dir / item.target
        part = dest.with_name(dest.name + ".part")
        params = {"querySuffix": "?download=true", "ownerId": owner_id}
        last_err: str | None = None

        for attempt in range(MAX_RETRIES):
            if self._stop_event.is_set():
                return "stopped"
            try:
                md5 = hashlib.md5()
                url = f"{drive_base}/nodes/{item.id}/contentRedirection"
                with client.stream("GET", url, params=params) as resp:
                    if resp.status_code in (401, 403):
                        raise AmazonAuthError(f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    with part.open("wb") as fp:
                        for chunk in resp.iter_bytes(CHUNK_SIZE):
                            if self._stop_event.is_set():
                                raise InterruptedError("stopped")
                            fp.write(chunk)
                            md5.update(chunk)

                if md5.hexdigest() != item.md5:
                    raise ValueError(f"Checksum MD5 inválido ({md5.hexdigest()} != {item.md5})")

                part.replace(dest)
                if item.taken:
                    os.utime(dest, (item.taken, item.taken))

                with self._lock:
                    self.status.downloaded += 1
                    self.status.downloaded_bytes += item.size
                return None
            except InterruptedError:
                part.unlink(missing_ok=True)
                return "stopped"
            except AmazonAuthError as exc:
                part.unlink(missing_ok=True)
                self._stop_event.set()
                return f"auth: {exc}"
            except Exception as exc:
                last_err = str(exc)
                part.unlink(missing_ok=True)
                if attempt < MAX_RETRIES - 1:
                    wait = min(2**attempt, 20) + random.random()
                    if self._stop_event.wait(wait):
                        return "stopped"
        return last_err or "Erro desconhecido no download."

    def _run(
        self,
        cookies: dict[str, str],
        domain: str,
        output_dir: Path,
        workers: int,
        limit: int | None,
        dry_run: bool,
        on_complete: Callable[[Path], None] | None,
    ) -> None:
        drive_base = f"https://www.amazon.{domain}/drive/v1"
        try:
            with self._client_factory(cookies, workers) as client:
                root_resp = self._get_json(client, f"{drive_base}/nodes", {"filters": "isRoot:true"})
                root_nodes = root_resp.get("data") or []
                if not root_nodes:
                    raise AmazonAuthError("Não foi possível identificar a pasta raiz da conta Amazon Photos.")
                owner_id = root_nodes[0]["ownerId"]
                self._log(f"Conectado com sucesso em amazon.{domain}!")

                raw_items = self._list_library(client, drive_base, max_items=limit)
                unique_items = assign_unique_filenames(raw_items)

                output_dir.mkdir(parents=True, exist_ok=True)
                for stale in output_dir.glob("*.part"):
                    stale.unlink(missing_ok=True)

                existing_sizes = {
                    p.name: p.stat().st_size for p in output_dir.iterdir() if p.is_file()
                }
                todo = [it for it in unique_items if existing_sizes.get(it.target) != it.size]
                todo.sort(key=lambda i: i.size, reverse=True)

                already = len(unique_items) - len(todo)
                total_bytes = sum(it.size for it in todo)

                with self._lock:
                    self.status.total_found = len(unique_items)
                    self.status.already_downloaded = already
                    self.status.to_download = len(todo)
                    self.status.total_bytes = total_bytes

                self._log(
                    f"Encontrados {len(unique_items)} itens únicos ({already} já baixados, {len(todo)} para baixar)."
                )

                if dry_run or not todo:
                    with self._lock:
                        self.status.state = "completed"
                        self.status.finished_at = time.time()
                    if dry_run:
                        self._log("Prévia concluída! Clique em 'Baixar Todas as Fotos e Vídeos' para iniciar.")
                    else:
                        self._log("Todas as fotos e vídeos já estão baixados nesta pasta!")
                        if on_complete:
                            on_complete(output_dir)
                    return

                with self._lock:
                    self.status.state = "downloading"

                auth_error_msg: str | None = None
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(self._download_one, client, drive_base, owner_id, output_dir, it): it
                        for it in todo
                    }
                    for done_count, future in enumerate(as_completed(futures), 1):
                        item = futures[future]
                        err = future.result()
                        if err and err != "stopped":
                            if err.startswith("auth:"):
                                auth_error_msg = err
                            else:
                                with self._lock:
                                    self.status.failed += 1
                                self._log(f"Falha ao baixar {item.target}: {err}")
                        if done_count % 10 == 0 or done_count == len(todo):
                            with self._lock:
                                self._log(
                                    f"Download Amazon: {self.status.downloaded}/{len(todo)} concluídos "
                                    f"({self.status.failed} falhas)."
                                )

                with self._lock:
                    self.status.finished_at = time.time()
                    if auth_error_msg:
                        self.status.state = "error"
                        self._log(
                            "A sessão da Amazon expirou durante o download. Entre novamente com e-mail e senha e retome "
                            "(os arquivos já baixados serão mantidos)."
                        )
                    elif self._stop_event.is_set():
                        self.status.state = "paused"
                        self._log("Download pausado. Você pode retomar depois sem baixar tudo novamente.")
                    else:
                        self.status.state = "completed"
                        self._log(
                            f"Download finalizado! {self.status.downloaded} arquivos salvos em {output_dir}."
                        )
                        if on_complete:
                            on_complete(output_dir)
        except Exception as exc:
            with self._lock:
                self.status.state = "error"
                self.status.finished_at = time.time()
            self._log(f"Erro no download da Amazon Photos: {exc}")
