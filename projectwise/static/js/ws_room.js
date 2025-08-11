(() => {
  // UI elements
  const roomInput = document.getElementById("roomId");
  const userInput = document.getElementById("userId");
  const btnConnect = document.getElementById("btnConnect");
  const btnDisconnect = document.getElementById("btnDisconnect");
  const logEl = document.getElementById("log");
  const memberList = document.getElementById("memberList");
  const currentRoom = document.getElementById("currentRoom");
  const msgForm = document.getElementById("msgForm");
  const msgInput = document.getElementById("msgInput");

  let ws = null;
  let roomId = null;
  let userId = null;
  const members = new Set();

  function appendLog(html, cls = "") {
    const div = document.createElement("div");
    div.className = "log-item " + cls;
    div.innerHTML = html;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function updateMemberList() {
    memberList.innerHTML = "";
    Array.from(members).forEach(m => {
      const li = document.createElement("li");
      li.textContent = m;
      memberList.appendChild(li);
    });
  }

  // build ws url from current origin
  function wsUrlFor(path) {
    const loc = window.location;
    const scheme = loc.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${loc.host}${path}`;
  }

  function connect() {
    roomId = roomInput.value.trim();
    userId = userInput.value.trim();
    if (!roomId || !userId) {
      alert("Please fill both room id and user id");
      return;
    }

    const url = wsUrlFor(`/ws/chat/${encodeURIComponent(roomId)}/${encodeURIComponent(userId)}`);
    ws = new WebSocket(url);

    ws.onopen = () => {
      appendLog(`<em>Connected to room <strong>${roomId}</strong> as <strong>${userId}</strong></em>`, "meta");
      btnConnect.disabled = true;
      btnDisconnect.disabled = false;
      currentRoom.textContent = `${roomId} (you: ${userId})`;
      members.add(userId);
      updateMemberList();
    };

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        // protocol: start, delta, completed, error, broadcast info
        if (data.type === "start") {
          appendLog(`<strong>${data.from}</strong>: ${escapeHtml(data.message)}`, "user");
          members.add(data.from);
          updateMemberList();
        } else if (data.type === "delta") {
          // stream partials: append to latest assistant block
          handleDelta(data);
        } else if (data.type === "completed") {
          appendLog(`<em>Assistant finished responding</em>`, "meta");
        } else if (data.type === "error") {
          appendLog(`<span class="error">Error: ${escapeHtml(String(data.error))}</span>`, "error");
        } else if (data.type === "member_join") {
          members.add(data.user);
          updateMemberList();
        } else if (data.type === "member_leave") {
          members.delete(data.user);
          updateMemberList();
        } else {
          // fallback
          appendLog(`<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`);
        }
      } catch (e) {
        appendLog(`<span class="error">Invalid message: ${escapeHtml(evt.data)}</span>`);
      }
    };

    ws.onclose = () => {
      appendLog(`<em>Disconnected</em>`, "meta");
      btnConnect.disabled = false;
      btnDisconnect.disabled = true;
      members.clear();
      updateMemberList();
      currentRoom.textContent = "—";
      ws = null;
    };

    ws.onerror = (err) => {
      console.error("WebSocket error", err);
      appendLog(`<span class="error">WebSocket error</span>`, "error");
    };
  }

  function disconnect() {
    if (ws) {
      try { ws.close(); } catch (e) {}
    }
  }

  // maintain last assistant element to append deltas
  let lastAssistantElem = null;
  function handleDelta(data) {
    // if delta from assistant, append to assistant block
    const who = data.from || "assistant";
    if (!lastAssistantElem || lastAssistantElem.dataset.who !== who) {
      lastAssistantElem = document.createElement("div");
      lastAssistantElem.className = "log-item assistant";
      lastAssistantElem.dataset.who = who;
      lastAssistantElem.innerHTML = `<strong>${escapeHtml(who)}:</strong> <span class="stream"></span>`;
      logEl.appendChild(lastAssistantElem);
    }
    const span = lastAssistantElem.querySelector(".stream");
    span.innerHTML += escapeHtml(data.content);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function escapeHtml(s) {
    if (!s && s !== 0) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Send message
  msgForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      alert("Not connected");
      return;
    }
    const text = msgInput.value.trim();
    if (!text) return;
    const payload = {
      message: text
    };
    ws.send(JSON.stringify(payload));
    msgInput.value = "";
    // show locally
    appendLog(`<strong>${userId}</strong>: ${escapeHtml(text)}`, "user");
  });

  btnConnect.addEventListener("click", connect);
  btnDisconnect.addEventListener("click", disconnect);

  // allow Enter to send message when connected
  msgInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      msgForm.dispatchEvent(new Event("submit"));
    }
  });
})();
