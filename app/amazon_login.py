"""Login no Amazon Photos com e-mail e senha usando um navegador automatizado (Playwright).

Fluxo:
1. Abre o Amazon Photos em um navegador Chromium (invisível por padrão).
2. Preenche e-mail e senha e envia o formulário oficial de login da Amazon.
3. Se a Amazon pedir verificação em duas etapas (código OTP/SMS/e-mail), o status
   passa para ``need_otp`` e aguarda o usuário informar o código.
4. Se a Amazon pedir aprovação pelo celular, aguarda o usuário aprovar.
5. Se a Amazon exibir um CAPTCHA/verificação de segurança, a aplicação NÃO tenta
   resolvê-lo: abre uma janela visível do navegador para o próprio usuário concluir.
6. Ao concluir, salva os cookies da sessão em ``data/amazon_session.json`` para que o
   download funcione sem precisar digitar a senha novamente. A senha nunca é salva.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import DATA_DIR

DEFAULT_SESSION_PATH = DATA_DIR / "amazon_session.json"
DEFAULT_BROWSER_STATE_PATH = DATA_DIR / "amazon_browser_state.json"

LOGIN_TIMEOUT_SECONDS = 300
MAX_PASSWORD_SUBMITS = 3
MAX_OTP_ATTEMPTS = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Seletores do formulário oficial de login da Amazon (/ap/signin e /ap/cvf)
EMAIL_SEL = "input[name='email']:not([type='hidden'])"
CONTINUE_SEL = "input#continue, #continue"
PASSWORD_SEL = "input[name='password']"
REMEMBER_SEL = "input[name='rememberMe']"
SIGNIN_SUBMIT_SEL = "#signInSubmit, input#signInSubmit"
SIGNIN_LINK_SEL = "a[href*='/ap/signin']"
ERROR_SEL = "#auth-error-message-box, .a-alert-error"
OTP_SEL = "input#auth-mfa-otpcode, input[name='otpCode'], input#cvf-input-code, input[name='code']"
OTP_REMEMBER_SEL = "input#auth-mfa-remember-device, input[name='rememberDevice']"
OTP_SUBMIT_SEL = (
    "#auth-signin-button, #cvf-submit-otp-button input, #cvf-submit-otp-button, "
    "input[type='submit'], button[type='submit']"
)
APPROVAL_SEL = "#resend-approval-link, .transaction-approval-word, #channelDetailsForApproval"
CAPTCHA_SEL = (
    "#auth-captcha-image, #captchacharacters, input[name='guess'], "
    "form[action*='validateCaptcha'], iframe[src*='arkoselabs'], "
    "#cvf-aamation-challenge-iframe, #aa-challenge-whole-page-iframe"
)

AUTH_COOKIE_PREFIXES = ("at-acb",)
AUTH_COOKIE_NAMES = ("at-main", "at_main")


class AmazonLoginError(RuntimeError):
    """Falha definitiva no login (senha incorreta, conta bloqueada, tempo esgotado...)."""


class NeedsVisibleBrowser(RuntimeError):
    """A Amazon exigiu uma verificação que o próprio usuário precisa concluir em uma janela visível."""


def _clean_domain(domain: str | None) -> str:
    value = (domain or "com.br").strip().lower()
    for prefix in ("https://", "http://", "www.", "amazon."):
        value = value.removeprefix(prefix)
    return value.strip("/") or "com.br"


def mask_email(email: str) -> str:
    user, at, host = email.partition("@")
    if not at:
        return email[:2] + "***"
    return f"{user[:2]}***@{host}"


def has_auth_cookie(cookies: dict[str, str]) -> bool:
    return "session-id" in cookies and any(
        name in AUTH_COOKIE_NAMES or name.startswith(AUTH_COOKIE_PREFIXES) for name in cookies
    )


def cookies_for_domain(raw_cookies: list[dict[str, Any]], domain: str) -> dict[str, str]:
    """Filtra os cookies do navegador que pertencem a amazon.<domain>."""
    host = f"amazon.{domain}"
    result: dict[str, str] = {}
    for c in raw_cookies:
        cookie_domain = str(c.get("domain", "")).lstrip(".")
        if cookie_domain == host or cookie_domain.endswith("." + host) or cookie_domain in ("localhost", "127.0.0.1"):
            result[c["name"]] = c["value"]
    return result


def _visible(page: Any, selector: str) -> Any | None:
    try:
        el = page.query_selector(selector)
    except Exception:
        return None
    if el is None:
        return None
    try:
        return el if el.is_visible() else None
    except Exception:
        return el


def _text(page: Any, selector: str) -> str:
    el = _visible(page, selector)
    if not el:
        return ""
    try:
        return " ".join((el.inner_text() or "").split())
    except Exception:
        return ""


def detect_page_state(page: Any, cookies: dict[str, str]) -> str:
    """Classifica a página atual do fluxo de login da Amazon."""
    url = page.url or ""
    if has_auth_cookie(cookies) and "/ap/" not in url:
        return "logged_in"
    if _visible(page, CAPTCHA_SEL) or "validateCaptcha" in url:
        return "captcha"
    if _visible(page, ERROR_SEL):
        return "error"
    if _visible(page, OTP_SEL):
        return "otp"
    if _visible(page, APPROVAL_SEL) or "/ap/cvf/approval" in url:
        return "approval"
    if _visible(page, PASSWORD_SEL):
        return "password"
    if _visible(page, EMAIL_SEL):
        return "email"
    return "unknown"


@dataclass
class AmazonLoginStatus:
    # idle | logging_in | need_otp | need_approval | need_captcha | logged_in | error
    state: str = "idle"
    message: str = "Entre com seu e-mail e senha da Amazon."
    email: str | None = None
    domain: str = "com.br"
    visible_browser: bool = False
    logs: list[str] = field(default_factory=list)


BrowserFactory = Callable[[bool, Path | None], tuple[Any, Any, Callable[[], None]]]
"""Recebe (headless, storage_state_path) e retorna (context, page, close_fn)."""


def _ensure_chromium_installed() -> None:
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
        capture_output=True,
    )


def playwright_browser_factory(headless: bool, storage_state: Path | None) -> tuple[Any, Any, Callable[[], None]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise AmazonLoginError(
            "Dependência 'playwright' não instalada. Rode: pip install -r requirements.txt"
        ) from exc

    pw = sync_playwright().start()
    launch_args = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    try:
        try:
            browser = pw.chromium.launch(**launch_args)
        except Exception as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            _ensure_chromium_installed()
            browser = pw.chromium.launch(**launch_args)
    except Exception as exc:
        pw.stop()
        if not headless:
            raise AmazonLoginError(
                "Não foi possível abrir uma janela do navegador neste computador "
                f"(é necessário um ambiente com tela). Detalhe: {exc}"
            ) from exc
        raise AmazonLoginError(f"Não foi possível iniciar o navegador: {exc}") from exc

    context_kwargs: dict[str, Any] = {
        "user_agent": USER_AGENT,
        "locale": "pt-BR",
        "viewport": {"width": 1280, "height": 900},
    }
    if storage_state and storage_state.is_file():
        context_kwargs["storage_state"] = str(storage_state)
    context = browser.new_context(**context_kwargs)
    page = context.new_page()

    def _close() -> None:
        try:
            browser.close()
        finally:
            pw.stop()

    return context, page, _close


class AmazonLoginManager:
    """Gerencia o login com e-mail/senha e a sessão salva do Amazon Photos."""

    def __init__(
        self,
        *,
        session_path: Path = DEFAULT_SESSION_PATH,
        browser_state_path: Path = DEFAULT_BROWSER_STATE_PATH,
        browser_factory: BrowserFactory | None = None,
        base_url_template: str = "https://www.amazon.{domain}",
        timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
        poll_interval: float = 1.0,
    ) -> None:
        self.session_path = Path(session_path)
        self.browser_state_path = Path(browser_state_path)
        self._browser_factory = browser_factory or playwright_browser_factory
        self._base_url_template = base_url_template
        self._timeout = timeout_seconds
        self._poll = poll_interval
        self._lock = threading.RLock()
        self._otp_queue: queue.Queue[str] = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = AmazonLoginStatus()
        saved = self._read_session()
        if saved:
            self.status.state = "logged_in"
            self.status.email = saved.get("email")
            self.status.domain = saved.get("domain", "com.br")
            self.status.message = f"Conectado à Amazon como {saved.get('email') or 'sua conta'}."

    # ---------- sessão salva ----------
    def _read_session(self) -> dict[str, Any] | None:
        if not self.session_path.is_file():
            return None
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not has_auth_cookie(data.get("cookies") or {}):
            return None
        return data

    def _write_session(self, email: str, domain: str, cookies: dict[str, str]) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "email": mask_email(email),
            "domain": domain,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cookies": cookies,
        }
        tmp = self.session_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.session_path)

    def has_session(self) -> bool:
        return self._read_session() is not None

    def get_session(self) -> tuple[dict[str, str], str] | None:
        """Retorna (cookies, domínio) da sessão salva, ou None se não houver login."""
        data = self._read_session()
        if not data:
            return None
        return dict(data["cookies"]), data.get("domain", "com.br")

    def logout(self) -> None:
        self.cancel()
        self.session_path.unlink(missing_ok=True)
        self.browser_state_path.unlink(missing_ok=True)
        with self._lock:
            self.status = AmazonLoginStatus(message="Você saiu da conta Amazon.")

    # ---------- status ----------
    def _set(self, state: str | None = None, message: str | None = None) -> None:
        with self._lock:
            if state:
                self.status.state = state
            if message:
                self.status.message = message
                self.status.logs.append(f"[{time.strftime('%H:%M:%S')}] {message}")
                self.status.logs = self.status.logs[-50:]

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.status.state,
                "message": self.status.message,
                "email": self.status.email,
                "domain": self.status.domain,
                "visible_browser": self.status.visible_browser,
                "logged_in": self.status.state == "logged_in",
                "in_progress": self.is_running(),
                "logs": list(self.status.logs),
            }

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def cancel(self) -> None:
        self._cancel.set()
        if self.is_running():
            self.wait(10)

    def submit_otp(self, code: str) -> None:
        code = "".join(code.split())
        if not code:
            raise ValueError("Informe o código de verificação.")
        if self.status.state != "need_otp":
            raise RuntimeError("A Amazon não está aguardando um código de verificação no momento.")
        self._otp_queue.put(code)
        self._set("logging_in", "Código enviado. Verificando...")

    # ---------- login ----------
    def start(self, email: str, password: str, *, domain: str = "com.br", show_browser: bool = False) -> None:
        email = (email or "").strip()
        if not email or not password:
            raise ValueError("Informe o e-mail e a senha da sua conta Amazon.")
        with self._lock:
            if self.is_running():
                raise RuntimeError("Já existe um login da Amazon em andamento.")
            clean = _clean_domain(domain)
            self._cancel.clear()
            while not self._otp_queue.empty():
                self._otp_queue.get_nowait()
            self.status = AmazonLoginStatus(
                state="logging_in",
                email=mask_email(email),
                domain=clean,
                visible_browser=show_browser,
            )
            self._set(message=f"Entrando na Amazon (amazon.{clean})...")
            self._thread = threading.Thread(
                target=self._run, args=(email, password, clean, show_browser), daemon=True
            )
            self._thread.start()

    def _run(self, email: str, password: str, domain: str, show_browser: bool) -> None:
        try:
            try:
                cookies = self._attempt(email, password, domain, headless=not show_browser)
            except NeedsVisibleBrowser:
                with self._lock:
                    self.status.visible_browser = True
                self._set(
                    "need_captcha",
                    "A Amazon pediu uma verificação de segurança. Abrindo uma janela do navegador "
                    "para você concluir (resolva a verificação na janela que abrir).",
                )
                cookies = self._attempt(email, password, domain, headless=False)
            self._write_session(email, domain, cookies)
            self._set("logged_in", f"Conectado à Amazon como {mask_email(email)}!")
        except AmazonLoginError as exc:
            self._set("error", str(exc))
        except Exception as exc:  # pragma: no cover - proteção geral
            self._set("error", f"Erro inesperado no login da Amazon: {exc}")

    def _attempt(self, email: str, password: str, domain: str, *, headless: bool) -> dict[str, str]:
        context, page, close = self._browser_factory(headless, self.browser_state_path)
        try:
            cookies = self._flow(context, page, email, password, domain, headless=headless)
            try:
                self.browser_state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(self.browser_state_path))
                os.chmod(self.browser_state_path, 0o600)
            except Exception:
                pass
            return cookies
        finally:
            try:
                close()
            except Exception:
                pass

    def _settle(self, page: Any) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

    def _sleep(self, seconds: float) -> None:
        if self._cancel.wait(seconds):
            raise AmazonLoginError("Login cancelado.")

    def _flow(
        self, context: Any, page: Any, email: str, password: str, domain: str, *, headless: bool
    ) -> dict[str, str]:
        base = self._base_url_template.format(domain=domain)
        page.goto(f"{base}/photos/all")
        self._settle(page)

        def current_cookies() -> dict[str, str]:
            return cookies_for_domain(context.cookies(), domain)

        if detect_page_state(page, current_cookies()) == "unknown":
            link = _visible(page, SIGNIN_LINK_SEL)
            if link:
                link.click()
                self._settle(page)

        deadline = time.time() + self._timeout
        password_submits = 0
        otp_attempts = 0
        email_submits = 0
        announced: str | None = None

        while time.time() < deadline:
            if self._cancel.is_set():
                raise AmazonLoginError("Login cancelado.")
            cookies = current_cookies()
            state = detect_page_state(page, cookies)

            if state == "logged_in":
                return cookies

            if state == "captcha":
                if headless:
                    raise NeedsVisibleBrowser()
                if announced != "captcha":
                    self._set(
                        "need_captcha",
                        "Conclua a verificação de segurança na janela do navegador que foi aberta.",
                    )
                    announced = "captcha"
                self._sleep(self._poll)
                continue

            if state == "error":
                msg = _text(page, ERROR_SEL) or "A Amazon recusou o login."
                raise AmazonLoginError(f"Login recusado pela Amazon: {msg}")

            if state == "email":
                email_submits += 1
                if email_submits > MAX_PASSWORD_SUBMITS:
                    raise AmazonLoginError("A Amazon não aceitou o e-mail informado.")
                page.fill(EMAIL_SEL, email)
                btn = _visible(page, CONTINUE_SEL)
                if btn:
                    btn.click()
                else:
                    page.press(EMAIL_SEL, "Enter")
                self._settle(page)
                announced = None
                continue

            if state == "password":
                password_submits += 1
                if password_submits > MAX_PASSWORD_SUBMITS:
                    raise AmazonLoginError("A Amazon não aceitou a senha. Confira e tente novamente.")
                if _visible(page, EMAIL_SEL):
                    page.fill(EMAIL_SEL, email)
                page.fill(PASSWORD_SEL, password)
                remember = _visible(page, REMEMBER_SEL)
                if remember:
                    try:
                        remember.check()
                    except Exception:
                        pass
                btn = _visible(page, SIGNIN_SUBMIT_SEL)
                if btn:
                    btn.click()
                else:
                    page.press(PASSWORD_SEL, "Enter")
                self._set("logging_in", "Senha enviada. Aguardando resposta da Amazon...")
                self._settle(page)
                announced = None
                continue

            if state == "otp":
                otp_attempts += 1
                if otp_attempts > MAX_OTP_ATTEMPTS:
                    raise AmazonLoginError("Código de verificação recusado várias vezes.")
                self._set(
                    "need_otp",
                    "A Amazon enviou um código de verificação (SMS, e-mail ou app autenticador). "
                    "Digite o código para continuar.",
                )
                code = self._wait_otp(deadline)
                page.fill(OTP_SEL, code)
                remember = _visible(page, OTP_REMEMBER_SEL)
                if remember:
                    try:
                        remember.check()
                    except Exception:
                        pass
                btn = _visible(page, OTP_SUBMIT_SEL)
                if btn:
                    btn.click()
                else:
                    page.press(OTP_SEL, "Enter")
                self._settle(page)
                announced = None
                continue

            if state == "approval":
                if announced != "approval":
                    self._set(
                        "need_approval",
                        "A Amazon enviou uma notificação de aprovação para seu celular/e-mail. "
                        "Aprove o acesso para continuar.",
                    )
                    announced = "approval"
                self._sleep(max(self._poll, 0.01) * 2)
                continue

            # Página intermediária (redirecionamentos, carregamento do Amazon Photos...)
            self._sleep(self._poll)

        raise AmazonLoginError("Tempo esgotado aguardando o login da Amazon. Tente novamente.")

    def _wait_otp(self, deadline: float) -> str:
        while time.time() < deadline:
            if self._cancel.is_set():
                raise AmazonLoginError("Login cancelado.")
            try:
                return self._otp_queue.get(timeout=0.5)
            except queue.Empty:
                continue
        raise AmazonLoginError("Tempo esgotado aguardando o código de verificação.")
