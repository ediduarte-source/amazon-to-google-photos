"""Configurações e constantes da aplicação de migração."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MIGRATOR_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_PATH = DATA_DIR / "migration_state.db"
DEFAULT_CREDENTIALS_PATH = DATA_DIR / "credentials.json"
DEFAULT_TOKEN_PATH = DATA_DIR / "token.json"
DEFAULT_EXTRACT_DIR = DATA_DIR / "extracted"

DEFAULT_ALBUM_NAME = "Porta Retrato"

# Escopos válidos da Google Photos Library API (pós-março/2025):
# - appendonly: permite enviar bytes, criar itens de mídia e criar álbuns.
# - readonly.appcreateddata: permite listar álbuns criados por esta aplicação
#   para reutilizar o álbum "Porta Retrato" em retomadas sem duplicá-lo.
GOOGLE_PHOTOS_SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]

GOOGLE_PHOTOS_API_BASE = "https://photoslibrary.googleapis.com/v1"
GOOGLE_PHOTOS_UPLOAD_URL = f"{GOOGLE_PHOTOS_API_BASE}/uploads"
GOOGLE_PHOTOS_ALBUMS_URL = f"{GOOGLE_PHOTOS_API_BASE}/albums"
GOOGLE_PHOTOS_BATCH_CREATE_URL = f"{GOOGLE_PHOTOS_API_BASE}/mediaItems:batchCreate"

# Limites oficiais da Google Photos Library API
MAX_BATCH_CREATE_SIZE = 50
MAX_ALBUM_ITEMS = 20_000
DEFAULT_UPLOAD_WORKERS = 4
DEFAULT_REQUEST_TIMEOUT = 120

SUPPORTED_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".dng",
    ".cr2",
    ".nef",
    ".arw",
}

SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".3gp",
    ".mts",
    ".m2ts",
    ".wmv",
}

SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS

MIME_TYPE_OVERRIDES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}
