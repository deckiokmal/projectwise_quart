/* ==========================================================================
   ProjectWise — MCP Control (FINAL)
   - Menandai MCP aktif: window.__PW_HAS_MCP__ = true  → main.js skip fallback
   - UI getter (selalu ambil window.UI terbaru, hindari race)
   - Listen: ui:mcp-connect-click / ui:mcp-disconnect-click / ui:mcp-reconnect-click / ui:submit
   - Chat ke backend: POST /chat/message (JSON key: "response" secara default)
   - Mode respons: "json" | "chunk" | "sse"
   ========================================================================== */

// Beri tahu UI bahwa lapisan MCP aktif
window.__PW_HAS_MCP__ = true;

/** ====== KONFIG CHAT (ubah sesuai backend Anda) ====== */
const CHAT = {
  endpoint: "/chat/message",
  method: "POST",
  mode: "json",        // "json" | "chunk" | "sse"
  jsonKey: "response", // kunci field jawaban di JSON
  headers: {
    // contoh: "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]')?.content || ''
  },
};

/** ====== Endpoint MCP ====== */
const MCP = {
  status:     "/mcp/status",
  connect:    "/mcp/connect",
  disconnect: "/mcp/disconnect",
  reconnect:  "/mcp/reconnect",
};

/** ====== UI getter (hindari snapshot saat UI belum siap) ====== */
const __ui_fallback = {
  setMCPStatus: () => {},
  setTyping: () => {},
  appendAssistantMarkdown: () => {},
  appendAssistantChunk: () => {},
  toast: () => {},
};
const UI = () => (window.UI ?? __ui_fallback);

/** ====== HTTP helper ====== */
async function http(method, url, body, extraHeaders = {}) {
  const opt = { method, headers: { ...extraHeaders } };
  if (body && !(body instanceof FormData)) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  } else if (body) {
    opt.body = body; // FormData
  }
  const res = await fetch(url, opt);
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    const msg = data?.error || res.statusText || "HTTP error";
    throw new Error(msg);
  }
  return data ?? {};
}

/** ====== MCP Status Polling ====== */
let pollTimer = null;
let pollMs = 1500;
let isRefreshing = false;

function mapStatus(s) {
  if (s?.connecting) return "connecting";
  if (s?.connected)  return "connected";
  if (s?.error)      return "error";
  return "disconnected";
}
function labelFor(s, status) {
  if (status === "connected") {
    const model = s?.llm_model ? ` (${s.llm_model})` : "";
    return `Connected ✓${model}`;
  }
  if (status === "connecting") return "Connecting…";
  if (status === "error")      return s?.error ? `Error: ${s.error}` : "Error";
  return "Disconnected";
}
function renderServerState(s) {
  const status = mapStatus(s);
  UI().setMCPStatus(status, labelFor(s, status));
}
async function refreshStatus() {
  if (isRefreshing) return;
  isRefreshing = true;
  try {
    const s = await http("GET", MCP.status);
    renderServerState(s);
    const next = s?.connecting ? 500 : 1500;
    if (next !== pollMs) { pollMs = next; restartPolling(); }
  } catch (e) {
    UI().setMCPStatus("error", `Error: ${e?.message || e}`);
    if (pollMs !== 4000) { pollMs = 4000; restartPolling(); }
  } finally { isRefreshing = false; }
}
function startPolling() { if (!pollTimer) { pollTimer = setInterval(refreshStatus, pollMs); refreshStatus(); } }
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
function restartPolling() { stopPolling(); startPolling(); }

/** ====== Aksi MCP ====== */
async function onConnect() {
  UI().setMCPStatus("connecting", "Connecting…");
  try {
    await http("POST", MCP.connect, null, CHAT.headers);
    UI().toast("MCP connected.", "ok");
  } catch (e) {
    UI().setMCPStatus("error", `Error: ${e?.message || e}`);
    UI().toast(`Gagal connect: ${e?.message || e}`, "error");
  } finally { refreshStatus(); }
}
async function onDisconnect() {
  try {
    await http("POST", MCP.disconnect, null, CHAT.headers);
    UI().setMCPStatus("disconnected", "Disconnected");
    UI().toast("MCP disconnected.", "warn");
  } catch (e) {
    UI().setMCPStatus("error", `Error: ${e?.message || e}`);
    UI().toast(`Gagal disconnect: ${e?.message || e}`, "error");
  } finally { refreshStatus(); }
}
async function onReconnect() {
  UI().setMCPStatus("connecting", "Connecting…");
  try {
    await http("POST", MCP.reconnect, null, CHAT.headers);
    UI().toast("MCP reconnecting…", "ok");
  } catch (e) {
    UI().setMCPStatus("error", `Error: ${e?.message || e}`);
    UI().toast(`Gagal reconnect: ${e?.message || e}`, "error");
  } finally { refreshStatus(); }
}

/** ====== Chat ke backend ====== */
function buildPayload(text, files) {
  if (files && files.length) {
    const fd = new FormData();
    fd.set("message", text);
    [...files].forEach((f, i) => fd.append("files", f, f.name || `file_${i}`));
    return fd;
  }
  return { message: text };
}

async function sendChatJSON(text, files) {
  const body = buildPayload(text, files);
  const res = await fetch(CHAT.endpoint, {
    method: CHAT.method || "POST",
    headers: body instanceof FormData ? { ...CHAT.headers } : { "Content-Type": "application/json", ...CHAT.headers },
    body: body instanceof FormData ? body : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error || res.statusText || "HTTP error");
  const msg = (CHAT.jsonKey && data?.[CHAT.jsonKey]) || data?.message || data?.answer || data?.output || "";
  if (!msg) throw new Error("Respons kosong atau key JSON tidak ditemukan.");
  UI().appendAssistantMarkdown(msg);
}

async function sendChatChunk(text, files) {
  const body = buildPayload(text, files);
  const res = await fetch(CHAT.endpoint, {
    method: CHAT.method || "POST",
    headers: body instanceof FormData ? { ...CHAT.headers } : { "Content-Type": "application/json", ...CHAT.headers },
    body: body instanceof FormData ? body : JSON.stringify(body),
  });
  if (!res.ok) {
    let err = "HTTP error";
    try { const j = await res.json(); err = j?.error || res.statusText || err; } catch {}
    throw new Error(err);
  }
  const reader = res.body?.getReader?.();
  if (!reader) {
    const textResp = await res.text();
    UI().appendAssistantMarkdown(textResp || "(empty)");
    return;
  }
  const decoder = new TextDecoder();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    if (chunk) UI().appendAssistantChunk(chunk);
  }
}

async function sendChatSSE(text, files) {
  // Implementasi dasar: perlakukan seperti "chunk".
  await sendChatChunk(text, files);
}

/** ====== Listener kiriman UI ====== */
async function onUISubmit(ev) {
  const { text, files } = ev.detail || {};
  if (!text && (!files || !files.length)) return;
  UI().setTyping(true);
  try {
    if (CHAT.mode === "chunk")      await sendChatChunk(text, files);
    else if (CHAT.mode === "sse")   await sendChatSSE(text, files);
    else                            await sendChatJSON(text, files);
  } catch (err) {
    UI().toast(`Gagal memproses: ${err?.message || err}`, "error");
    UI().setMCPStatus("error", `Error: ${err?.message || err}`);
  } finally {
    UI().setTyping(false);
  }
}

/** ====== Wiring ====== */
document.addEventListener("ui:mcp-connect-click", onConnect);
document.addEventListener("ui:mcp-disconnect-click", onDisconnect);
document.addEventListener("ui:mcp-reconnect-click", onReconnect);
document.addEventListener("ui:submit", onUISubmit);

// Hemat resource saat tab blur
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else startPolling();
});

/** ====== Init ====== */
UI().setMCPStatus("disconnected", "Disconnected");
startPolling();
