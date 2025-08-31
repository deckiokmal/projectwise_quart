/* ============================================================================
   PROJECTWISE — MAIN UI SCRIPT (refactor)
   - Chat UI (bubble, typing, empty state)
   - Input bar (textarea + send) dengan Enter=submit, Shift+Enter=newline
   - Upload menu → modal KAK/Product + polling ingestion
   - Topbar mobile toggle
   - Toast notifications
   - MCP UI controls (badge + connect/disconnect/reconnect)
   - Fallback ke /chat/message bila MCP tidak aktif (di sisi pemanggil)
   ============================================================================ */

'use strict';

document.addEventListener('DOMContentLoaded', () => {
  if (window.__pwMainInitialized) return; // cegah init ganda
  window.__pwMainInitialized = true;

  /* =============================
   *  Util DOM & helpers umum
   *  (Komentar berbahasa Indonesia, nama fungsi/variabel berbahasa Inggris)
   * ============================= */
  const $  = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

  /* =============================
   *  Markdown renderer (markdown-it)
   *  - Satu instance untuk seluruh halaman
   *  - Opsi aman untuk konten chat
   * ============================= */
  const md = window.markdownit({
    html: false,
    linkify: true,
    breaks: true,
    typographer: true,
  });

  // Inject CSS runtime untuk kebutuhan kecil (border tabel, dsb) tanpa ubah file CSS global
  (function injectRuntimeStyle() {
    const id = 'pw-runtime-style';
    if (document.getElementById(id)) return;
    const style = document.createElement('style');
    style.id = id;
    style.textContent = `
      .md-table-wrap { overflow:auto; max-width:100%; margin:.5rem 0 1rem; border:1px solid rgba(255, 255, 255, 0.95); border-radius:10px; }
      .md-table-wrap table { width:100%; border-collapse:collapse; min-width:640px; }
      .md-table-wrap th, .md-table-wrap td { padding:.5rem .75rem; border:1px solid rgba(255, 255, 255, 0.92); }
      .codebar { display:flex; justify-content:flex-end; margin-bottom:.25rem; }
      .btn-copy, .btn-expand { font:inherit; padding:.25rem .5rem; border:1px solid rgba(255, 255, 255, 0.89); background:rgba(255,255,255,.07); border-radius:.5rem; cursor:pointer; }
      .btn-expand { margin-top:.5rem; }
    `;
    document.head.appendChild(style);
  })();

  /* =============================
   *  Referensi elemen UI
   * ============================= */
  const getChatArea = () =>
    document.getElementById('chat-section') ||
    document.getElementById('ws-section') ||
    document.querySelector('.chat__messages') ||
    document.querySelector('.ws__messages') ||
    document.querySelector('.main__chat');

  const el = {
    // chat
    chatForm:   $('#chat-form'),
    wsForm:     $('#ws-form'),
    chatInput:  $('#chat-input'),
    wsInput:    $('#ws-input'),
    typing:     $('#typing'),
    emptyState: $('#empty-state'),
    // upload & modal
    uploadBtn:       $('#upload-btn'),
    uploadMenu:      $('#upload-menu'),
    modalKak:        $('#modal-kak'),
    modalProduct:    $('#modal-product'),
    formKak:         $('#form-kak'),
    formProduct:     $('#form-product'),
    fileInput:       $('#file-input'),      // dipakai oleh UI.clearAttachments (opsional)
    attachmentPreview: $('#attachment-preview'),
    // layout
    toastRoot:      $('#toast-root'),
    btnTopbarToggle: $('#btnTopbarToggle'),
    // MCP UI controls
    mcpStatus:    $('#mcpStatus'),
    btnConnect:   $('#btnConnect'),
    btnDisconnect: $('#btnDisconnect'),
    btnReconnect:  $('#btnReconnect'),
  };

  /* =============================
   *  Toast sederhana
   *  - Klik untuk menutup
   *  - Timeout auto-hilang
   * ============================= */
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

  /* =============================
   *  Auto-grow untuk textarea input chat
   * ============================= */
  const autoGrow = (ta) => {
    if (!ta) return;
    ta.style.height = 'auto';
    const max = Math.floor(window.innerHeight * 0.45);
    ta.style.height = `${clamp(ta.scrollHeight, 40, max)}px`;
  };
  if (el.chatInput) {
    ['input', 'change'].forEach(evt => el.chatInput.addEventListener(evt, () => autoGrow(el.chatInput)));
    autoGrow(el.chatInput);
  }

  // const messagesArea = document.getElementById('messages-area');
  // function scrollToBottom() {
  //   if (!messagesArea) return;
  //   // Smooth kalau dekat bawah; instant kalau jauh
  //   const nearBottom = messagesArea.scrollHeight - messagesArea.scrollTop - messagesArea.clientHeight < 120;
  //   messagesArea.scrollTo({ top: messagesArea.scrollHeight, behavior: nearBottom ? 'smooth' : 'auto' });
  // }

  /* =============================
   *  Markdown pipeline
   *  - promoteSectionHeadings: "xxx:" → heading otomatis
   *  - renderMarkdown: string → HTML
   *  - enhanceRendered: pasca-render (tabel, link, tombol copy)
   * ============================= */
  // --- helper: decode \n, \t, \\ menjadi karakter asli ---
  const decodeEscapes = (text) => {
    const s = String(text ?? '');
    // Cepat & aman: coba JSON.parse pada string quoted
    try {
      return JSON.parse('"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"');
    } catch {
      // Fallback manual jika ada karakter aneh
      return s
        .replace(/\\n/g, '\n')
        .replace(/\\r/g, '\r')
        .replace(/\\t/g, '\t');
    }
  };

  // --- helper: unwrap + re-render fenced ```markdown ... ``` ---
  const unwrapMarkdownFences = (src) => {
    const wholeRe = /^\s*```(?:markdown|md)\s*\n([\s\S]*?)\n```[\s]*$/i;
    const partRe  = /```(?:markdown|md)\s*\n([\s\S]*?)\n```/gi;

    // Jika SELURUH teks adalah satu blok markdown
    const m = src.match(wholeRe);
    if (m) return { text: m[1], htmlSegments: null };

    // Jika ada satu/lebih segmen markdown di tengah
    if (partRe.test(src)) {
      // Reset lastIndex utk loop kedua
      partRe.lastIndex = 0;
      let out = '';
      let lastIdx = 0;
      let match;
      while ((match = partRe.exec(src)) !== null) {
        // Tambah bagian plaintext sebelum segmen
        out += src.slice(lastIdx, match.index);
        // Render segmen markdown → HTML lalu sisipkan
        const inner = match[1];
        out += md.render(inner); // aman: md.html=false
        lastIdx = partRe.lastIndex;
      }
      // Sisa tail setelah segmen terakhir
      out += src.slice(lastIdx);
      return { text: null, htmlSegments: out };
    }

    // Tidak ada fence markdown khusus
    return { text: src, htmlSegments: null };
  };

  const promoteSectionHeadings = (text) => {
    const sections = [
      'proyek','barang','jasa','rekomendasi_principal','ruang_lingkup',
      'deliverables','minimum_struktur_organisasi','perizinan_pekerjaan',
      'service_level_agreement','biaya_tersembunyi','kepatuhan_wajib',
      'risiko_teknis','risiko_non_teknis','peluang_value_added',
      'analisis_kompetitor','dependensi_pekerjaan','persyaratan_tkdn_k3ll',
      'persyaratan_pembayaran_jaminan','timeline_constraint',
      'kriteria_evaluasi_tender','pertanyaan_klarifikasi',
      'strategi_penawaran_harga', 'komponen_biaya_kritis', 'mitigasi_risiko_sla',
      'mitigasi_risiko_penalti','dasar_go_no_go','definisi_walk_away_price','rekomendasi cost structure',
      'komponen_biaya_kritis', 'mitigasi_risiko_sla','mitigasi_risiko_penalti', 'dasar_go_no_go','definisi_walk_away_price','capex', 'opex', 'cost of sales'
    ];
    const re = new RegExp(`^(?:${sections.join('|')}):\\s*$`, 'gmi');
    return String(text || '').replace(re, (m) => {
      const title = m.replace(':','').trim().replace(/_/g,' ');
      return '### ' + title.charAt(0).toUpperCase() + title.slice(1);
    });
  };

  const renderMarkdown = (input, { promoteHeadings = false } = {}) => {
    // 1) Normalisasi escape → karakter asli
    const decoded = decodeEscapes(input);

    // 2) Opsional: heading otomatis seperti sebelumnya
    const promoted = promoteHeadings ? promoteSectionHeadings(decoded) : decoded;

    // 3) Deteksi & tangani blok ```markdown …```
    const { text, htmlSegments } = unwrapMarkdownFences(promoted);

    // Jika htmlSegments terisi, itu berarti ada satu/lebih segmen markdown yang sudah
    // langsung dirender menjadi HTML (disisipkan ke dalam teks).
    if (htmlSegments !== null) return htmlSegments;

    // Jika hanya sebuah fenced penuh atau tidak ada fenced sama sekali → render biasa
    return md.render(String(text || ''));
  };

  const enhanceRendered = (containerEl) => {
    if (!containerEl) return;

    // Bungkus <table> untuk scroll horizontal dan border
    containerEl.querySelectorAll('table').forEach((tb) => {
      if (tb.parentElement && tb.parentElement.classList.contains('md-table-wrap')) return;
      const wrap = document.createElement('div');
      wrap.className = 'md-table-wrap';
      tb.parentNode.insertBefore(wrap, tb);
      wrap.appendChild(tb);
    });

    // Buka tautan di tab baru untuk keamanan
    containerEl.querySelectorAll('a[href]').forEach((a) => {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    });

    // Tombol "Copy" untuk blok kode
    containerEl.querySelectorAll('pre > code').forEach((code) => {
      const pre = code.parentElement;
      if (pre.previousElementSibling && pre.previousElementSibling.classList?.contains('codebar')) return;
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

  /* =============================
   *  Chat bubbles & append
   *  - createMsgEl: buat bubble berdasarkan role
   *  - appendMessage: sisipkan ke area chat
   * ============================= */
  // ===== Per-user color utilities (deterministik dari nama/user_id) =====
  const _COLOR_CACHE = new Map();

  function _hashString(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) {
      h = (h << 5) - h + s.charCodeAt(i);
      h |= 0;
    }
    return Math.abs(h);
  }

  /**
   * Hasilkan palet warna untuk user tertentu.
   * - Konsisten lintas sesi/device selama 'key' sama.
   * - Bubble: pastel terang; Who: lebih gelap pada hue sama.
   * - Text color otomatis #111 (gelap) bila background terang.
   */
  function paletteForUser(key, { role } = {}) {
    if (!key) key = "User";

    // Warna tetap untuk assistant/system
    const lower = String(key).toLowerCase();
    if (lower === "assistant" || lower === "system") {
      return {
        who: "hsl(220 80% 38%)",
        bubbleBg: "hsl(220 30% 95%)",
        bubbleFg: "#111",
      };
    }

    // Cache supaya hemat
    const cacheKey = `${role || ""}:${key}`;
    if (_COLOR_CACHE.has(cacheKey)) return _COLOR_CACHE.get(cacheKey);

    // Hue dari hash nama
    const hue = _hashString(key) % 360;

    // Sedikit beda tone utk self vs peer (opsional)
    const bubbleS = role === "user" ? 72 : 80;   // saturasi
    const bubbleL = role === "user" ? 76 : 86;   // lightness
    const nameS   = 72;
    const nameL   = 32;

    const bubbleBg = `hsl(${hue} ${bubbleS}% ${bubbleL}%)`;
    const bubbleFg = bubbleL > 65 ? "#111" : "#fff"; // teks gelap jika pastel terang
    const who      = `hsl(${hue} ${nameS}% ${nameL}%)`;

    const out = { who, bubbleBg, bubbleFg };
    _COLOR_CACHE.set(cacheKey, out);
    return out;
  }

  const createMsgEl = ({
    role = 'assistant',       // 'user' | 'peer' | 'assistant'
    html = '',
    text = '',
    who = '',
    time = '',
    avatar = undefined        // 'user' | 'robot' (opsional)
  } = {}) => {
    const isSelf = role === 'user';
    const isPeer = role === 'peer';
    const variantClass = isSelf ? 'msg--user' : 'msg--assistant';

    const wrap = document.createElement('div');
    wrap.className = `msg ${variantClass}`;

    // Avatar
    const avatarEl = document.createElement('div');
    avatarEl.className = 'msg__avatar';
    const avatarKind = avatar || ((isSelf || isPeer) ? 'user' : 'robot');
    avatarEl.innerHTML = (avatarKind === 'user')
      ? '<i class="fa-solid fa-user"></i>'
      : '<i class="fa-solid fa-robot"></i>';

    // Main (who + bubble)
    const main = document.createElement('div');
    main.className = 'msg__main';

    // Bubble content
    const contentEl = document.createElement('div');
    contentEl.className = 'msg__content';

    // WHO (nama kecil di atas)
    const whoEl = document.createElement('div');
    whoEl.className = 'msg__who';
    if (!who) {
      if (isSelf) who = (window?.state?.userId || document.getElementById('ws-user-id')?.value || 'You');
      else if (!isPeer) who = 'Assistant';
      // untuk peer biarkan kosong jika tidak disuplai, tapi sebaiknya isi dengan "from"
    }
    if (who) contentEl.appendChild(whoEl), (whoEl.textContent = who);

    const textEl = document.createElement('div');
    textEl.className = 'msg__text';
    const rendered = html || renderMarkdown(String(text || ''));
    textEl.innerHTML = rendered;
    enhanceRendered(textEl); // tetap pakai enhancer yang ada

    const timeEl = document.createElement('div');
    timeEl.className = 'msg__time';
    const t = time || new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    timeEl.textContent = t;

    contentEl.appendChild(textEl);
    contentEl.appendChild(timeEl);
    main.appendChild(contentEl);

    // Susunan kiri/kanan (avatar & main)
    if (isSelf) { wrap.appendChild(main); wrap.appendChild(avatarEl); }
    else { wrap.appendChild(avatarEl); wrap.appendChild(main); }

    // === >>> Injeksi warna per-user lewat CSS variables <<< ===
    const keyForColor = who || (isSelf ? (window?.wsState?.userId || 'You') : 'User');
    const pal = paletteForUser(keyForColor, { role });
    wrap.style.setProperty('--who-color', pal.who);
    wrap.style.setProperty('--bubble-bg', pal.bubbleBg);
    wrap.style.setProperty('--bubble-fg', pal.bubbleFg);

    // (opsional) simpan data atribut untuk debugging/QA
    wrap.dataset.username = keyForColor;

    return { el: wrap, contentEl: textEl };
  };


  const appendMessage = ({ role = 'assistant', html = '', text = '', meta = '', who = '', time = '', avatar = undefined } = {}) => {
    const area = getChatArea();
    const { el: bubble, contentEl } = createMsgEl({ role, html, text, who, time: time || meta, avatar });
    area?.appendChild(bubble);
    hideEmptyState();
    scrollToBottom();
    return { el: bubble, contentEl };
  };


  // Bubble asisten sementara untuk menunggu respons (akan diisi saat balasan datang)
  let pendingAssistant = null; // { el, contentEl } | null
  const ensurePendingAssistant = () => {
    if (pendingAssistant && document.body.contains(pendingAssistant.el)) return pendingAssistant;
    pendingAssistant = appendMessage({ role: 'assistant', html: '<em>…</em>' });
    // scrollToBottom();
    return pendingAssistant;
  };

  const showEmptyState = () => { el.emptyState?.classList.remove('hidden'); };
  const hideEmptyState = () => { el.emptyState?.classList.add('hidden'); };

  const scrollToBottom = () => {
    const area = getChatArea();
    if (!area) return;
    area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' });
  };

  /* =============================
   *  Typing indicator (#typing)
   *  - Ditaruh di paling bawah area chat (fallback universal)
   *  - Muncul saat ada proses berjalan (refcount berbasis typingCount)
   * ============================= */
  const placeTypingAtBottom = () => {
    const area = getChatArea();
    const typingEl = document.getElementById('typing');
    if (!area || !typingEl) return;
    area.appendChild(typingEl);
  };

  let typingCount = 0;
  const setTyping = (on) => {
    typingCount = clamp(typingCount + (on ? 1 : -1), 0, 99);
    const show = typingCount > 0;
    if (!el.typing) return;
    el.typing.classList.toggle('hidden', !show);
    if (show) {
      placeTypingAtBottom();
      scrollToBottom();
    }
  };

  /* =============================
   *  Topbar toggle & Upload menu
   * ============================= */
  (() => {
    const btn = document.getElementById('btnTopbarToggle');
    const sidebar = document.getElementById('app-sidebar');
    const overlay = document.getElementById('overlay');
    if (!btn || !sidebar || !overlay) return;

    const bpDesktop = window.matchMedia('(min-width:1024px)');
    const isDesktop = () => bpDesktop.matches;

    function openSidebar(){
      if (isDesktop()) return;                 // desktop: tidak dipakai
      document.body.classList.add('sidebar-open');
      btn.setAttribute('aria-expanded', 'true');
      overlay.hidden = false;
      // fokus ke sidebar (aksesibilitas)
      sidebar.setAttribute('tabindex','-1');
      sidebar.focus();
    }

    function closeSidebar(){
      document.body.classList.remove('sidebar-open');
      btn.setAttribute('aria-expanded', 'false');
      overlay.hidden = true;
      // kembalikan fokus ke tombol (aksesibilitas)
      btn.focus();
    }

    function toggleSidebar(){
      if (document.body.classList.contains('sidebar-open')) closeSidebar();
      else openSidebar();
    }

    // Events
    btn.addEventListener('click', toggleSidebar);
    overlay.addEventListener('click', closeSidebar);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSidebar();
    });

    // Jika user resize ke desktop, pastikan sidebar tertutup & overlay hilang
    bpDesktop.addEventListener('change', () => {
      if (isDesktop()){
        document.body.classList.remove('sidebar-open');
        overlay.hidden = true;
        btn.setAttribute('aria-expanded','false');
      }
    });

    // (Opsional) Tutup sidebar setelah klik link di dalamnya
    sidebar.addEventListener('click', (ev) => {
      const a = ev.target.closest('a');
      if (a) closeSidebar();
    });
  })();

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

  // Klik item pada upload menu untuk membuka modal yang sesuai
  el.uploadMenu && el.uploadMenu.addEventListener('click', (e) => {
    const btn = e.target.closest('.upload-menu__item'); if (!btn) return;
    const type = btn.getAttribute('data-type');
    toggleUploadMenu(false);
    if (type === 'kak') el.modalKak?.classList.remove('hidden');
    if (type === 'product') el.modalProduct?.classList.remove('hidden');
  });

  // Tutup modal saat klik backdrop/tombol close
  $$('.modal').forEach((modal) => {
    modal.addEventListener('click', (e) => {
      if (e.target.dataset.close || e.target.classList.contains('modal__backdrop')) {
        modal.classList.add('hidden');
      }
    });
  });

  /* =============================
   *  MCP status badge + tombol kontrol
   *  - Sinkron teks, warna badge, dan visibilitas tombol
   * ============================= */
  const setMCPStatus = (status, label) => {
    const badge = el.mcpStatus;
    if (!badge) return;

    const text = label || (String(status || '').charAt(0).toUpperCase() + String(status || '').slice(1));
    badge.textContent = text;
    badge.dataset.status = status;

    // Warna badge
    badge.classList.remove('badge--ok', 'badge--warning', 'badge--danger');
    if (status === 'connected') badge.classList.add('badge--ok');
    else if (status === 'connecting' || status === 'pending') badge.classList.add('badge--warning');
    else badge.classList.add('badge--danger');

    // Toggle tombol
    const show = (btn, yes) => btn && btn.classList.toggle('hidden', !yes);
    const dis  = (btn, yes) => btn && (btn.disabled = !!yes);

    if (status === 'connected') {
      show(el.btnConnect, false);
      show(el.btnDisconnect, true);
      show(el.btnReconnect, true);
      dis(el.btnDisconnect, false); dis(el.btnReconnect, false);
    } else if (status === 'connecting') {
      show(el.btnConnect, true);
      show(el.btnDisconnect, false);
      show(el.btnReconnect, false);
      dis(el.btnConnect, true);
    } else { // disconnected/error/unknown
      show(el.btnConnect, true);
      show(el.btnDisconnect, false);
      show(el.btnReconnect, false);
      dis(el.btnConnect, false);
    }
  };

  // Dispatch event agar modul MCP (mcp_control.js) bisa menangkap aksi pengguna
  el.btnConnect    && el.btnConnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-connect-click')));
  el.btnDisconnect && el.btnDisconnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-disconnect-click')));
  el.btnReconnect  && el.btnReconnect.addEventListener('click', () => document.dispatchEvent(new CustomEvent('ui:mcp-reconnect-click')));

  /* =============================
   *  Form KAK (upload + polling status)
   *  - Struktur logika dipertahankan
   * ============================= */
  el.formKak && el.formKak.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(el.formKak);
    el.modalKak?.classList.add('hidden');

    toast('KAK analisis proses di background.', 'warning', 3000);

    try {
      const res = await fetch('/upload-kak/', { method: 'POST', body: formData });
      const ct = res.headers.get('content-type') || '';

      if (!ct.includes('application/json')) {
        const t = await res.text();
        throw new Error(`Expected JSON, got:\n${t}`);
      }

      const data = await res.json();
      appendMessage({ role: 'assistant', text:'KAK Analyzer sedang diproses..' });
      // scrollToBottom();

      if (res.status === 202) {
        if (data.message) toast(data.message, 'ok', 2500);
        if (data.job_id && data.status_url) {
          pollStatus(data.job_id, data.status_url); // polling status ingestion
        } else {
          toast('Tidak ada job_id/status_url pada respons.', 'error', 4000);
        }
      } else {
        throw new Error(data?.error || res.statusText);
      }

    } catch (err) {
      const msg = err?.message || String(err);
      toast('Upload KAK gagal: ' + msg, 'error', 5000);
    } finally {
      el.formKak.reset();
    }
  });

  /* =============================
   *  Form Product (contoh serupa)
   * ============================= */
  el.formProduct && el.formProduct.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(el.formProduct);
    console.debug('[upload-product] formData entries:',
      Array.from(formData.entries()).map(([k,v]) => [k, v instanceof File ? `${v.name} (${v.type}, ${v.size}B)` : v]));
    el.modalProduct?.classList.add('hidden');

    toast('Product sizing proses di background.', 'warning', 3000);

    try {
      const res = await fetch('/upload-product/', { method: 'POST', body: formData });
      const ct = res.headers.get('content-type') || '';

      if (!ct.includes('application/json')) {
        const t = await res.text();
        throw new Error(`Expected JSON, got:\n${t}`);
      }

      const data = await res.json();
      appendMessage({ role: 'assistant', text: 'Product Sizing sedang diproses..' });
      // scrollToBottom();
      
      if (res.status === 202) {
        if (data.message) toast(data.message, 'ok', 2500);
        if (data.job_id && data.status_url) {
          pollStatus(data.job_id, data.status_url); // polling status ingestion
        } else {
          toast('Tidak ada job_id/status_url pada respons.', 'error', 4000);
        }
      } else {
        throw new Error(data?.error || res.statusText);
      }

    } catch (err) {
      const msg = err?.message || String(err);
      toast('Upload Product gagal: ' + msg, 'error', 5000);
    } finally {
      el.formProduct.reset();
    }
  });


  /* =============================
  *  Polling status ingestion (KAK)
  *  - Saat success/skipped, tampilkan summary (markdown + heading otomatis)
  *  - Backoff & 429-aware
  * ============================= */
  async function pollStatus(jobId, statusUrl) {
    let attempt = 0;
    let stopped = false;

    // Status yang dianggap masih berjalan → lanjut polling
    const ACTIVE_STATUSES = new Set([
      'pending', 'processing', 'running', 'queued', 'in_progress', 'tersimpan'
    ]);

    // Status final yang dianggap OK
    const OK_STATUSES = new Set(['success', 'skipped']);

    const maxWaitMs = 1000 * 60 * 10; // 10 menit
    const startedAt = Date.now();

    try {
      while (!stopped) {
        // hard timeout
        if (Date.now() - startedAt > maxWaitMs) {
          setTyping(false);
          toast('Polling timeout, coba lagi.', 'warning', 2500);
          break;
        }

        let res;
        try {
          res = await fetch(statusUrl);
        } catch (netErr) {
          setTyping(false);
          toast('Gagal polling status: ' + (netErr?.message || netErr), 'error', 2500);
          break;
        }

        // Penanganan 429 (rate limit) dengan Retry-After jika ada
        if (res.status === 429) {
          const retryAfter = Number(res.headers.get('retry-after')) || 3;
          // setTyping(true);
          await new Promise(r => setTimeout(r, retryAfter * 1000));
          attempt++; // naikkan attempt agar backoff jalan setelah 429
          continue;
        }

        let data = null, raw = '';
        try { data = await res.json(); } catch { raw = await res.text().catch(()=>''); }

        const sRaw = data?.status || (res.ok ? 'processing' : 'error');
        const s = String(sRaw).toLowerCase();

        // 1) Masih berjalan? lanjut polling + backoff
        if (ACTIVE_STATUSES.has(s)) {
          // setTyping(true);
          const delay = Math.min(5000 * Math.pow(1.4, attempt++), 120000); // 5s → 120s
          await new Promise(r => setTimeout(r, delay));
          continue;
        }

        // 2) Selesai sukses atau di-skip (final)
        setTyping(false);
        if (OK_STATUSES.has(s)) {
          toast(
            data?.message ||
            (s === 'skipped' ? 'File sudah pernah diingest, dilewati.' : 'Ingestion selesai.', 2000),
            'ok'
          );

          // summary diprioritaskan dari root (proxy sudah normalisasi), fallback ke result.summary
          const summary = data?.summary ?? data?.result?.summary;
          if (summary) {
            window.UI.appendAssistantSummary(String(summary));
          }
          stopped = true;
          break;
        }

        // 3) Error / status lain → tampilkan pesan lalu berhenti
        const msg = data?.message || raw || res.statusText || 'Terjadi kesalahan saat memeriksa status.';
        toast(msg, mapStatusToToastType(s), 2500);
        stopped = true;
        break;
      }
    } finally {
      // safety: pastikan indikator mati saat keluar loop karena alasan apapun
      setTyping(false);
    }
  }


  /* =============================
   *  Mapping status → tipe toast
   *  - Diekspos ke window untuk dipakai modul lain (mcp_control.js)
   * ============================= */
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
  window.mapStatusToToastType = mapStatusToToastType;

  /* =============================
   *  Public UI API (akses lintas modul)
   * ============================= */
  window.UI = {
    // Render markdown asisten; jika ada pending bubble, isi ke sana agar tidak duplikatif
    appendAssistantMarkdown(markdown, meta = '') {
      const html = renderMarkdown(String(markdown || ''));
      if (pendingAssistant && pendingAssistant.contentEl && document.body.contains(pendingAssistant.el)) {
        pendingAssistant.contentEl.innerHTML = html;
        enhanceRendered(pendingAssistant.contentEl);
      } else {
        appendMessage({ role: 'assistant', html, meta });
        // scrollToBottom();
      }
      if (typeof resetAssistantChunk === 'function') resetAssistantChunk();
      pendingAssistant = null;
    },

    // Render summary dengan heading otomatis (promoteSectionHeadings)
    appendAssistantSummary(summaryText, meta = '') {
      appendMessage({ role: 'assistant', html: renderMarkdown(String(summaryText || ''), { promoteHeadings: true }), meta });
      // scrollToBottom();
      if (typeof resetAssistantChunk === 'function') resetAssistantChunk();
    },

    // Placeholder untuk streaming chunk; sesuaikan bila menggunakan SSE/WebSocket
    appendAssistantChunk(chunk) {
      appendMessage({ role: 'assistant', text: String(chunk || '') });
      // scrollToBottom();
    },

    // Render bubble user + aktifkan pending + typing indicator
    appendUserMarkdown(markdown, meta = '') {
      appendMessage({ role: 'user', html: renderMarkdown(String(markdown || '')), meta });
      pendingAssistant = ensurePendingAssistant();
      setTyping(true);
      // scrollToBottom();
    },

    setTyping,
    setMCPStatus,
    toast,

    clearInput() { if (!el.chatInput) return; el.chatInput.value = ''; autoGrow(el.chatInput); },
    focusInput() { el.chatInput?.focus(); },

    // Opsi pembersihan lampiran bila dipakai di masa depan
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

  /* =============================
   *  State awal halaman
   * ============================= */
  showEmptyState();

  /* =============================
   *  Form chat
   *  - Submit via Enter
   *  - Newline via Shift+Enter
   * ============================= */
  el.chatForm && el.chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = el.chatInput?.value?.trim();
    if (!text) return;

    appendMessage({ role: 'user', text });
    setTyping(true);
    // scrollToBottom();

    el.chatInput.value = '';
    autoGrow(el.chatInput);

    try {
      const res = await fetch('/chat/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      const data = await res.json().catch(() => ({}));

      if (data?.reply) {
        window.UI.appendAssistantMarkdown(String(data.reply));
      } else if (data?.summary) {
        window.UI.appendAssistantSummary(String(data.summary));
      } else {
        toast('Tidak ada balasan dari server.', 'warning');
      }
      setTyping(false);
    } catch (err) {
      toast('Gagal mengirim pesan: ' + (err?.message || err), 'error');
      setTyping(false);
    } finally {
      scrollToBottom();
    }
  });

  // Enter=submit, Shift+Enter=newline pada textarea
  el.chatInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      if (e.shiftKey) return; // izinkan newline
      e.preventDefault();
      el.chatForm?.requestSubmit();
    }
  });


  /* =============================
  *  Form ws chat (WS Room)
  *  - Submit via Enter
  *  - Shift+Enter = Newline
  *  - Selaras dengan ws_room.html
  * ============================= */

  // ======= State =======
  let ws = null;
  const wsState = {
    roomId: null,
    userId: null,
    reconnectAttempt: 0,
    autoReconnect: false,
    reconnectTimer: null,
  };

  // ======= Helpers =======
  function gid(id) { return document.getElementById(id); } // id only
  function val(id) { return (gid(id)?.value || "").trim(); }
  function qs(sel) { return document.querySelector(sel); } // css selector

  function saveLast() {
    try {
      localStorage.setItem("ws_room_id", wsState.roomId || "");
      localStorage.setItem("ws_user_id", wsState.userId || "");
      localStorage.setItem("ws_auto_reconnect", String(wsState.autoReconnect));
    } catch {}
  }

  function saveHist(roomId, entry) {
    try {
      const key = `ws_hist_${roomId}`;
      const arr = JSON.parse(localStorage.getItem(key) || "[]");
      arr.push({ ...entry, ts: Date.now() });
      if (arr.length > 500) arr.shift();           // batasi size
      localStorage.setItem(key, JSON.stringify(arr));
    } catch {}
  }

  function renderHist(roomId) {
    try {
      const key = `ws_hist_${roomId}`;
      const arr = JSON.parse(localStorage.getItem(key) || "[]");
      arr.forEach((m) => {
        // langsung panggil appendMessage agar tidak menulis ulang ke storage
        const role = m.role || (m.self ? 'user' : (m.type === 'message' ? 'peer' : 'assistant'));
        appendMessage({ role, text: m.content || m.text || '', who: m.who, time: m.time, avatar: m.avatar });
      });
      if (arr.length) gid("empty-state")?.classList.add("hidden");
    } catch {}
  }

  function loadLast() {
    try {
      const r = localStorage.getItem("ws_room_id") || "";
      const u = localStorage.getItem("ws_user_id") || "";
      const a = localStorage.getItem("ws_auto_reconnect") === "true";
      if (r) gid("ws-room-id").value = r;
      if (u) gid("ws-user-id").value = u;
      gid("ws-auto-reconnect").checked = a;
    } catch {}
  }

  function setUIConnected(connected) {
    gid("join-room-btn").classList.toggle("hidden", connected);
    gid("leave-room-btn").classList.toggle("hidden", !connected);
    gid("leave-room-btn").disabled = !connected;

    const input = gid("ws-input");
    const sendBtn = qs('#ws-form button[type="submit"]');
    if (input) input.disabled = !connected;
    if (sendBtn) sendBtn.disabled = !connected;

    gid("conn-dot").classList.toggle("on", connected);
    gid("conn-label").textContent = connected ? "Connected" : "Disconnected";
    gid("conn-dot").title = connected ? "Connected" : "Disconnected";

    if (connected) {
      gid("empty-state")?.classList.add("hidden");
      input?.focus();
    }
  }

  function parseJSONSafe(s) { try { return JSON.parse(s); } catch { return null; } }

  function wsURL(roomId, userId) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/ws/chat/${encodeURIComponent(roomId)}/${encodeURIComponent(userId)}`;
  }

  // Adaptor payload WebSocket -> bubble UI ala WhatsApp
  const wsAppend = ({ type = 'message', from = '', content = '', self = false } = {}) => {
    // time otomatis di createMsgEl
    if (type === 'message') {
      if (self) {
        // pesan saya: kanan, ikon user, nama saya
        appendMessage({ role: 'user', text: content, who: from || (window?.state?.userId || 'You'), avatar: 'user' });
        saveHist(wsState.roomId, { role: 'user', content, who: from || (window?.state?.userId || 'You'), avatar: 'user' });
      } else {
        // pesan user lain: kiri, ikon user, nama pengirim
        appendMessage({ role: 'peer', text: content, who: from || 'User', avatar: 'user' });
        saveHist(wsState.roomId, { role: 'peer', content, who: from || 'User', avatar: 'user' });
      }
    } else {
      // system/assistant: kiri, ikon robot, nama "Assistant"
      appendMessage({ role: 'assistant', text: content, who: 'Assistant', avatar: 'robot' });
      saveHist(wsState.roomId, { role: 'assistant', content, who: 'Assistant', avatar: 'robot' });
    }
  };


  // ======= Reconnect backoff =======
  function scheduleReconnect() {
    if (!wsState.autoReconnect) return;
    const delay = Math.min(30000, 1000 * Math.pow(2, wsState.reconnectAttempt)); // 1s→2s→4s ... max 30s
    clearTimeout(wsState.reconnectTimer);
    wsState.reconnectTimer = setTimeout(() => {
      wsState.reconnectAttempt++;
      connectWS();
    }, delay);
  }

  // ======= Core connect / disconnect =======
  function connectWS() {
    const roomId = val("ws-room-id");
    const userId = val("ws-user-id");
    if (!roomId || !userId) {
      wsAppend({ type: "system", content: "Room ID dan User ID wajib diisi." });
      return;
    }
    wsState.roomId = roomId;
    wsState.userId = userId;
    wsState.autoReconnect = !!gid("ws-auto-reconnect")?.checked; // id sesuai HTML
    saveLast();

    try {
      if (ws) {
        ws.onopen = ws.onclose = ws.onmessage = ws.onerror = null;
        ws.close();
      }
    } catch {}

    ws = new WebSocket(wsURL(roomId, userId)); // path sesuai backend /ws/chat/<room>/<user>

    ws.onopen = () => {
      wsState.reconnectAttempt = 0;
      setUIConnected(true);
      renderHist(wsState.roomId); 
      wsAppend({ type: "system", content: `Connected to room ${roomId} as ${userId}` });
    };

    ws.onmessage = (evt) => {
      const data = parseJSONSafe(evt.data);
      if (!data) { wsAppend({ type: 'system', content: String(evt.data ?? '') }); return; }

      if (data.type === 'history' && Array.isArray(data.items)) {
        // render lama -> baru
        for (const m of data.items) {
          const isSelf = m.type === 'message' && m.from === wsState.userId;
          wsAppend({ type: m.type, from: m.from, content: m.content, self: isSelf });
        }
        return;
      }

      if (data.type === 'message') {
        const isSelf = data.from === wsState.userId;
        if (!isSelf) wsAppend({ type: 'message', from: data.from, content: data.content, self: false });
      } else {
        wsAppend({ type: 'system', content: data.content ?? JSON.stringify(data) });
      }
    };

    ws.onerror = () => {
      wsAppend({ type: "system", content: "WebSocket error" });
    };

    ws.onclose = () => {
      setUIConnected(false);
      scheduleReconnect();
    };
  }

  function disconnectWS() {
    try {
      if (ws && ws.readyState === WebSocket.OPEN) ws.close(1000, "client closed");
    } catch {}
    setUIConnected(false);
    clearTimeout(wsState.reconnectTimer);
  }

  // ======= DOM Events =======
  document.addEventListener("DOMContentLoaded", () => {
    loadLast();
  });

  gid("join-room-btn")?.addEventListener("click", () => connectWS());
  
  // helper kecil (promise delay)
  const delay = (ms) => new Promise((r) => setTimeout(r, ms));

  gid("leave-room-btn")?.addEventListener("click", async () => {
    wsState.autoReconnect = false;
    const cb = gid("ws-auto-reconnect");
    if (cb) cb.checked = false;

    // 1) kirim sinyal LEAVE (jika masih OPEN)
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "leave" }));
      } catch {}

      // 2) SELF-SHOW di UI (agar terlihat di sisi user yang keluar)
      const who = wsState.userId || gid("ws-user-id")?.value || "You";
      wsAppend({ type: "system", content: `${who} left` });

      // 3) beri sedikit jeda agar server sempat broadcast ke klien lain
      await delay(150);

      // 4) tutup socket
      try { ws.close(1000, "leave"); } catch {}
    }

    // 5) update UI lokal
    disconnectWS();
  });

  gid("ws-auto-reconnect")?.addEventListener("change", (e) => {
    wsState.autoReconnect = !!e.target.checked;
    saveLast();
  });

  gid("ws-form")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = val("ws-input");
    if (!text) return;

    if (!ws || ws.readyState !== WebSocket.OPEN) {
      wsAppend({ type: "system", content: "Not connected" });
      return;
    }

    // tampilkan segera di UI (server tidak meng-echo balik pengirim)
    wsAppend({ type: "message", from: wsState.userId, content: text, self: true });
    const ta = gid("ws-input");
    if (ta) { ta.value = ""; ta.dispatchEvent(new Event("input")); } // trigger autogrow jika ada

    // kirim ke server
    try { ws.send(JSON.stringify({ message: text })); }
    catch { wsAppend({ type: "system", content: "Failed to send message" }); }
  });

  // Enter=submit, Shift+Enter=newline
  gid("ws-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      gid("ws-form")?.requestSubmit();
    }
  });

});
