/* ============================================================================
   PROJECTWISE — MCP CONTROL SCRIPT (refactor, MCP-only)
   - Mengelola koneksi MCP (connect / disconnect / reconnect)
   - Polling status MCP dengan interval adaptif
   - Integrasi dengan UI publik (window.UI) untuk badge & toast
   ----------------------------------------------------------------------------
   Ketentuan refactor:
   - Tidak mengubah logika bisnis yang ada, hanya menyederhanakan & merapikan
   - Menghapus seluruh jalur chat dari modul ini (Single Source of Truth di main.js)
   - Menghapus pemanggilan typing indicator; hanya main.js yang berhak
   - Mengandalkan util toast global dari main.js (toast atau toastWithCooldown)
   - Komentar: Bahasa Indonesia; Nama variabel & fungsi: Bahasa Inggris
   ============================================================================ */

'use strict';

/* =============================
 *  Konstanta Endpoint MCP
 * ============================= */
const MCP = {
  status: '/mcp/status',
  connect: '/mcp/connect',
  disconnect: '/mcp/disconnect',
  reconnect: '/mcp/reconnect',
};

/* =============================
 *  Akses UI Publik
 *  - Mengambil window.UI setiap kali dipakai agar aman terhadap urutan load
 *  - Menyediakan wrapper toast yang memprioritaskan util global (toastWithCooldown)
 * ============================= */
const __uiFallback = {
  setMCPStatus: () => {},
  toast: () => {},
};
const UI = () => (window.UI ?? __uiFallback);

// Wrapper notifikasi: gunakan toastWithCooldown bila tersedia agar seragam
function notify(message, type = 'info', key = undefined, cooldownMs = undefined) {
  const ui = UI();
  if (typeof ui.toastWithCooldown === 'function') {
    // Disarankan: ui.toastWithCooldown(message, type, key, cooldownMs)
    ui.toastWithCooldown(message, type, key, cooldownMs);
    return;
  }
  // Fallback: pakai toast biasa (tanpa cooldown)
  ui.toast(message, type);
}

/* =============================
 *  Util HTTP (fetch wrapper)
 *  - Kembalikan JSON bila ada; jika non-JSON → kembalikan { message }
 *  - Tidak mengelola cooldown internal; serahkan ke util toast global
 * ============================= */
async function http(method, url, body, extraHeaders = {}, { toastOnError = true } = {}) {
  const init = { method, headers: { ...extraHeaders } };

  if (body && !(body instanceof FormData)) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  } else if (body) {
    init.body = body; // FormData
  }

  const res = await fetch(url, init);
  const ct = res.headers.get('content-type') || '';

  let data = null;
  let raw = '';
  if (ct.includes('application/json')) data = await res.json().catch(() => ({}));
  else raw = await res.text().catch(() => '');

  if (!res.ok) {
    const status = data && data.status ? String(data.status).toLowerCase() : 'error';
    const message = (data && (data.message || data.error)) || raw || res.statusText || 'HTTP error';

    if (toastOnError !== false) {
      const t = (window.mapStatusToToastType?.(status)) || (status === 'success' ? 'ok' : status === 'failed' ? 'warning' : status === 'error' ? 'error' : 'info');
      notify(message, t, 'mcp:http:error', 15000);
    }

    throw new Error(message);
  }

  return data ?? (raw ? { message: raw } : {});
}

/* =============================
 *  Pemetaan Status → UI
 * ============================= */
function mapMCPStatus(stateObj) {
  if (stateObj?.connecting) return 'connecting';
  if (stateObj?.connected) return 'connected';
  if (stateObj?.error) return 'error';
  return 'disconnected';
}

function labelForMCP(stateObj, status) {
  if (status === 'connected') {
    const model = stateObj?.llm_model ? ` (${stateObj.llm_model})` : '';
    return `Connected ✓${model}`;
  }
  if (status === 'connecting') return 'Connecting…';
  if (status === 'error') return stateObj?.error ? `Error: ${stateObj.error}` : 'Error';
  return 'Disconnected';
}

function renderMCPState(stateObj) {
  const status = mapMCPStatus(stateObj);
  UI().setMCPStatus(status, labelForMCP(stateObj, status));
}

/* =============================
 *  Polling Status MCP
 *  - Interval adaptif: connecting → cepat, normal → standar, error → lambat
 *  - Tanpa cooldown lokal; mengandalkan util global notify()
 * ============================= */
let lastMCPState = window.__MCP_LAST_STATE || null; // simpan untuk notifikasi transisi
let pollTimer = null;
let pollIntervalMs = 1500; // default normal
let isRefreshing = false;

async function refreshMCPStatus() {
  if (isRefreshing) return;
  isRefreshing = true;

  try {
    const state = await http('GET', MCP.status, null, {}, { toastOnError: false });
    renderMCPState(state);

    const nowState = mapMCPStatus(state);

    // Notifikasi ringan saat transisi utama connected/disconnected
    if (lastMCPState !== null && nowState !== lastMCPState) {
      if ((nowState === 'connected' && lastMCPState !== 'connected') ||
          (nowState === 'disconnected' && lastMCPState !== 'disconnected')) {
        notify(
          nowState === 'connected' ? 'MCP connected.' : 'MCP disconnected',
          nowState === 'connected' ? 'ok' : 'warning',
          'mcp:state:transition',
          8000
        );
      }
    }

    lastMCPState = nowState;
    window.__MCP_LAST_STATE = nowState;

    // Interval adaptif berdasarkan state terbaru
    const nextInterval = state?.connecting ? 500 : 1500; // cepat saat connecting
    if (nextInterval !== pollIntervalMs) { pollIntervalMs = nextInterval; restartPolling(); }

  } catch (err) {
    // Render error ke badge + notifikasi via util global
    UI().setMCPStatus('error', `Error: ${err?.message || err}`);
    notify('Gagal memeriksa status MCP.', 'warning', 'mcp:poll:error', 15000);

    if (pollIntervalMs !== 4000) { pollIntervalMs = 4000; restartPolling(); }
  } finally {
    isRefreshing = false;
  }
}

function startPolling() {
  if (!pollTimer) {
    pollTimer = setInterval(refreshMCPStatus, pollIntervalMs);
    refreshMCPStatus();
  }
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function restartPolling() {
  stopPolling();
  startPolling();
}

/* =============================
 *  Aksi Koneksi MCP
 * ============================= */
async function onConnect() {
  UI().setMCPStatus('connecting', 'Connecting…');
  try {
    await http('POST', MCP.connect, null, {});
  } catch (err) {
    UI().setMCPStatus('error', `Error: ${err?.message || err}`);
    notify(`Gagal connect: ${err?.message || err}`, 'error', 'mcp:connect:error', 15000);
  } finally {
    refreshMCPStatus();
  }
}

async function onDisconnect() {
  try {
    await http('POST', MCP.disconnect, null, {});
    UI().setMCPStatus('disconnected', 'Disconnected');
  } catch (err) {
    UI().setMCPStatus('error', `Error: ${err?.message || err}`);
    notify(`Gagal disconnect: ${err?.message || err}`, 'error', 'mcp:disconnect:error', 15000);
  } finally {
    refreshMCPStatus();
  }
}

async function onReconnect() {
  UI().setMCPStatus('connecting', 'Connecting…');
  try {
    await http('POST', MCP.reconnect, null, {});
    notify('MCP reconnecting…', 'ok', 'mcp:reconnect:info', 8000);
  } catch (err) {
    UI().setMCPStatus('error', `Error: ${err?.message || err}`);
    notify(`Gagal reconnect: ${err?.message || err}`, 'error', 'mcp:reconnect:error', 15000);
  } finally {
    refreshMCPStatus();
  }
}

/* =============================
 *  Wiring Events & Lifecycle
 * ============================= */
// Event dari tombol UI (diterbitkan oleh main.js)
document.addEventListener('ui:mcp-connect-click', onConnect);
document.addEventListener('ui:mcp-disconnect-click', onDisconnect);
document.addEventListener('ui:mcp-reconnect-click', onReconnect);

// Hemat resource saat tab blur/focus
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPolling();
  else startPolling();
});

/* =============================
 *  Init Awal
 * ============================= */
UI().setMCPStatus('disconnected', 'Disconnected');
startPolling();
