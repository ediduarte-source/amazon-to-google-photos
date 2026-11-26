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

function statusBadgeInfo(state) {
  const map = {
    idle: ["Pronto", "badge-idle"],
    scanning: ["Escaneando", "badge-run"],
    running: ["Migrando...", "badge-run"],
    paused: ["Pausado", "badge-warn"],
    completed: ["Concluído", "badge-ok"],
    error: ["Erro", "badge-err"],
  };
  return map[state] || [state, "badge-idle"];
}

function itemStatusLabel(status) {
  const labels = {
    pending: "Pendente",
    uploading: "Enviando...",
    uploaded: "Enviada",
    failed: "Falhou",
    skipped: "Ignorada",
  };
  return labels[status] || status;
}

async function refreshAuth() {
  const resp = await fetch("/api/auth/status");
  const data = await resp.json();

  const authBadge = $("auth-badge");
  const btnAuthUrl = $("btn-auth-url");
  const btnDisconnect = $("btn-disconnect");

  if (data.authenticated) {
    authBadge.textContent = "Conectado";
    authBadge.className = "badge badge-ok";
    btnAuthUrl.classList.add("hidden");
    btnDisconnect.classList.remove("hidden");
    $("oauth-code-box").classList.add("hidden");
  } else {
    authBadge.textContent = data.oauth_in_progress ? "Autorizando..." : "Desconectado";
    authBadge.className = `badge ${data.oauth_in_progress ? "badge-run" : "badge-warn"}`;
    btnAuthUrl.classList.remove("hidden");
    btnAuthUrl.disabled = !data.has_credentials;
    btnDisconnect.classList.add("hidden");
  }

  if (data.last_error) {
    $("auth-message").textContent = `Erro OAuth: ${data.last_error}`;
  }
}

async function refreshMigrationStatus() {
  const album = encodeURIComponent($("album-name").value.trim() || "Porta Retrato");
  const resp = await fetch(`/api/migrate/status?album_name=${album}`);
  const data = await resp.json();

  const [label, cls] = statusBadgeInfo(data.state);
  const badge = $("global-badge");
  badge.textContent = label;
  badge.className = `badge ${cls}`;

  const isRunning = data.state === "running" || data.state === "scanning";
  $("btn-start").disabled = isRunning;
  $("btn-scan").disabled = isRunning;
  $("btn-stop").classList.toggle("hidden", !isRunning);

  const s = data.summary || {};
  $("stat-total").textContent = s.total || 0;
  $("stat-uploaded").textContent = s.uploaded || 0;
  $("stat-pending").textContent = s.pending || 0;
  $("stat-failed").textContent = s.failed || 0;
  $("stat-size").textContent = formatBytes(s.total_bytes || 0);

  const pct = s.total ? Math.round(((s.uploaded || 0) / s.total) * 100) : 0;
  $("progress-bar").style.width = `${pct}%`;
  $("progress-text").textContent = `${pct}% concluído`;
  $("runtime-msg").textContent = data.message || "";

  const logBox = $("log-box");
  if (data.logs && data.logs.length) {
    logBox.textContent = data.logs.join("\n");
    logBox.scrollTop = logBox.scrollHeight;
  }

  const tbody = $("recent-tbody");
  if (!data.recent_items || !data.recent_items.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="muted">Nenhum arquivo listado ainda.</td></tr>';
  } else {
    tbody.innerHTML = data.recent_items
      .map((item) => {
        const titleAttr = item.error_message ? ` title="${item.error_message.replace(/"/g, "&quot;")}"` : "";
        return `
          <tr${titleAttr}>
            <td>${item.file_name}</td>
            <td>${formatBytes(item.file_size)}</td>
            <td>${itemStatusLabel(item.status)}</td>
          </tr>
        `;
      })
      .join("");
  }
}

$("credentials-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);

  const resp = await fetch("/api/auth/credentials", { method: "POST", body: form });
  const data = await resp.json();
  $("auth-message").textContent = resp.ok ? data.message : `Erro: ${data.detail}`;
  await refreshAuth();
});

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
      $("auth-message"),
      "Link copiado! Cole em uma nova aba do navegador."
    );
  });
});

$("btn-auth-url").addEventListener("click", async () => {
  const resp = await fetch("/api/auth/start-url", { method: "POST" });
  const data = await resp.json();
  if (!resp.ok) {
    $("auth-message").textContent = `Erro: ${data.detail}`;
    return;
  }
  $("oauth-url-display").value = data.auth_url;
  $("oauth-code-box").classList.remove("hidden");
  $("auth-message").textContent = "Clique em 'Copiar Link', abra em uma nova aba do navegador e cole a URL retornada.";
});

$("btn-copy-oauth").addEventListener("click", () => {
  const url = $("oauth-url-display").value;
  if (url) {
    copyText(url, $("auth-message"), "Link de autorização copiado! Abra em uma nova aba do navegador.");
  }
});

$("btn-exchange-code").addEventListener("click", async () => {
  const code_or_url = $("oauth-code-input").value.trim();
  if (!code_or_url) {
    $("auth-message").textContent = "Cole a URL ou o código de autorização retornado pelo Google.";
    return;
  }
  const resp = await fetch("/api/auth/exchange-code", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code_or_url }),
  });
  const data = await resp.json();
  $("auth-message").textContent = resp.ok ? data.message : `Erro: ${data.detail}`;
  if (resp.ok) {
    $("oauth-code-input").value = "";
  }
  await refreshAuth();
});

$("btn-disconnect").addEventListener("click", async () => {
  const resp = await fetch("/api/auth/disconnect", { method: "POST" });
  const data = await resp.json();
  $("auth-message").textContent = data.message;
  await refreshAuth();
});

async function handleMediaUpload(fileList) {
  if (!fileList || !fileList.length) return;
  const album = encodeURIComponent($("album-name").value.trim() || "Porta Retrato");
  const form = new FormData();
  for (const file of fileList) {
    form.append("files", file);
  }
  $("action-message").textContent = `Enviando ${fileList.length} arquivo(s) para análise...`;
  const resp = await fetch(`/api/upload-source?album_name=${album}`, {
    method: "POST",
    body: form,
  });
  const data = await resp.json();
  if (!resp.ok) {
    $("action-message").textContent = `Erro no envio: ${data.detail}`;
    return;
  }
  $("source-path").value = data.source_path;
  $("action-message").textContent =
    `Pronto! ${data.total_files_found} mídias detectadas (${formatBytes(data.total_bytes)}) — ${data.newly_queued} novas na fila.`;
  await refreshMigrationStatus();
}

const dropzone = $("dropzone");
$("media-upload-input").addEventListener("change", (ev) => handleMediaUpload(ev.target.files));
dropzone.addEventListener("dragover", (ev) => {
  ev.preventDefault();
  dropzone.classList.add("dragover");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (ev) => {
  ev.preventDefault();
  dropzone.classList.remove("dragover");
  handleMediaUpload(ev.dataTransfer.files);
});

$("btn-scan").addEventListener("click", async () => {
  const source_path = $("source-path").value.trim();
  const album_name = $("album-name").value.trim() || "Porta Retrato";
  if (!source_path) {
    $("action-message").textContent = "Arraste arquivos acima ou informe o caminho da pasta/.zip.";
    return;
  }
  $("action-message").textContent = "Escaneando arquivos...";
  const resp = await fetch("/api/scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_path, album_name }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    $("action-message").textContent = `Erro: ${data.detail}`;
    return;
  }
  $("action-message").textContent =
    `Prévia: ${data.total_files_found} mídias (${formatBytes(data.total_bytes)}) — ${data.newly_queued} novas na fila.`;
  await refreshMigrationStatus();
});

$("btn-start").addEventListener("click", async () => {
  const source_path = $("source-path").value.trim();
  const album_name = $("album-name").value.trim() || "Porta Retrato";
  const retry_failed = $("retry-failed").checked;
  if (!source_path) {
    $("action-message").textContent = "Arraste arquivos acima ou informe o caminho da pasta/.zip.";
    return;
  }
  const resp = await fetch("/api/migrate/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_path, album_name, dry_run: false, retry_failed }),
  });
  const data = await resp.json();
  $("action-message").textContent = resp.ok ? "Migração iniciada!" : `Erro: ${data.detail}`;
  await refreshMigrationStatus();
});

$("btn-stop").addEventListener("click", async () => {
  await fetch("/api/migrate/stop", { method: "POST" });
  $("action-message").textContent = "Pausa solicitada...";
  await refreshMigrationStatus();
});

refreshAuth();
refreshMigrationStatus();
setInterval(() => {
  refreshAuth();
  refreshMigrationStatus();
}, 2000);
