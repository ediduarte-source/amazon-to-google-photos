"""Testes para o downloader em massa do Amazon Photos."""

import hashlib
from pathlib import Path

import httpx

from app.amazon_downloader import (
    AmazonLibraryDownloader,
    detect_amazon_domain,
    normalize_cookies,
)


def test_detect_amazon_domain_and_normalize_cookies() -> None:
    br_cookies = normalize_cookies(
        session_id="123-456",
        ubid="789-012",
        at_token="Atza|BRTOKEN",
        domain="com.br",
    )
    assert br_cookies["ubid-acbbr"] == "789-012"
    assert br_cookies["at-acbbr"] == "Atza|BRTOKEN"
    assert detect_amazon_domain(br_cookies) == "com.br"

    us_cookies = normalize_cookies(
        cookies_json='{"session-id": "1", "ubid-main": "2", "at-main": "3"}'
    )
    assert detect_amazon_domain(us_cookies) == "com"

    uk_cookies = normalize_cookies(
        cookies_json='{"session-id": "1", "ubid-acbuk": "2", "at-acbuk": "3"}'
    )
    assert detect_amazon_domain(uk_cookies) == "co.uk"


def test_bulk_download_deduplication_md5_and_resume(tmp_path: Path) -> None:
    photo_1 = b"foto-ferias-bytes-1"
    photo_2 = b"video-familia-bytes-2"
    md5_1 = hashlib.md5(photo_1).hexdigest()
    md5_2 = hashlib.md5(photo_2).hexdigest()

    download_hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/drive/v1/nodes"):
            return httpx.Response(200, json={"data": [{"id": "root", "ownerId": "OWNER1"}]})
        if path.endswith("/drive/v1/search"):
            return httpx.Response(
                200,
                json={
                    "count": 3,
                    "data": [
                        {
                            "id": "n1",
                            "name": "IMG_001.jpg",
                            "createdDate": "2024-01-01T10:00:00Z",
                            "contentProperties": {"md5": md5_1, "size": len(photo_1)},
                        },
                        # Duplicata exata (mesmo MD5) -> deve ser ignorada
                        {
                            "id": "n1-dup",
                            "name": "IMG_001_copy.jpg",
                            "createdDate": "2024-01-02T10:00:00Z",
                            "contentProperties": {"md5": md5_1, "size": len(photo_1)},
                        },
                        # Mesmo nome, conteúdo diferente -> deve ganhar sufixo de hash
                        {
                            "id": "n2",
                            "name": "IMG_001.jpg",
                            "createdDate": "2024-01-03T10:00:00Z",
                            "contentProperties": {"md5": md5_2, "size": len(photo_2)},
                        },
                    ],
                },
            )
        if "/nodes/n1/contentRedirection" in path:
            download_hits.append("n1")
            return httpx.Response(200, content=photo_1)
        if "/nodes/n2/contentRedirection" in path:
            download_hits.append("n2")
            return httpx.Response(200, content=photo_2)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    downloader = AmazonLibraryDownloader(
        default_output_dir=tmp_path / "amazon_out",
        client_factory=lambda cookies, workers: httpx.Client(transport=transport),
    )

    cookies = normalize_cookies(
        session_id="111", ubid="222", at_token="333", domain="com.br"
    )

    # 1ª execução: baixa os 2 arquivos únicos
    downloader.start(cookies, domain="com.br", output_dir=tmp_path / "amazon_out", workers=2)
    downloader.wait(timeout=10)

    st = downloader.get_status()
    assert st["state"] == "completed"
    assert st["total_found"] == 2
    assert st["downloaded"] == 2
    assert st["failed"] == 0
    files = sorted(p.name for p in (tmp_path / "amazon_out").iterdir())
    assert files == ["IMG_001.jpg", f"IMG_001_{md5_2[:8]}.jpg"]

    # 2ª execução (retomada): pula os 2 já baixados
    download_hits.clear()
    downloader.start(cookies, domain="com.br", output_dir=tmp_path / "amazon_out", workers=2)
    downloader.wait(timeout=10)

    st2 = downloader.get_status()
    assert st2["state"] == "completed"
    assert st2["already_downloaded"] == 2
    assert st2["to_download"] == 0
    assert download_hits == []
