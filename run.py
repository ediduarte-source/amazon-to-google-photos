#!/usr/bin/env python3
"""Ponto de entrada da aplicação: interface web local ou modo CLI."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

import uvicorn

from app.config import (
    DEFAULT_ALBUM_NAME,
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_EXTRACT_DIR,
    DEFAULT_TOKEN_PATH,
    DEFAULT_UPLOAD_WORKERS,
)
from app.google_photos import GooglePhotosClient
from app.migrator import PhotoMigrator
from app.state import MigrationStateStore


def run_cli(args: argparse.Namespace) -> int:
    store = MigrationStateStore(DEFAULT_DB_PATH)
    client = GooglePhotosClient(
        credentials_path=DEFAULT_CREDENTIALS_PATH,
        token_path=DEFAULT_TOKEN_PATH,
    )

    if not args.dry_run and not client.is_authenticated():
        if not client.has_client_secrets():
            print(
                f"Erro: coloque seu arquivo credentials.json em {DEFAULT_CREDENTIALS_PATH}\n"
                "Veja as instruções no README.md."
            )
            return 1
        print("Abrindo navegador para autenticar sua conta Google...")
        client.authenticate_interactive(port=0)

    migrator = PhotoMigrator(
        store,
        client,
        extract_dir=DEFAULT_EXTRACT_DIR,
        workers=args.workers,
    )
    migrator.start(
        args.source,
        args.album,
        dry_run=args.dry_run,
        retry_failed=args.retry_failed,
    )

    last_log_count = 0
    try:
        while migrator.is_running():
            status = migrator.get_status(args.album)
            logs = status["logs"]
            for line in logs[last_log_count:]:
                print(line)
            last_log_count = len(logs)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nInterrupção solicitada. Pausando com segurança...")
        migrator.stop()
        migrator.wait()

    final_status = migrator.get_status(args.album)
    for line in final_status["logs"][last_log_count:]:
        print(line)

    summary = final_status["summary"]
    print(
        f"\nResumo ({args.album}): total={summary['total']} | "
        f"enviadas={summary['uploaded']} | pendentes={summary['pending']} | falhas={summary['failed']}"
    )
    return 0 if final_status["state"] == "completed" and summary["failed"] == 0 else 1


def amazon_login_cli(args: argparse.Namespace) -> tuple[dict[str, str], str] | None:
    """Faz login na Amazon pelo terminal (e-mail + senha) ou reutiliza a sessão salva."""
    import getpass
    import os

    from app.amazon_login import AmazonLoginManager

    manager = AmazonLoginManager()
    if not args.amazon_relogin:
        session = manager.get_session()
        if session:
            print(f"Usando a sessão Amazon salva ({manager.get_status()['email']}). "
                  "Use --amazon-relogin para entrar com outra conta.")
            return session

    email = args.amazon_email or input("E-mail da Amazon: ").strip()
    password = os.environ.get("AMAZON_PASSWORD") or getpass.getpass("Senha da Amazon: ")
    manager.start(
        email,
        password,
        domain=args.amazon_domain or "com.br",
        show_browser=args.show_browser,
    )

    last_msg = None
    try:
        while manager.is_running():
            st = manager.get_status()
            if st["message"] != last_msg:
                print(st["message"])
                last_msg = st["message"]
            if st["state"] == "need_otp":
                code = input("Código de verificação: ").strip()
                try:
                    manager.submit_otp(code)
                except (ValueError, RuntimeError) as exc:
                    print(f"Erro: {exc}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nLogin cancelado.")
        manager.cancel()
        return None

    st = manager.get_status()
    if st["message"] != last_msg:
        print(st["message"])
    return manager.get_session() if st["logged_in"] else None


def run_amazon_download_cli(args: argparse.Namespace) -> int:
    from pathlib import Path

    from app.amazon_downloader import (
        DEFAULT_AMAZON_DOWNLOAD_DIR,
        AmazonLibraryDownloader,
        normalize_cookies,
    )

    if args.amazon_cookies:
        cookies_path = Path(args.amazon_cookies).expanduser()
        if not cookies_path.is_file():
            print(f"Erro: arquivo de cookies não encontrado em {cookies_path}")
            return 1
        cookies = normalize_cookies(cookies_json=cookies_path.read_text(encoding="utf-8"))
        domain = args.amazon_domain
    else:
        session = amazon_login_cli(args)
        if not session:
            return 1
        cookies, domain = session

    out_dir = Path(args.amazon_out).expanduser() if args.amazon_out else DEFAULT_AMAZON_DOWNLOAD_DIR
    downloader = AmazonLibraryDownloader(default_output_dir=out_dir)
    downloader.start(
        cookies,
        domain=domain,
        output_dir=out_dir,
        workers=args.workers,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    last_log_count = 0
    try:
        while downloader.is_running():
            st = downloader.get_status()
            for line in st["logs"][last_log_count:]:
                print(line)
            last_log_count = len(st["logs"])
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nPausando download da Amazon Photos...")
        downloader.stop()
        downloader.wait()

    final = downloader.get_status()
    for line in final["logs"][last_log_count:]:
        print(line)
    if final["state"] == "error" and not args.amazon_cookies:
        print("Dica: se a sessão expirou, rode novamente com --amazon-relogin.")
    return 0 if final["state"] == "completed" and final["failed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrador de fotos do Amazon Photos para o Google Fotos (álbum Porta Retrato)."
    )
    parser.add_argument("--cli", action="store_true", help="Executa em modo linha de comando.")
    parser.add_argument("--download-amazon", action="store_true", help="Baixa todas as fotos/vídeos do Amazon Photos de uma vez.")
    parser.add_argument("--amazon-email", type=str, default=None, help="E-mail da conta Amazon (a senha é pedida no terminal ou lida de AMAZON_PASSWORD).")
    parser.add_argument("--amazon-relogin", action="store_true", help="Ignora a sessão Amazon salva e faz login novamente.")
    parser.add_argument("--show-browser", action="store_true", help="Mostra a janela do navegador durante o login da Amazon.")
    parser.add_argument("--amazon-cookies", type=str, default=None, help="(Avançado) Usa um arquivo cookies.json em vez de e-mail e senha.")
    parser.add_argument("--amazon-domain", type=str, default=None, help="Domínio Amazon (ex: com.br, com, co.uk).")
    parser.add_argument("--amazon-out", type=str, default=None, help="Pasta de destino do download da Amazon.")
    parser.add_argument("--limit", type=int, default=None, help="Limita quantidade de itens no download da Amazon (teste).")
    parser.add_argument("--source", type=str, help="Pasta local ou arquivo .zip baixado do Amazon Photos.")
    parser.add_argument(
        "--album",
        type=str,
        default=DEFAULT_ALBUM_NAME,
        help=f"Nome do álbum de destino no Google Fotos (padrão: '{DEFAULT_ALBUM_NAME}').",
    )
    parser.add_argument("--dry-run", action="store_true", help="Apenas simula e conta os arquivos sem enviar/baixar.")
    parser.add_argument("--retry-failed", action="store_true", help="Tenta reenviar itens que falharam antes.")
    parser.add_argument("--workers", type=int, default=DEFAULT_UPLOAD_WORKERS, help="Operações paralelas (1-16).")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host do servidor web local.")
    parser.add_argument("--port", type=int, default=8765, help="Porta do servidor web local.")
    parser.add_argument("--no-browser", action="store_true", help="Não abre o navegador automaticamente.")

    args = parser.parse_args()

    if args.download_amazon:
        return run_amazon_download_cli(args)

    if args.cli:
        if not args.source:
            parser.error("No modo --cli é obrigatório informar --source <pasta_ou_zip>.")
        return run_cli(args)

    url = f"http://{args.host}:{args.port}"
    print(f"Iniciando interface web em {url}")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run("app.server:app", host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
