document.addEventListener("DOMContentLoaded", () => {
  // Elemen utama
  const form          = document.getElementById("chat-form");
  const chatInput     = document.getElementById("chat-input");
  const chatArea      = document.querySelector(".main__chat");
  const uploadBtn     = document.getElementById("upload-btn");
  const uploadMenu    = document.getElementById("upload-menu");
  const modalKak      = document.getElementById("modal-kak");
  const modalProduct  = document.getElementById("modal-product");
  const formKak       = document.getElementById("form-kak");
  const formProduct   = document.getElementById("form-product");
  
  // Inisialisasi markdown-it dengan options
  const md = window.markdownit({
    html:        false,       // nonaktifkan HTML mentah
    linkify:     true,        // auto-link URL
    typographer: true,        // kutipan pintar, dash, dll
  });

  // Utility
  const scrollToBottom = () => { chatArea.scrollTop = chatArea.scrollHeight; };

  function appendMessage(text, sender = "assistant") {
    const msg = document.createElement("div");
    msg.classList.add("message", sender);
    // Render Markdown menjadi HTML
    msg.innerHTML = md.render(text);
    document.querySelector(".main__chat").appendChild(msg);
    msg.scrollIntoView({ behavior: "smooth" });
    // msg.innerHTML = window.formatChatText ? window.formatChatText(text) : text;
    // chatArea.appendChild(msg);
    // scrollToBottom();
    return msg;
  }

  function initAutoResize() {
    if (!chatInput) return;
    const adjust = () => {
      chatInput.style.height = "auto";
      chatInput.style.height = chatInput.scrollHeight + "px";
      const maxH = parseInt(getComputedStyle(chatInput).maxHeight);
      chatInput.classList.toggle("scrolled", chatInput.scrollHeight > maxH);
    };
    chatInput.addEventListener("input", adjust);
    adjust();
  }

  // 1) Chat form submit
  form.addEventListener("submit", e => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    appendMessage(text, "user");
    chatInput.value = "";
    initAutoResize();
    sendMessage(text);
  });

  // 2) Dropdown “+” toggle
  uploadBtn.addEventListener("click", e => {
    e.stopPropagation();
    uploadMenu.classList.toggle("hidden");
  });
  document.addEventListener("click", () => uploadMenu.classList.add("hidden"));

  // 3) Pilih KAK/Product → buka modal, jangan langsung file picker
  uploadMenu.addEventListener("click", e => {
    const btn = e.target.closest(".upload-menu__item");
    if (!btn) return;
    const type = btn.dataset.type;
    if (type === "kak") {
      modalKak.classList.remove("hidden");
    } else {
      modalProduct.classList.remove("hidden");
    }
    uploadMenu.classList.add("hidden");
  });

  // 4) Tutup modal saat klik backdrop atau tombol close
  document.querySelectorAll(".modal").forEach(modal => {
    modal.addEventListener("click", e => {
      if (e.target.dataset.close || e.target.classList.contains("modal__backdrop")) {
        modal.classList.add("hidden");
      }
    });
  });

  // Fungsi polling status ingestion
  async function pollStatus(jobId, statusUrl, interval = 5000) {
    appendMessage(`Memeriksa status job ${jobId}`, "assistant");
    const timer = setInterval(async () => {
      try {
        const res  = await fetch(statusUrl);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || res.statusText);

        appendMessage(`Status: ${data.status}`, "assistant");
        if (data.status === "success" || data.status === "failure") {
          clearInterval(timer);
          appendMessage(
            data.status === "success"
              ? data.message
              : `Ingestion gagal: ${data.message}`,
            "assistant"
          );
          if (data.status === "success" && data.summary) {
            appendMessage(
              `${data.summary}`,
              "assistant"
            );
          }
        }
      } catch (err) {
        clearInterval(timer);
        appendMessage(`Error checking status: ${err.message}`, "assistant");
      }
    }, interval);
  }

  // 5) Submit form KAK
  formKak.addEventListener("submit", async e => {
    e.preventDefault();
    const formData = new FormData(formKak);
    
    // Tutup modal ketika submit 
    modalKak.classList.add("hidden");

    appendMessage("Upload KAK/TOR diterima, menunggu job_id…", "assistant");
    try {
      const res  = await fetch("/upload-kak-via-flask/", {
        method: "POST",
        body: formData
      });
    
        // Baca content-type
    const contentType = res.headers.get("content-type") || "";
    let data;
    if (contentType.includes("application/json")) {
      data = await res.json();
    } else {
      // kalau bukan JSON, ambil text untuk debug
      const text = await res.text();
      throw new Error(`Expected JSON, got:\n${text}`);
    }

      if (res.status === 202) {
        appendMessage(data.message, "assistant");
        // Mulai polling menggunakan job_id dan status_url
        pollStatus(data.job_id, data.status_url);
      } else {
        throw new Error(data.error || res.statusText);
      }
    } catch (err) {
      appendMessage("Upload KAK gagal: " + err.message, "assistant");
    } finally {
      formKak.reset();
    }
  });

  // 6) Submit form Product
  formProduct.addEventListener("submit", async e => {
    e.preventDefault();
    const data = new FormData(formProduct);
    appendMessage("Uploading Product...", "assistant");
    try {
      const res  = await fetch("/upload-product", { method: "POST", body: data });
      const json = await res.json();
      appendMessage(res.ok
        ? "Product berhasil diupload."
        : `Error: ${json.error||res.statusText}`, "assistant"
      );
    } catch (err) {
      appendMessage("Upload Product gagal: " + err.message, "assistant");
    } finally {
      formProduct.reset();
      modalProduct.classList.add("hidden");
    }
  });

  // 7) Send chat ke backend
  async function sendMessage(msg) {
    const typing = appendMessage("…", "assistant");
    try {
      const res  = await fetch("/chat/chat_message", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({message: msg})
      });
      const data = await res.json();
      typing.remove();
      appendMessage(
        res.ok && data.response
          ? data.response
          : "Error: " + (data.error || res.statusText),
        "assistant"
      );
    } catch {
      typing.remove();
      appendMessage("Connection error", "assistant");
    }
  }

  // Inisialisasi
  initAutoResize();
});
