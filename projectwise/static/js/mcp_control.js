/**
 * ============================================================
 *  MCP Control Module (Client-Side)
 * ============================================================
 *
 *  Tujuan:
 *  --------
 *  - Mengelola koneksi ke MCP server (connect, disconnect, status).
 *  - Menyediakan mekanisme polling status MCP dengan interval adaptif.
 *  - Menampilkan indikator status MCP di UI (via renderServerState / setMCPStatus).
 *  - Menangani error secara human-readable melalui toast & indikator UI.
 *
 *  Perbaikan & Penyempurnaan:
 *  --------------------------
 *  1) **Single Source of Truth untuk toast mapping**
 *     - Sebelumnya `mapStatusToToastType` didefinisikan ulang di file ini dan di `main.js`.
 *     - Sekarang hanya didefinisikan sekali di `main.js` dan diekspor ke `window`.
 *     - File ini cukup pakai `window.mapStatusToToastType(...)` agar konsisten.
 *
 *  2) **HTTP helper lebih fleksibel**
 *     - Fungsi `http()` sekarang punya opsi `{ toastOnError: true|false }`.
 *     - Default: `true` → error akan menampilkan toast.
 *     - Untuk polling status MCP, dipanggil dengan `toastOnError:false` agar tidak banjir notifikasi.
 *
 *  3) **Polling status MCP tanpa spam**
 *     - Polling jalan terus dengan interval adaptif (500ms → 1500ms → 4000ms).
 *     - Toast hanya muncul saat ada **transisi status** (mis. "connected" → "disconnected").
 *     - Jika error berulang, toast dibatasi dengan cooldown (mis. 15 detik sekali).
 *     - Indikator MCP (`setMCPStatus`) tetap diperbarui setiap kali agar UI real-time.
 *
 *  4) **UX lebih bersih & konsisten**
 *     - Tidak ada lagi notifikasi berulang tiap kali polling gagal.
 *     - Pesan toast lebih human-readable: 
 *       contoh: "MCP terhubung.", "MCP terputus.", "Gagal memeriksa status MCP."
 *     - Indikator visual (ikon/label MCP) tetap konsisten dengan implementasi UI sebelumnya.
 *
 *  Dampak Positif:
 *  ---------------
 *  - Code lebih DRY (tidak ada duplikasi logika).
 *  - Toast lebih relevan (hanya saat transisi / error signifikan).
 *  - Indikator MCP tetap realtime & akurat.
 *  - UX lebih baik: user tidak dibanjiri notifikasi saat server down / reconnect.
 *
 * ============================================================
 */


// Beri tahu UI bahwa lapisan MCP aktif
window.__PW_HAS_MCP__ = true;

/** ====== KONFIG CHAT ====== */
const CHAT = {
  endpoint: "/chat/message",
  method: "POST",
  mode: "json",        // "json" | "chunk" | "sse"
  jsonKey: "reply", // kunci field jawaban di JSON
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
async function http(method, url, body, extraHeaders = {}, opts = { toastOnError: true }) {
  const opt = { method, headers: { ...extraHeaders } };
  
  if (body && !(body instanceof FormData)) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  } else if (body) {
    opt.body = body; // FormData
  }

  const res = await fetch(url, opt);

  const ct  = res.headers.get('content-type') || '';
  
  let data = null;
  let raw   = '';
  
  if (ct.includes('application/json')) {
    data = await res.json().catch(() => ({}));
  } else {
    raw = await res.text().catch(() => '');
  }

  if (!res.ok) {
    // Pakai message humanis dari backend bila ada
    const status  = (data && data.status) ? String(data.status).toLowerCase() : 'error';
    const message = (data && (data.message || data.error)) || raw || res.statusText || 'HTTP error';
  
    // opsional: tampilkan toast di sini untuk semua pemanggil http()
    if (!opts || opts.toastOnError !== false) {
      // Tampilkan toast sesuai status
      UI().toast(
        message,
        window.mapStatusToToastType?.(status)
          ?? (status === 'success' ? 'ok' : status === 'failed' ? 'warning' : status === 'error' ? 'error' : 'info')
      );
    }

    throw new Error(message);
  }

  // Balikan JSON bila ada; jika non-JSON, bungkus agar tetap bisa dipakai pemanggil
  return data ?? (raw ? { message: raw } : {});
}

/** ====== MCP Status Polling ====== */
let __MCP_LAST_STATE = window.__MCP_LAST_STATE || null;
let __MCP_ERR_COOLDOWN_UNTIL = window.__MCP_ERR_COOLDOWN_UNTIL || 0;

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
    const s = await http("GET", MCP.status, null, {}, { toastOnError: false });
    renderServerState(s);

    const nowState = mapStatus(s); // "connected"|"connecting"|"error"|"disconnected"

    if (__MCP_LAST_STATE !== null && nowState !== __MCP_LAST_STATE) {
      if ((nowState === "connected" && __MCP_LAST_STATE !== "connected") ||
        (nowState === "disconnected" && __MCP_LAST_STATE !== "disconnected")) {
        UI().toast(
          nowState === "connected" ? "MCP connected." : "MCP disconnected",
          nowState === "connected" ? "ok" : "warning"
        );
      }
    }

    __MCP_LAST_STATE = nowState;
    window.__MCP_LAST_STATE = nowState;

    const next = s?.connecting ? 500 : 1500;

    if (next !== pollMs) { pollMs = next; restartPolling(); }
  } catch (e) {
    const now = Date.now();
    if (now >= __MCP_ERR_COOLDOWN_UNTIL) {
      UI().toast("Gagal memeriksa status MCP.", "warning");
      __MCP_ERR_COOLDOWN_UNTIL = now + 15000;
      window.__MCP_ERR_COOLDOWN_UNTIL = __MCP_ERR_COOLDOWN_UNTIL;
    }

    UI().setMCPStatus("error", `Error: ${e?.message || e}`);

    if (pollMs !== 4000) { pollMs = 4000; restartPolling(); }
  } finally {
    isRefreshing = false;
  }
}

function startPolling() {
  if (!pollTimer) {
    pollTimer = setInterval(refreshStatus, pollMs);
    refreshStatus();
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer); pollTimer = null;
  }
}

function restartPolling() {
  stopPolling();
  startPolling();
}

/** ====== Aksi MCP ====== */
async function onConnect() {
  UI().setMCPStatus("connecting", "Connecting…");
  try {
    await http("POST", MCP.connect, null, CHAT.headers);
    // UI().toast("MCP connected.", "ok");
  } catch (e) {
    UI().setMCPStatus("error", `Error: ${e?.message || e}`);
    UI().toast(`Gagal connect: ${e?.message || e}`, "error");
  } finally { refreshStatus(); }
}

async function onDisconnect() {
  try {
    await http("POST", MCP.disconnect, null, CHAT.headers);
    UI().setMCPStatus("disconnected", "Disconnected");
    // UI().toast("MCP disconnected.", "warning");
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
  
  // 🔹 Normalisasi status & message dari server
  const status  = String(data?.status || (res.ok ? 'success' : 'error')).toLowerCase();
  const message = data?.message || data?.error || res.statusText || 'Terjadi kesalahan.';

  // Tampilkan toast sesuai status
  UI().toast(
    message,
    window.mapStatusToToastType?.(status)
      ?? (status === 'success' ? 'ok' : status === 'failed' ? 'warning' : status === 'error' ? 'error' : 'info')
  );

  // Error HTTP: lempar agar onUISubmit catch → status UI di-set error
  if (!res.ok) throw new Error(message);

  // Ambil isi balasan
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
    let status = 'error';
    let msg = 'HTTP error';
    try {
      const j = await res.json();
      status = String(j?.status || 'error').toLowerCase();
      msg    = j?.message || j?.error || res.statusText || msg;
    } catch {
      try { msg = await res.text(); } catch {}
    }
    // Tampilkan toast sesuai status
    UI().toast(
      message,
      window.mapStatusToToastType?.(status)
        ?? (status === 'success' ? 'ok' : status === 'failed' ? 'warning' : status === 'error' ? 'error' : 'info')
    );
    throw new Error(msg);
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

  // Reset flag & cooldown saat mulai submit baru
  window.__LAST_SUBMIT_FAILED__ = false;
  window.__TOAST_COOLDOWN__ = window.__TOAST_COOLDOWN__ || 0;

  try {
    if (CHAT.mode === "chunk") {
      await sendChatChunk(text, files);
    } else if (CHAT.mode === "sse") {
      await sendChatSSE(text, files);
    } else {
      await sendChatJSON(text, files);
    }

    // Sukses → reset cooldown agar error berikutnya tampil normal
    window.__TOAST_COOLDOWN__ = 0;

  } catch (err) {
    window.__LAST_SUBMIT_FAILED__ = true;

    const rawMessage = (err && err.message) ? err.message : String(err || "");
    const isGenericHttp = rawMessage.toLowerCase().includes("http error") || rawMessage === "HTTP error";
    const isOffline = (typeof navigator !== "undefined" && navigator && navigator.onLine === false);

    const mcpState = (window.__MCP_LAST_STATE || "").toLowerCase(); // "connected" | "disconnected" | "connecting" | ""
    const now = Date.now();
    const canToast = now >= window.__TOAST_COOLDOWN__;

    let message, level;

    if (isOffline) {
      message = "Koneksi internet terputus. Periksa jaringan Anda.";
      level = "warning"; // gunakan "warning" jika CSS kamu .toast--warning
    } else if (mcpState === "disconnected") {
      message = "MCP belum terhubung.";
      level = "warning";
    } else if (mcpState === "connecting") {
      message = "Sedang mencoba menghubungkan MCP...";
      level = "info";
    } else {
      // Pesan dari error yang bukan generic HTTP, jika tidak ada gunakan fallback
      message = (!isGenericHttp && rawMessage) ? rawMessage : "Terjadi kesalahan saat memproses permintaan.";
      level = "error";
    }

    if (canToast) {
      UI().toast(message, level);
      window.__TOAST_COOLDOWN__ = now + 10_000; // cooldown 10 detik anti spam
    }

    // Update indikator status MCP di UI (hindari 'error' jika state belum diketahui)
    UI().setMCPStatus(
      mcpState === "disconnected" ? "disconnected" :
      (mcpState === "connecting" ? "connecting" :
        (mcpState ? "error" : "unknown")),
      message
    );

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
