/* ==========================================================================
   PROJECTWISE — MAIN UI SCRIPT (Refactor 2025-08-17)
   - Chat UI (bubble, typing, empty state)
   - Input bar horizontal [+] textarea flex [Send]
   - Upload menu → modal KAK/Product + polling ingestion
   - Topbar mobile toggle
   - Toast notifications
   - Event ke MCP: ui:submit / ui:mcp-*
   - Fallback otomatis ke /chat/message bila MCP tidak aktif

   NOTE REFaktor:
   - [ADDED] Markdown pipeline (markdown-it) terpusat: renderMarkdown(), promoteSectionHeadings(), enhanceRendered()
   - [ADDED] UI.appendAssistantSummary() → khusus summary (heading otomatis + table wrapper + copy buttons)
   - [CHANGED] createMsgEl() → konsisten kembalikan { el, contentEl }, panggil enhanceRendered()
   - [ADDED] Inject runtime CSS untuk border tabel putih (tanpa ubah file CSS global)
   - [REMOVED] Duplikasi kecil pada appendMessage berbasis text/html (dikonvergensi ke createMsgEl)
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  (function initProjectWiseUI() {
    'use strict';
    if (window.__pwMainInitialized) return;
    window.__pwMainInitialized = true;

    /* ===== Shortcuts ===== */
    const $  = (sel, el = document) => el.querySelector(sel);
    const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
    const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

    /* ===== Markdown-it init (gunakan lib yang sudah di-load di HTML) ===== */
    // [CHANGED] Gunakan 1 instance saja, aktifkan options yang aman untuk chat.
    const md = window.markdownit({
      html: false,        // jangan render HTML mentah
      linkify: true,      // deteksi URL → <a>
      breaks: true,       // newline → <br>
      typographer: true,  // tanda kutip pintar, dll
    });

    // [ADDED] Inject CSS runtime untuk border tabel putih & komponen kecil.
    (function injectRuntimeStyle() {
      const id = 'pw-runtime-style';
      if (document.getElementById(id)) return;
      const style = document.createElement('style');
      style.id = id;
      style.textContent = `
        .md-table-wrap { overflow:auto; max-width:100%; margin:.5rem 0 1rem; border:1px solid rgba(255,255,255,.25); border-radius:10px; }
        .md-table-wrap table { width:100%; border-collapse:collapse; min-width:640px; }
        .md-table-wrap th, .md-table-wrap td { padding:.5rem .75rem; border:1px solid rgba(255,255,255,0.6); } /* border putih jelas */
        .codebar { display:flex; justify-content:flex-end; margin-bottom:.25rem; }
        .btn-copy, .btn-expand { font:inherit; padding:.25rem .5rem; border:1px solid rgba(255,255,255,.3); background:rgba(255,255,255,.07); border-radius:.5rem; cursor:pointer; }
        .btn-expand { margin-top:.5rem; }
      `;
      document.head.appendChild(style);
    })();

    /* ===== Elements ===== */
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

    /* ===== Toast ===== */
    const toast = (message, type = 'ok', timeout = 3000) => {
      if (!el.toastRoot) return console.log(`[toast:${type}]`, message);
      const t = document.createElement('div');
      t.className = `toast toast--${type}`;
      t.innerHTML = `<span>${message}</span>`;
      el.toastRoot.appendChild(t);
      const remove = () => t.remove();
      setTimeout(remove, timeout);
      t.addEventListener('click', remove, { once: true });
    };

    /* ===== Auto-grow textarea ===== */
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

    /* ==========================================================================
       MARKDOWN RENDERING PIPELINE
       ========================================================================== */

    // [ADDED] Promosikan "xxx:" (baris sendiri) menjadi "### Xxx"
    const promoteSectionHeadings = (text) => {
      const sections = [
        'proyek','barang','jasa','rekomendasi_principal','ruang_lingkup',
        'deliverables','minimum_struktur_organisasi','perizinan_pekerjaan',
        'service_level_agreement','biaya_tersembunyi','kepatuhan_wajib',
        'risiko_teknis','risiko_non_teknis','peluang_value_added',
        'analisis_kompetitor','dependensi_pekerjaan','persyaratan_tkdn_k3ll',
        'persyaratan_pembayaran_jaminan','timeline_constraint',
        'kriteria_evaluasi_tender','pertanyaan_klarifikasi',
        'strategi_penawaran_harga','rekomendasi cost structure'
      ];
      const re = new RegExp(`^(?:${sections.join('|')}):\\s*$`, 'gmi');
      return String(text || '').replace(re, (m) => {
        const title = m.replace(':','').trim().replace(/_/g,' ');
        return '### ' + title.charAt(0).toUpperCase() + title.slice(1);
      });
    };

    // [ADDED] Render markdown dari string → HTML siap pakai.
    const renderMarkdown = (input, { promoteHeadings = false } = {}) => {
      const text = String(input ?? '');
      const src = promoteHeadings ? promoteSectionHeadings(text) : text;
      return md.render(src);
    };

    // [ADDED] Enhancement pasca-render: table wrapper, link target, tombol Copy untuk code.
    const enhanceRendered = (containerEl) => {
      if (!containerEl) return;

      // wrap table untuk scroll horizontal
      containerEl.querySelectorAll('table').forEach((tb) => {
        // skip kalau sudah dibungkus
        if (tb.parentElement && tb.parentElement.classList.contains('md-table-wrap')) return;
        const wrap = document.createElement('div');
        wrap.className = 'md-table-wrap';
        tb.parentNode.insertBefore(wrap, tb);
        wrap.appendChild(tb);
      });

      // buka link di tab baru
      containerEl.querySelectorAll('a[href]').forEach((a) => {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer');
      });

      // tombol copy untuk block code
      containerEl.querySelectorAll('pre > code').forEach((code) => {
        const pre = code.parentElement;
        if (pre.previousElementSibling && pre.previousElementSibling.classList?.contains('codebar')) return; // sudah ada
        const bar = document.createElement('div');
        bar.className = 'codebar';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-copy';
        btn.textContent = 'Copy';
        btn.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(code.innerText);
            toast('Disalin ke clipboard', 'ok');
          } catch {
            toast('Gagal menyalin', 'error');
          }
        });
        bar.appendChild(btn);
        pre.parentNode.insertBefore(bar, pre);
      });
    };

    /* ==========================================================================
       BUBBLES / APPEND MESSAGE
       ========================================================================== */

    // [CHANGED] createMsgEl konsisten: terima {html|text}, render markdown sekali di sini, enhance di sini.
    const createMsgEl = ({ role = 'assistant', html = '', text = '', meta = '' } = {}) => {
      const isUser = role === 'user';
      const wrap = document.createElement('div');
      wrap.className = `msg ${isUser ? 'msg--user' : 'msg--assistant'}`;

      const avatar = document.createElement('div');
      avatar.className = 'msg__avatar';
      avatar.innerHTML = isUser ? '<i class="fa-solid fa-user"></i>' : '<i class="fa-solid fa-robot"></i>';

      const content = document.createElement('div');
      content.className = 'msg__content';

      // Jika ada html, pakai apa adanya; jika tidak, render markdown dari text.
      const rendered = html || renderMarkdown(String(text || ''));
      content.innerHTML = rendered;

      // [ADDED] Enhancements UI setelah render
      enhanceRendered(content);

      const metaEl = document.createElement('div');
      metaEl.className = 'msg__meta';
      metaEl.textContent = meta || (new Date()).toLocaleTimeString();

      const main = document.createElement('div');
      main.appendChild(content);
      main.appendChild(metaEl);

      if (isUser) { wrap.appendChild(main); wrap.appendChild(avatar); }
      else { wrap.appendChild(avatar); wrap.appendChild(main); }

      return { el: wrap, contentEl: content };
    };

    // [CHANGED] appendMessage: satukan logika; gunakan createMsgEl.
    const appendMessage = ({ role = 'assistant', html = '', text = '', meta = '' } = {}) => {
      const area = getChatArea();
      const { el: bubble, contentEl } = createMsgEl({ role, html, text, meta });
      area?.appendChild(bubble);
      hideEmptyState();
      scrollToBottom();
      return { el: bubble, contentEl };
    };

    const showEmptyState = () => { el.emptyState?.classList.remove('hidden'); };
    const hideEmptyState = () => { el.emptyState?.classList.add('hidden'); };

    const scrollToBottom = () => {
      const area = getChatArea();
      if (!area) return;
      area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
    };

    let typingCount = 0;
    const setTyping = (on) => {
      typingCount = clamp(typingCount + (on ? 1 : -1), 0, 99);
      const show = typingCount > 0;
      if (!el.typing) return;
      el.typing.classList.toggle('hidden', !show);
      if (show) scrollToBottom();
    };

    /* ==========================================================================
       TOPBAR TOGGLE & UPLOAD MENU (unchanged kecuali minor konsistensi)
       ========================================================================== */
    el.btnTopbarToggle && el.btnTopbarToggle.addEventListener('click', () => {
      document.body.classList.toggle('sidebar-open');
    });

    const toggleUploadMenu = (force = null) => {
      if (!el.uploadMenu) return;
      const willShow = force ?? el.uploadMenu.classList.contains('hidden');
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

    /* ==========================================================================
       MCP Status Controls (tidak diubah kecuali konsistensi handler)
       ========================================================================== */
    const setMCPStatus = (status) => {
      if (!el.mcpStatus) return;
      el.mcpStatus.textContent = status;
      el.mcpStatus.dataset.status = status;
    };

    el.btnConnect   && el.btnConnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-connect-click')));
    el.btnDisconnect&& el.btnDisconnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-disconnect-click')));
    el.btnReconnect && el.btnReconnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-reconnect-click')));

    /* ==========================================================================
       FORM: KAK (upload + polling) — (struktur logika dipertahankan)
       ========================================================================== */
    el.formKak && el.formKak.addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const formData = new FormData(form);

      try {
        const res = await fetch(form.action, { method: 'POST', body: formData });
        let data = null, raw = '';
        try { data = await res.json(); } catch { raw = await res.text().catch(()=>''); }

        if (!res.ok) {
          const msg = data?.error || data?.message || data?.detail || res.statusText || 'Gagal upload.';
          throw new Error(msg);
        }

        // Normalisasi
        const message   = data?.message || 'Upload diproses.';
        const jobId     = data?.job_id   || data?.jobId   || data?.jobID;
        const statusUrl = data?.status_url || data?.statusUrl;

        toast(message, 'ok', 2500);

        if (jobId && statusUrl && typeof pollStatus === 'function') {
          pollStatus(jobId, statusUrl);
        } else if (res.status === 202) {
          toast('Server menerima request, tapi job_id/status_url tidak ditemukan.', 'warning', 4000);
        }
      } catch (err) {
        const msg = err?.message || String(err);
        toast('Upload KAK gagal: ' + msg, 'error', 5000);
      } finally {
        el.formKak.reset();
      }
    });

    /* ==========================================================================
       FORM: Product (contoh) — bagian lain serupa (dipersingkat)
       ========================================================================== */
    el.formProduct && el.formProduct.addEventListener('submit', async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const fd = new FormData(form);

      setTyping(true);
      try {
        const res = await fetch(form.action, { method: 'POST', body: fd });
        let data = null, raw = '';
        try { data = await res.json(); } catch { raw = await res.text().catch(()=>''); }

        setTyping(false);

        const status  = String(data?.status || (res.ok ? 'success' : 'error')).toLowerCase();
        if (status === 'success') {
          const reply = data?.reply;
          if (reply !== undefined && reply !== null && String(reply).trim() !== '') {
            // [CHANGED] render markdown agar konsisten
            appendMessage({ role: 'assistant', text: String(reply) });
            scrollToBottom?.();
          } else {
            toast('Balasan kosong dari LLM.', 'warning');
          }
        } else {
          const msg = data?.message || raw || res.statusText || 'Terjadi kesalahan.';
          toast(msg, mapStatusToToastType(status));
        }
      } catch (err) {
        setTyping(false);
        toast("Gagal terhubung ke server: " + (err?.message || err), "error");
        appendMessage({ role: 'assistant', text: 'Connection error: ' + (err?.message || err) });
      } finally {
        form.reset();
      }
    });

    /* ==========================================================================
       POLLING STATUS (KAK) — tampilkan summary saat success
       ========================================================================== */
    // [NOTE] Implementasi asli Anda dipertahankan; pastikan saat success → pakai appendAssistantSummary(data.summary)
    async function pollStatus(jobId, statusUrl) {
      let stopped = false;
      const maxWaitMs = 1000 * 60 * 10; // 10 menit
      const startedAt = Date.now();

      while (!stopped) {
        if (Date.now() - startedAt > maxWaitMs) {
          toast('Polling timeout, coba lagi.', 'warning');
          break;
        }

        try {
          const res = await fetch(`${statusUrl}?job_id=${encodeURIComponent(jobId)}`);
          let data = null, raw = '';
          try { data = await res.json(); } catch { raw = await res.text().catch(()=>''); }

          const s = String(data?.status || (res.ok ? 'processing' : 'error')).toLowerCase();
          if (s === 'processing' || s === 'pending') {
            setTyping(true);
            await new Promise(r => setTimeout(r, 1500));
            continue;
          }

          setTyping(false);

          if (s === 'success') {
            toast(data?.message || 'Ingestion selesai.', 'ok');
            if (data?.summary) {
              // [CHANGED] Gunakan API baru: heading otomatis + markdown + enhancer
              window.UI.appendAssistantSummary(String(data.summary));
            }
            stopped = true;
            break;
          }

          // error/warning
          const msg = data?.message || raw || res.statusText || 'Terjadi kesalahan saat memeriksa status.';
          toast(msg, mapStatusToToastType(s));
          stopped = true;
        } catch (err) {
          setTyping(false);
          toast("Gagal polling status: " + (err?.message || err), "error");
          stopped = true;
        }
      }
    }

    /* ==========================================================================
       UTIL LAIN
       ========================================================================== */
    const mapStatusToToastType = (s) => {
      switch (s) {
        case 'ok':
        case 'success': return 'ok';
        case 'warning':
        case 'retry':
        case 'pending': return 'warning';
        default: return 'error';
      }
    };

    const setTypingIndicatorFor = async (p) => {
      setTyping(true);
      try { return await p; }
      finally { setTyping(false); }
    };

    /* ==========================================================================
       PUBLIC UI API
       ========================================================================== */
    window.UI = {
      // kirim markdown generik
      appendAssistantMarkdown(markdown, meta = '') {
        appendMessage({ role: 'assistant', html: renderMarkdown(String(markdown || '')), meta });
        if (typeof resetAssistantChunk === 'function') resetAssistantChunk();
      },

      // [ADDED] summary: heading otomatis + markdown + enhancer
      appendAssistantSummary(summaryText, meta = '') {
        appendMessage({ role: 'assistant', html: renderMarkdown(String(summaryText || ''), { promoteHeadings: true }), meta });
        if (typeof resetAssistantChunk === 'function') resetAssistantChunk();
      },

      // potongan streaming (tetap sama)
      appendAssistantChunk(chunk) {
        // [REMOVED] implementasi lama duplikatif (jika ada). Pertahankan versi yang konsisten di project Anda.
        // Placeholder—isi sesuai mekanisme streaming Anda:
        appendMessage({ role: 'assistant', text: String(chunk || '') });
      },

      appendUserMarkdown(markdown, meta = '') {
        appendMessage({ role: 'user', html: renderMarkdown(String(markdown || '')), meta });
      },

      setTyping, setMCPStatus, toast,

      clearInput() { if (!el.chatInput) return; el.chatInput.value = ''; autoGrow(el.chatInput); },
      focusInput() { el.chatInput?.focus(); },

      clearAttachments() {
        if (!el.fileInput) return;
        el.fileInput.value = '';
        if (el.attachmentPreview) {
          el.attachmentPreview.innerHTML = '';
          el.attachmentPreview.classList.add('hidden');
        }
      },

      get els() { return { ...el }; },
    };

    /* ==========================================================================
       INITIAL UI STATE
       ========================================================================== */
    showEmptyState();

    /* ==========================================================================
       FORM CHAT (contoh minimal)
       ========================================================================== */
    el.chatForm && el.chatForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = el.chatInput?.value?.trim();
      if (!text) return;

      appendMessage({ role: 'user', text });
      el.chatInput.value = '';
      autoGrow(el.chatInput);

      // contoh post
      try {
        const res = await fetch('/chat/message', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text }) });
        const data = await res.json().catch(() => ({}));
        if (data?.reply) {
          appendMessage({ role: 'assistant', text: String(data.reply) });
        } else if (data?.summary) {
          // [CHANGED] jika backend balas ringkasan → gunakan summary renderer
          window.UI.appendAssistantSummary(String(data.summary));
        } else {
          toast('Tidak ada balasan dari server.', 'warning');
        }
      } catch (err) {
        toast('Gagal mengirim pesan: ' + (err?.message || err), 'error');
      }
    });

  })();
});
