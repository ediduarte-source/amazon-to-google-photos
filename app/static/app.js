const $ = (id) => document.getElementById(id);

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = bytes;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(idx >= 2 ? 1 : 0)} ${units[idx]}`;
}

async function copyText(text, feedbackEl, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    if (feedbackEl) feedbackEl.textContent = okMsg;
  } catch {
    const tmp = document.createElement("textarea");
    tmp.value = text;
    document.body.appendChild(tmp);
    tmp.select();
    document.execCommand("copy");
    document.body.removeChild(tmp);
    if (feedbackEl) feedbackEl.textContent = okMsg;
  }
}

document.querySelectorAll("[data-copy]").forEach((btn) => {
  btn.addEventListener("click", () => {
    copyText(
      btn.getAttribute("data-copy"),
      $("folder-message"),
      "Link copiado! Cole em uma nova aba do navegador."
    );
  });
});

// ---- Cookies manuais (Avançado) ----
const DOMAIN_LABELS = {
  com: ["ubid-main", "at-main"],
  "com.br": ["ubid-acbbr", "at-acbbr"],
  "co.uk": ["ubid-acbuk", "at-acbuk"],
  de: ["ubid-acbde", "at-acbde"],
  es: ["ubid-acbes", "at-acbes"],
  fr: ["ubid-acbfr", "at-acbfr"],
  it: ["ubid-acbit", "at-acbit"],
  ca: ["ubid-acbca", "at-acbca"],
};

let uploadedCookiesJson = null;

$("amazon-domain").addEventListener("change", () => {
  const [ubidName, atName] = DOMAIN_LABELS[$("amazon-domain").value] || ["ubid-acbbr", "at-acbbr"];
  $("label-ubid").textContent = ubidName;
  $("label-at").textContent = atName;
});

$("amazon-cookies-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  uploadedCookiesJson = await file.text();
  $("amazon-message").textContent = `Arquivo '${file.name}' carregado! Clique em 'Contar Biblioteca' ou 'Baixar Todas as Fotos e Vídeos'.`;
});

function hasManualCookies() {
  return Boolean(
    uploadedCookiesJson ||
      $("amazon-session-id").value.trim() ||
      $("amazon-ubid").value.trim() ||
      $("amazon-at").value.trim()
  );
}

// ---- Login Amazon com e-mail e senha ----
let amazonLoggedIn = false;

async function amazonLogin() {
  const email = $("amazon-email").value.trim();
  const password = $("amazon-password").value;
  if (!email || !password) {
    $("amazon-login-message").textContent = "Informe o e-mail e a senha da sua conta Amazon.";
    return;
  }
  $("btn-amazon-login").disabled = true;
  $("amazon-login-message").textContent = "Entrando na Amazon...";
  try {
    const resp = await fetch("/api/amazon/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        domain: $("amazon-domain").value,
        show_browser: $("amazon-show-browser").checked,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      $("amazon-login-message").textContent = `Erro: ${data.detail}`;
      $("btn-amazon-login").disabled = false;
      return;
    }
    await refreshAmazonLogin();
  } catch (err) {
    $("amazon-login-message").textContent = `Erro de conexão com o servidor local: ${err.message || err}`;
    $("btn-amazon-login").disabled = false;
  }
}

$("btn-amazon-login").addEventListener("click", amazonLogin);
$("amazon-password").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") amazonLogin();
});

async function submitAmazonOtp() {
  const code = $("amazon-otp").value.trim();
  if (!code) return;
  const resp = await fetch("/api/amazon/login/otp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  const data = await resp.json();
  $("amazon-login-message").textContent = resp.ok ? "Código enviado. Verificando..." : `Erro: ${data.detail}`;
  $("amazon-otp").value = "";
  await refreshAmazonLogin();
}

$("btn-amazon-otp").addEventListener("click", submitAmazonOtp);
$("amazon-otp").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") submitAmazonOtp();
});

$("btn-amazon-logout").addEventListener("click", async () => {
  await fetch("/api/amazon/logout", { method: "POST" });
  await refreshAmazonLogin();
});

async function refreshAmazonLogin() {
  const resp = await fetch("/api/amazon/login/status");
  const data = await resp.json();
  amazonLoggedIn = data.logged_in;

  $("amazon-login-box").classList.toggle("hidden", data.logged_in);
  $("amazon-session-box").classList.toggle("hidden", !data.logged_in);
  $("amazon-otp-box").classList.toggle("hidden", data.state !== "need_otp");
  $("btn-amazon-login").disabled = data.in_progress;
  if (data.logged_in) {
    $("amazon-session-email").textContent = `${data.email || "sua conta"} (amazon.${data.domain})`;
    $("amazon-password").value = "";
  }
  $("amazon-login-message").textContent =
    data.state === "idle" || data.logged_in ? "" : data.message || "";
}

// ---- Download em massa da Amazon ----
async function startAmazonDownload(dryRun) {
  if (!amazonLoggedIn && !hasManualCookies()) {
    $("amazon-message").textContent = "Entre primeiro com o e-mail e a senha da sua conta Amazon.";
    $("amazon-email").focus();
    return;
  }
  const outputDir = $("amazon-output-dir").value.trim() || null;
  const payload = {
    domain: $("amazon-domain").value,
    session_id: $("amazon-session-id").value.trim() || null,
    ubid: $("amazon-ubid").value.trim() || null,
    at_token: $("amazon-at").value.trim() || null,
    cookies_json: uploadedCookiesJson,
    output_dir: outputDir,
    dry_run: dryRun,
  };
  $("amazon-message").textContent = dryRun
    ? "Conectando ao Amazon Photos para contar sua biblioteca..."
    : "Iniciando download de todas as fotos e vídeos do Amazon Photos...";

  const resp = await fetch("/api/amazon/download/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    $("amazon-message").textContent = `Erro: ${data.detail}`;
    return;
  }
  await refreshAmazonStatus();
}

$("btn-amazon-preview").addEventListener("click", () => startAmazonDownload(true));
$("btn-amazon-download").addEventListener("click", () => startAmazonDownload(false));
$("btn-amazon-stop").addEventListener("click", async () => {
  await fetch("/api/amazon/download/stop", { method: "POST" });
  $("amazon-message").textContent = "Pausa solicitada...";
  await refreshAmazonStatus();
});

async function refreshAmazonStatus() {
  const resp = await fetch("/api/amazon/download/status");
  const data = await resp.json();

  const stateMap = {
    idle: ["Pronto", "badge-idle"],
    listing: ["Listando...", "badge-run"],
    downloading: ["Baixando...", "badge-run"],
    paused: ["Pausado", "badge-warn"],
    completed: ["Concluído", "badge-ok"],
    error: ["Erro", "badge-err"],
  };
  const [label, cls] = stateMap[data.state] || [data.state, "badge-idle"];
  const badge = $("amazon-badge");
  badge.textContent = label;
  badge.className = `badge ${cls}`;

  const active = data.state === "listing" || data.state === "downloading";
  $("btn-amazon-preview").disabled = active;
  $("btn-amazon-download").disabled = active;
  $("btn-amazon-stop").classList.toggle("hidden", !active);

  if (data.output_dir) {
    $("downloaded-folder-path").value = data.output_dir;
  }

  if (data.state !== "idle") {
    $("amazon-progress-wrap").classList.remove("hidden");
    $("amz-found").textContent = data.total_found || 0;
    $("amz-done").textContent = (data.already_downloaded || 0) + (data.downloaded || 0);
    $("amz-failed").textContent = data.failed || 0;
    $("amz-size").textContent = formatBytes(data.total_bytes || 0);

    const totalTodo = data.to_download || 0;
    const pct = totalTodo
      ? Math.round(((data.downloaded || 0) / totalTodo) * 100)
      : data.state === "completed"
        ? 100
        : 0;
    $("amz-progress-bar").style.width = `${pct}%`;
    $("amz-progress-text").textContent = `${pct}%`;
    $("amz-runtime-msg").textContent = data.message || "";

    const logBox = $("amz-log-box");
    if (data.logs && data.logs.length) {
      logBox.textContent = data.logs.join("\n");
      logBox.scrollTop = logBox.scrollHeight;
    }
  }
}

// ---- Passo 2: Pasta baixada -> Google Fotos ----
$("btn-copy-folder").addEventListener("click", () => {
  const path = $("downloaded-folder-path").value;
  copyText(path, $("folder-message"), `Caminho copiado: ${path}`);
});

$("btn-open-folder").addEventListener("click", async () => {
  const resp = await fetch("/api/amazon/open-folder", { method: "POST" });
  const data = await resp.json();
  $("folder-message").textContent = data.message || "";
});

refreshAmazonLogin();
refreshAmazonStatus();
setInterval(() => {
  refreshAmazonLogin();
  refreshAmazonStatus();
}, 2000);
