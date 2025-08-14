/* ==========================================================================
   PROJECTWISE — MAIN UI SCRIPT (No framework)
   - Chat UI (bubble, typing, empty state)
   - Input bar horizontal [+] textarea flex [Send]
   - Upload menu → modal KAK/Product + polling ingestion
   - Topbar mobile toggle
   - Toast notifications
   - Event ke MCP: ui:submit / ui:mcp-*
   - Fallback otomatis ke /chat/message bila MCP tidak aktif
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  (function initProjectWiseUI() {
    'use strict';
    if (window.__pwMainInitialized) return;
    window.__pwMainInitialized = true;
    console.log('[PW] MAIN INIT');

    /* ===== Konfigurasi fallback (dipakai HANYA jika MCP tidak aktif) ===== */
    const FALLBACK = {
      enabled: true,                     // true = fallback jika MCP down
      endpoint: '/chat/message',
      method: 'POST',
      jsonKey: 'response',               // key jawaban dari backend lama
      headers: { 'Content-Type': 'application/json' },
    };

    /* ===== Markdown renderer (aman) ===== */
    const md = (window.markdownit?.({
      html: false, linkify: true, typographer: true, breaks: true
    })) ?? { render: (s) => String(s ?? '') };

    /* ===== Helpers & Elements ===== */
    const $  = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    // Ambil kontainer chat (#chat-scroll → .chat__messages → .main__chat)
    const getChatArea = () =>
      document.getElementById('chat-scroll') ||
      document.querySelector('.chat__messages') ||
      document.querySelector('.main__chat');

    const el = {
      // chat
      chatForm:   $('#chat-form'),
      chatInput:  $('#chat-input'),
      typing:     $('#typing'),
      emptyState: $('#empty-state'),
      // upload & modal
      uploadBtn:     $('#upload-btn'),
      uploadMenu:    $('#upload-menu'),
      modalKak:      $('#modal-kak'),
      modalProduct:  $('#modal-product'),
      formKak:       $('#form-kak'),
      formProduct:   $('#form-product'),
      fileInput:     $('#file-input'),
      attachmentPreview: $('#attachment-preview'),
      // layout
      toastRoot:      $('#toast-root'),
      btnTopbarToggle: $('#btnTopbarToggle'),
      // MCP UI controls
      mcpStatus:   $('#mcpStatus'),
      btnConnect:  $('#btnConnect'),
      btnDisconnect: $('#btnDisconnect'),
      btnReconnect:  $('#btnReconnect'),
    };

    const clamp = (x, min, max) => Math.max(min, Math.min(x, max));
    const scrollToBottom = () => {
      const area = getChatArea();
      if (!area) return;
      area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
    };
    const hideEmptyState = () => el.emptyState && el.emptyState.classList.add('hidden');

    const toast = (message, type = 'ok', timeout = 4000) => {
      if (!el.toastRoot) return;
      const t = document.createElement('div');
      t.className = `toast toast--${type}`;
      t.innerHTML = `<span>${message}</span>`;
      el.toastRoot.appendChild(t);
      const remove = () => t.remove();
      setTimeout(remove, timeout);
      t.addEventListener('click', remove, { once: true });
    };

    const autoGrow = (ta) => {
      if (!ta) return;
      ta.style.height = 'auto';
      const max = Math.floor(window.innerHeight * 0.45);
      ta.style.height = `${clamp(ta.scrollHeight, 40, max)}px`;
    };
    el.chatInput && ['input','change'].forEach(evt =>
      el.chatInput.addEventListener(evt, () => autoGrow(el.chatInput))
    );
    autoGrow(el.chatInput);

    /* ===== Bubbles ===== */
    const createMsgEl = ({ role = 'assistant', html = '', text = '', meta = '' } = {}) => {
      const isUser = role === 'user';
      const wrap = document.createElement('div');
      wrap.className = `msg ${isUser ? 'msg--user' : 'msg--assistant'}`;

      const avatar = document.createElement('div');
      avatar.className = 'msg__avatar';
      avatar.innerHTML = isUser ? '<i class="fa-solid fa-user"></i>' : '<i class="fa-solid fa-robot"></i>';

      const content = document.createElement('div');
      content.className = 'msg__content';
      const safeHtml = html || md.render(String(text || ''));
      content.innerHTML = safeHtml;

      const metaEl = document.createElement('div');
      metaEl.className = 'msg__meta';
      metaEl.textContent = meta || (new Date()).toLocaleTimeString();

      const main = document.createElement('div');
      main.appendChild(content);
      main.appendChild(metaEl);

      if (isUser) { wrap.appendChild(main); wrap.appendChild(avatar); }
      else { wrap.appendChild(avatar); wrap.appendChild(main); }
      return { wrap, content };
    };

    const appendMessage = (args) => {
      const area = getChatArea();
      if (!area) { console.error('[PW] Chat container not found.'); return null; }
      hideEmptyState();
      const { wrap, content } = createMsgEl(args);
      area.appendChild(wrap);
      // Jika typing sedang tampil, pastikan tetap di paling bawah
      const typingEl = document.getElementById('typing');
      if (typingEl && !typingEl.classList.contains('hidden')) {
        area.appendChild(typingEl);
      }

      scrollToBottom();
      return { el: wrap, contentEl: content };
    };

    // Streaming helper (opsional)
    let _lastAssistantContent = null;
    const appendAssistantChunk = (mdChunk) => {
      if (!_lastAssistantContent) {
        const created = appendMessage({ role: 'assistant', html: md.render('') });
        _lastAssistantContent = created?.contentEl || null;
      }
      if (_lastAssistantContent) {
        _lastAssistantContent.innerHTML += md.render(String(mdChunk || ''));
        scrollToBottom();
      }
    };
    const resetAssistantChunk = () => { _lastAssistantContent = null; };

    const placeTypingAtBottom = () => {
      const area = getChatArea();
      const typingEl = document.getElementById('typing');
      if (!area || !typingEl) return;
      area.appendChild(typingEl); // re-append ⇒ selalu jadi anak terakhir
    };

    /* ===== Typing ===== */
    const setTyping = (on) => {
      const typingEl = document.getElementById('typing');
      if (!typingEl) return;
      typingEl.classList.toggle('hidden', !on);
      if (on) {
        placeTypingAtBottom();   // pastikan tepat di bawah pesan terbaru
        scrollToBottom();
      }
    };

    /* ===== Upload menu → modal ===== */
    const toggleUploadMenu = (force) => {
      if (!el.uploadMenu) return;
      const willShow = typeof force === 'boolean'
        ? force
        : el.uploadMenu.classList.contains('hidden');
      el.uploadMenu.classList.toggle('hidden', !willShow);
      el.uploadBtn?.setAttribute('aria-expanded', String(willShow));
    };
    el.uploadBtn && el.uploadBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleUploadMenu();
    });
    document.addEventListener('click', (e) => {
      if (!el.uploadMenu || el.uploadMenu.classList.contains('hidden')) return;
      const within = el.uploadMenu.contains(e.target) || (el.uploadBtn && el.uploadBtn.contains(e.target));
      if (!within) toggleUploadMenu(false);
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') toggleUploadMenu(false); });

    // Klik item upload → buka modal
    el.uploadMenu && el.uploadMenu.addEventListener('click', (e) => {
      const btn = e.target.closest('.upload-menu__item'); if (!btn) return;
      const type = btn.getAttribute('data-type');
      toggleUploadMenu(false);
      if (type === 'kak') el.modalKak?.classList.remove('hidden');
      if (type === 'product') el.modalProduct?.classList.remove('hidden');
    });

    // Tutup modal saat klik backdrop / tombol close
    $$('.modal').forEach((modal) => {
      modal.addEventListener('click', (e) => {
        if (e.target.dataset.close || e.target.classList.contains('modal__backdrop')) {
          modal.classList.add('hidden');
        }
      });
    });

    /* ===== KAK + Polling (REFactor: toast-first, append hanya saat success) ===== */
    async function pollStatus(jobId, statusUrl, interval = 5000) {
      let stopped = false;

      const levelOf = (s) => {
        const x = String(s || '').toLowerCase();
        if (x === 'success') return 'ok';
        if (x === 'failure' || x === 'error') return 'error';
        return 'warn'; // queued / running / processing / etc.
      };

      const tick = async () => {
        if (stopped) return;
        try {
          const res  = await fetch(statusUrl);
          const data = await res.json();
          if (!res.ok) throw new Error(data?.error || res.statusText);

          const s = String(data.status || '').toLowerCase();
          toast(`Job ${jobId}: ${s || 'unknown'}`, levelOf(s), 2500);

          if (s === 'success') {
            // tampilkan hasil ke chat (assistant)
            appendMessage({ role: 'assistant', text: data.message || 'Ingestion berhasil.' });
            if (data.summary) {
              appendMessage({ role: 'assistant', text: String(data.summary) });
            }
            stopped = true;
            return;
          }
          if (s === 'failure' || s === 'error') {
            // gagal → cukup toast, tanpa append ke chat
            stopped = true;
            return;
          }
        } catch (err) {
          toast(`Job ${jobId}: ${err?.message || err}`, 'error', 4000);
          stopped = true;
          return;
        }
        // lanjut polling
        setTimeout(tick, interval);
      };

      // kick-off
      toast(`Job ${jobId}: memeriksa status…`, 'warn', 2000);
      tick();
    }

    el.formKak && el.formKak.addEventListener('submit', async (e) => {
      e.preventDefault();
      const formData = new FormData(el.formKak);
      el.modalKak?.classList.add('hidden');

      // awalnya via toast, bukan chat bubble
      toast('Upload KAK/TOR diterima, menunggu job_id…', 'warn', 3000);

      try {
        const res  = await fetch('/upload-kak/', { method: 'POST', body: formData });
        const ct = res.headers.get('content-type') || '';
        if (!ct.includes('application/json')) { const t = await res.text(); throw new Error(`Expected JSON, got:\n${t}`); }
        const data = await res.json();
        if (res.status === 202) {
          if (data.message) toast(data.message, 'ok', 2500);
          if (data.job_id && data.status_url) {
            // polling akan menampilkan toast status & appendMessage saat success
            pollStatus(data.job_id, data.status_url);
          } else {
            toast('Tidak ada job_id/status_url pada respons.', 'error', 4000);
          }
        } else {
          throw new Error(data?.error || res.statusText);
        }
      } catch (err) {
        toast('Upload KAK gagal: ' + (err?.message || err), 'error', 5000);
      } finally {
        el.formKak.reset();
      }
    });

    /* ===== Product (REFactor: toast untuk progres; append hanya saat success) ===== */
    el.formProduct && el.formProduct.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = new FormData(el.formProduct);

      // progres via toast
      toast('Uploading Product…', 'warn', 2500);

      try {
        const res  = await fetch('/upload-product', { method: 'POST', body: data });
        const json = await res.json().catch(() => ({}));

        // gunakan "status" dari server bila ada; jika tidak, derive dari HTTP ok
        const status = String(json?.status || (res.ok ? 'success' : 'failure')).toLowerCase();

        if (status === 'success') {
          appendMessage({ role: 'assistant', text: json?.message || 'Product berhasil diupload.' });
          if (json?.summary) appendMessage({ role: 'assistant', text: String(json.summary) });
          toast('Upload product: success', 'ok', 2000);
        } else {
          const errMsg = json?.error || json?.message || res.statusText || 'Unknown error';
          toast(`Upload product gagal: ${errMsg}`, 'error', 4500);
        }
      } catch (err) {
        toast('Upload Product gagal: ' + (err?.message || err), 'error', 4500);
      } finally {
        el.formProduct.reset();
        el.modalProduct?.classList.add('hidden');
      }
    });

    /* ===== Submit Chat ===== */
    // Enter: kirim (Shift+Enter: newline)
    el.chatInput && el.chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); el.chatForm?.requestSubmit(); }
    });

    el.chatForm && el.chatForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text  = (el.chatInput?.value || '').trim();
      const files = el.fileInput?.files || [];
      if (!text && (!files || !files.length)) { el.chatInput?.focus(); return; }

      // 1) Render bubble user
      appendMessage({ role: 'user', text });
      // 2) Bersihkan input & preview
      if (el.chatInput) { el.chatInput.value = ''; autoGrow(el.chatInput); }
      if (el.fileInput) el.fileInput.value = '';
      if (el.attachmentPreview) { el.attachmentPreview.innerHTML = ''; el.attachmentPreview.classList.add('hidden'); }

      // 3) Broadcast ke MCP
      document.dispatchEvent(new CustomEvent('ui:submit', { detail: { text, files } }));

      // 4) Fallback otomatis jika MCP tidak aktif
      if (!window.__PW_HAS_MCP__ && FALLBACK.enabled) {
        try {
          setTyping(true);
          const res  = await fetch(FALLBACK.endpoint, {
            method: FALLBACK.method,
            headers: FALLBACK.headers,
            body: JSON.stringify({ message: text })
          });
          const data = await res.json().catch(() => ({}));
          setTyping(false);
          const reply = res.ok ? (data?.[FALLBACK.jsonKey] || data?.message || data?.answer || data?.output || '(kosong)')
                               : (`Error: ${data?.error || res.statusText}`);
          appendMessage({ role: 'assistant', text: reply });
        } catch (err) {
          setTyping(false);
          appendMessage({ role: 'assistant', text: 'Connection error: ' + (err?.message || err) });
        }
      }
    });

    /* ===== Topbar toggle ===== */
    el.btnTopbarToggle && el.btnTopbarToggle.addEventListener('click', () => {
      const open = document.body.classList.toggle('topbar-open');
      el.btnTopbarToggle.setAttribute('aria-expanded', String(open));
    });

    /* ===== MCP Status Buttons & Badge ===== */
    const setButtonsForState = (state) => {
      const show = (btn, on) => btn && btn.classList.toggle('hidden', !on);
      switch (state) {
        case 'connected':
          show(el.btnConnect, false); show(el.btnDisconnect, true); show(el.btnReconnect, true);
          el.btnConnect?.removeAttribute('disabled'); el.btnDisconnect?.removeAttribute('disabled'); el.btnReconnect?.removeAttribute('disabled');
          break;
        case 'connecting':
          show(el.btnConnect, true); show(el.btnDisconnect, true); show(el.btnReconnect, true);
          el.btnConnect?.setAttribute('disabled','true'); el.btnDisconnect?.setAttribute('disabled','true'); el.btnReconnect?.setAttribute('disabled','true');
          break;
        default:
          show(el.btnConnect, true); show(el.btnDisconnect, false); show(el.btnReconnect, false);
          el.btnConnect?.removeAttribute('disabled'); el.btnDisconnect?.removeAttribute('disabled'); el.btnReconnect?.removeAttribute('disabled');
      }
    };
    const setBadge = (status, label) => {
      if (!el.mcpStatus) return;
      el.mcpStatus.textContent = label || (
        status === 'connected' ? 'Connected' :
        status === 'connecting' ? 'Connecting…' :
        status === 'error' ? 'Error' : 'Disconnected'
      );
      el.mcpStatus.classList.remove('badge--ok','badge--warn','badge--danger');
      if (status === 'connected') el.mcpStatus.classList.add('badge--ok');
      if (status === 'connecting') el.mcpStatus.classList.add('badge--warn');
      if (status === 'error' || status === 'disconnected') el.mcpStatus.classList.add('badge--danger');
    };
    const setMCPStatus = (status, label) => { setBadge(status, label); setButtonsForState(status); };

    // Relay tombol ke MCP
    el.btnConnect    && el.btnConnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-connect-click')));
    el.btnDisconnect && el.btnDisconnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-disconnect-click')));
    el.btnReconnect  && el.btnReconnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-reconnect-click')));

    /* ===== Public UI API (dipakai MCP) ===== */
    window.UI = {
      appendAssistantMarkdown(markdown, meta = '') { appendMessage({ role: 'assistant', html: md.render(String(markdown || '')), meta }); resetAssistantChunk(); },
      appendAssistantChunk,
      appendUserMarkdown(markdown, meta = '') { appendMessage({ role: 'user', html: md.render(String(markdown || '')), meta }); },
      setTyping, setMCPStatus, toast,
      clearInput() { if (!el.chatInput) return; el.chatInput.value = ''; autoGrow(el.chatInput); },
      focusInput() { el.chatInput?.focus(); },
      clearAttachments() { if (el.fileInput) el.fileInput.value = ''; if (el.attachmentPreview) { el.attachmentPreview.innerHTML=''; el.attachmentPreview.classList.add('hidden'); } },
      get els() { return { ...el }; },
    };

    // Initial badge
    setMCPStatus('disconnected');

    // Chips → isi input
    $$('.chip').forEach((chip) => chip.addEventListener('click', () => {
      if (!el.chatInput) return;
      const t = chip.textContent?.trim() || '';
      el.chatInput.value = t ? `${t}: ` : '';
      autoGrow(el.chatInput); el.chatInput.focus();
    }));
  })();

  document.addEventListener('DOMContentLoaded', () => {
  const footer = document.querySelector('.chat__input');
  const root   = document.documentElement;
  if (!footer) return;

  const applyHeight = () => {
    const h = Math.ceil(footer.getBoundingClientRect().height);
    root.style.setProperty('--input-h', h + 'px');
  };

  const ro = new ResizeObserver(applyHeight);
  ro.observe(footer);

  applyHeight();
  window.addEventListener('resize', applyHeight);
});
});
