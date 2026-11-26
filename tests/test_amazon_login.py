"""Testes do login no Amazon Photos com e-mail e senha."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from fastapi.testclient import TestClient

from app.amazon_login import (
    EMAIL_SEL,
    AmazonLoginManager,
    cookies_for_domain,
    extract_amazon_domain,
    mask_email,
    playwright_browser_factory,
)
from app.server import create_app

# --------------------------------------------------------------------------
# Página/contexto falsos que simulam o fluxo de login da Amazon
# --------------------------------------------------------------------------


class FakeElement:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def is_visible(self) -> bool:
        return True

    def inner_text(self) -> str:
        return self.page.error_text

    def click(self) -> None:
        self.page.on_click(self.selector)

    def check(self) -> None:
        self.page.checked.add(self.selector)


class FakeContext:
    def __init__(self) -> None:
        self._cookies: list[dict] = []
        self.saved_state: str | None = None

    def cookies(self) -> list[dict]:
        return list(self._cookies)

    def add(self, name: str, value: str, domain: str = ".amazon.com.br") -> None:
        self._cookies.append({"name": name, "value": value, "domain": domain})

    def storage_state(self, path: str) -> None:
        Path(path).write_text("{}", encoding="utf-8")
        self.saved_state = path


class FakePage:
    """Máquina de estados: email -> password -> (otp|captcha|error) -> logado."""

    def __init__(self, context: FakeContext, *, after_password: str = "logged_in", valid_otp: str = "123456") -> None:
        self.context = context
        self.screen = "start"
        self.url = ""
        self.after_password = after_password
        self.valid_otp = valid_otp
        self.filled: dict[str, str] = {}
        self.checked: set[str] = set()
        self.error_text = ""

    # API usada pelo AmazonLoginManager
    def goto(self, url: str) -> None:
        self.url = "https://www.amazon.com.br/ap/signin"
        self.screen = "email"

    def wait_for_load_state(self, *_a, **_k) -> None:
        pass

    def query_selector(self, selector: str):
        visible = {
            "email": ["input[name='email']"],
            "password": ["input[name='password']", "#signInSubmit", "input[name='rememberMe']"],
            "otp": ["input#auth-mfa-otpcode", "#auth-signin-button"],
            "captcha": ["#auth-captcha-image"],
            "error": ["#auth-error-message-box"],
            "approval": ["#resend-approval-link"],
        }.get(self.screen, [])
        if self.screen == "email":
            visible.append("input#continue")
        for part in selector.split(","):
            part = part.strip()
            if any(part.startswith(v) for v in visible):
                return FakeElement(self, part)
        return None

    def fill(self, selector: str, value: str) -> None:
        self.filled[selector] = value

    def press(self, selector: str, key: str) -> None:
        self.on_click(selector)

    def _login(self) -> None:
        self.context.add("session-id", "111-2222222-3333333")
        self.context.add("ubid-acbbr", "444-5555555-6666666")
        self.context.add("at-acbbr", "Atza|token")
        self.url = "https://www.amazon.com.br/photos/all"
        self.screen = "photos"

    def on_click(self, selector: str) -> None:
        if self.screen == "email":
            self.screen = "password"
        elif self.screen == "password":
            if self.filled.get("input[name='password']") != "senha-certa":
                self.screen = "error"
                self.error_text = "Sua senha está incorreta"
            elif self.after_password == "logged_in":
                self._login()
            else:
                self.screen = self.after_password
        elif self.screen == "otp":
            code = next((v for k, v in self.filled.items() if "otp" in k.lower() or "code" in k.lower()), "")
            if code == self.valid_otp:
                self._login()


def make_factory(**page_kwargs):
    calls: list[bool] = []
    holder: dict = {}

    def factory(headless: bool, _state):
        calls.append(headless)
        ctx = FakeContext()
        kwargs = dict(page_kwargs)
        # Na janela visível, o usuário resolve o captcha sozinho -> simula conclusão
        if not headless and kwargs.get("after_password") == "captcha":
            kwargs["after_password"] = "logged_in"
        page = FakePage(ctx, **kwargs)
        holder["page"] = page
        return ctx, page, lambda: None

    return factory, calls, holder


def make_manager(tmp_path: Path, factory) -> AmazonLoginManager:
    return AmazonLoginManager(
        session_path=tmp_path / "amazon_session.json",
        browser_state_path=tmp_path / "browser_state.json",
        browser_factory=factory,
        timeout_seconds=10,
        poll_interval=0.01,
    )


# --------------------------------------------------------------------------
# Testes unitários
# --------------------------------------------------------------------------


def test_helpers() -> None:
    assert mask_email("eduardo@gmail.com") == "ed***@gmail.com"
    assert extract_amazon_domain("https://www.amazon.com/photos/all", "com.br") == "com"
    assert extract_amazon_domain("https://www.amazon.co.uk/ap/signin", "com") == "co.uk"
    assert extract_amazon_domain("http://127.0.0.1:8000/photos", "com.br") == "com.br"
    raw = [
        {"name": "session-id", "value": "1", "domain": ".amazon.com.br"},
        {"name": "at-acbbr", "value": "2", "domain": "www.amazon.com.br"},
        {"name": "other", "value": "3", "domain": ".google.com"},
    ]
    assert cookies_for_domain(raw, "com.br") == {"session-id": "1", "at-acbbr": "2"}


def test_login_success_saves_session_without_password(tmp_path: Path) -> None:
    factory, calls, holder = make_factory()
    mgr = make_manager(tmp_path, factory)
    mgr.start("eduardo@gmail.com", "senha-certa", domain="com.br")
    mgr.wait(5)

    st = mgr.get_status()
    assert st["state"] == "logged_in", st
    assert calls == [True]  # navegador invisível
    assert holder["page"].filled[EMAIL_SEL] == "eduardo@gmail.com"
    assert "input[name='rememberMe']" in holder["page"].checked

    cookies, domain = mgr.get_session()
    assert domain == "com.br"
    assert cookies["at-acbbr"] == "Atza|token"
    saved = (tmp_path / "amazon_session.json").read_text(encoding="utf-8")
    assert "senha-certa" not in saved
    assert "eduardo@gmail.com" not in saved  # e-mail mascarado

    # Nova instância reaproveita a sessão salva
    again = make_manager(tmp_path, factory)
    assert again.get_status()["logged_in"] is True

    again.logout()
    assert again.get_session() is None
    assert again.get_status()["logged_in"] is False


def test_wrong_password_reports_error(tmp_path: Path) -> None:
    factory, _, _ = make_factory()
    mgr = make_manager(tmp_path, factory)
    mgr.start("eduardo@gmail.com", "senha-errada")
    mgr.wait(5)
    st = mgr.get_status()
    assert st["state"] == "error"
    assert "senha está incorreta" in st["message"]
    assert mgr.get_session() is None


def test_two_factor_code_flow(tmp_path: Path) -> None:
    factory, _, _ = make_factory(after_password="otp")
    mgr = make_manager(tmp_path, factory)
    mgr.start("eduardo@gmail.com", "senha-certa")

    for _ in range(200):
        if mgr.get_status()["state"] == "need_otp":
            break
        time.sleep(0.02)
    assert mgr.get_status()["state"] == "need_otp"
    mgr.submit_otp(" 123 456 ")
    mgr.wait(5)
    assert mgr.get_status()["state"] == "logged_in"


def test_captcha_opens_visible_browser_for_user(tmp_path: Path) -> None:
    factory, calls, _ = make_factory(after_password="captcha")
    mgr = make_manager(tmp_path, factory)
    mgr.start("eduardo@gmail.com", "senha-certa")
    mgr.wait(5)
    assert calls == [True, False]  # tentou invisível, depois abriu janela para o usuário
    assert mgr.get_status()["state"] == "logged_in"
    assert mgr.get_status()["visible_browser"] is True


def test_server_uses_saved_session_for_download(tmp_path: Path, monkeypatch) -> None:
    factory, _, _ = make_factory()
    mgr = make_manager(tmp_path, factory)
    app = create_app(
        db_path=tmp_path / "state.db",
        credentials_path=tmp_path / "credentials.json",
        token_path=tmp_path / "token.json",
        extract_dir=tmp_path / "extracted",
        uploads_dir=tmp_path / "uploads",
        amazon_download_dir=tmp_path / "amz",
        amazon_login=mgr,
    )
    client = TestClient(app)

    # Sem login -> download bloqueado com mensagem clara
    resp = client.post("/api/amazon/download/start", json={"dry_run": True})
    assert resp.status_code == 400
    assert "e-mail e senha" in resp.json()["detail"]

    # Login via API
    assert client.post("/api/amazon/login", json={"email": "a@b.com", "password": "senha-certa"}).status_code == 200
    mgr.wait(5)
    status = client.get("/api/amazon/login/status").json()
    assert status["logged_in"] is True

    captured: dict = {}

    from app.amazon_downloader import AmazonLibraryDownloader

    def fake_start(self, cookies, **kwargs):
        captured["cookies"] = cookies
        captured["domain"] = kwargs.get("domain")

    monkeypatch.setattr(AmazonLibraryDownloader, "start", fake_start)
    resp = client.post("/api/amazon/download/start", json={"dry_run": True})
    assert resp.status_code == 200, resp.text
    assert captured["cookies"]["at-acbbr"] == "Atza|token"
    assert captured["domain"] == "com.br"

    assert client.post("/api/amazon/logout").json()["status"]["logged_in"] is False


# --------------------------------------------------------------------------
# Teste ponta a ponta com navegador real (Chromium) e página de login local
# --------------------------------------------------------------------------

SIGNIN_HTML = """<html><body>
<form method="post" action="/ap/signin">
  <input type="email" name="email" id="ap_email"/>
  <input type="submit" id="continue" value="Continuar"/>
</form></body></html>"""

PASSWORD_HTML = """<html><body>
<form method="post" action="/ap/password">
  <input type="hidden" name="email" value="{email}"/>
  <input type="password" name="password" id="ap_password"/>
  <input type="checkbox" name="rememberMe"/>
  <input type="submit" id="signInSubmit" value="Fazer login"/>
</form></body></html>"""

OTP_HTML = """<html><body>
<form method="post" action="/ap/mfa">
  <input type="text" name="otpCode" id="auth-mfa-otpcode"/>
  <input type="checkbox" name="rememberDevice" id="auth-mfa-remember-device"/>
  <input type="submit" id="auth-signin-button" value="Entrar"/>
</form></body></html>"""


class FakeAmazonHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silencia o servidor
        pass

    def _html(self, body: str, cookies: list[str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body.encode())

    def _redirect(self, to: str, cookies: list[str] | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/photos"):
            if "at-acbbr=" in (self.headers.get("Cookie") or ""):
                return self._html("<html><body><h1>Amazon Photos</h1></body></html>")
            return self._redirect("/ap/signin", ["session-id=111-2222222-3333333; Path=/"])
        if self.path.startswith("/ap/signin"):
            return self._html(SIGNIN_HTML)
        if self.path.startswith("/ap/mfa"):
            return self._html(OTP_HTML)
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if self.path == "/ap/signin":
            return self._html(PASSWORD_HTML.format(email=form.get("email", "")))
        if self.path == "/ap/password":
            if form.get("password") != "senha-certa" or "rememberMe" not in form:
                return self._html('<div id="auth-error-message-box">Senha incorreta</div>')
            return self._redirect("/ap/mfa")
        if self.path == "/ap/mfa":
            if form.get("otpCode") == "654321":
                return self._redirect(
                    "/photos/all",
                    ["at-acbbr=Atza|real; Path=/", "ubid-acbbr=444-5555555-6666666; Path=/"],
                )
            return self._html('<div id="auth-error-message-box">Código inválido</div>')

    def do_HEAD(self) -> None:  # pragma: no cover
        self.do_GET()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _chromium_available(), reason="Chromium do Playwright não instalado")
def test_end_to_end_real_browser(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAmazonHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        mgr = AmazonLoginManager(
            session_path=tmp_path / "amazon_session.json",
            browser_state_path=tmp_path / "browser_state.json",
            browser_factory=playwright_browser_factory,
            base_url_template=f"http://127.0.0.1:{port}",
            timeout_seconds=30,
            poll_interval=0.2,
        )
        mgr.start("eduardo@gmail.com", "senha-certa", domain="com.br")
        for _ in range(150):
            if mgr.get_status()["state"] in ("need_otp", "error"):
                break
            time.sleep(0.1)
        assert mgr.get_status()["state"] == "need_otp", mgr.get_status()
        mgr.submit_otp("654321")
        mgr.wait(30)

        st = mgr.get_status()
        assert st["state"] == "logged_in", st
        cookies, _ = mgr.get_session()
        assert cookies["at-acbbr"] == "Atza|real"
        assert cookies["session-id"] == "111-2222222-3333333"
        assert json.loads((tmp_path / "amazon_session.json").read_text())["email"] == "ed***@gmail.com"
    finally:
        server.shutdown()
