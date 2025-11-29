const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("message-input");
const statusEl = document.getElementById("status");
const sourcesEl = document.getElementById("sources-summary");
const linksEl = document.getElementById("concept-links");
const datasetEl = document.getElementById("dataset-log");

let sessionId = localStorage.getItem("session_id") || null;

async function ping() {
  try {
    const res = await fetch("/health");
    const js = await res.json();
    statusEl.textContent = js.status === "ok" ? "online" : "offline";
    statusEl.style.background = js.status === "ok" ? "#1b5e20" : "#263238";
  } catch (e) {
    statusEl.textContent = "offline";
    statusEl.style.background = "#8d6e63";
  }
}

function addMessage(role, text) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = role === "user" ? "Вы" : "GameDev LLM";
  const content = document.createElement("div");
  content.innerText = text;
  wrapper.appendChild(label);
  wrapper.appendChild(content);
  messagesEl.appendChild(wrapper);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function sendMessage(evt) {
  evt.preventDefault();
  const message = inputEl.value.trim();
  if (!message) return;
  addMessage("user", message);
  formEl.querySelector("button").disabled = true;

  const payload = {
    message,
    stage: document.getElementById("stage").value,
    engine: document.getElementById("engine").value,
    genre: document.getElementById("genre").value,
    constraints: document.getElementById("constraints").value,
    mode: document.getElementById("mode").value,
    session_id: sessionId,
  };

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const text = await res.text();
      addMessage("bot", `Ошибка: ${text}`);
      return;
    }
    const data = await res.json();
    sessionId = data.session_id;
    localStorage.setItem("session_id", sessionId);
    const finalText = data.final_answer || "(пустой ответ)";
    addMessage("bot", finalText);
    sourcesEl.innerText = data.sources_summary || "—";
    linksEl.innerHTML = "";
    (data.concept_links || []).forEach((item) => {
      const li = document.createElement("li");
      li.innerText = item;
      linksEl.appendChild(li);
    });
    datasetEl.innerText = JSON.stringify(data.dataset_log, null, 2);
  } catch (err) {
    console.error(err);
    addMessage("bot", "Не удалось связаться с сервером.");
  } finally {
    formEl.querySelector("button").disabled = false;
    inputEl.value = "";
    inputEl.focus();
  }
}

formEl.addEventListener("submit", sendMessage);
window.addEventListener("load", ping);
