"""SafeMap single-file server with Flask-SocketIO and aiogram.

This module hosts the SafeMap application that provides QR/barcode-based
authentication, session restoration, real-time geolocation sharing, and
shared danger markers on a Leaflet map. It keeps all state in memory with
thread-safe access and persists active tokens to disk.
"""
import asyncio
import datetime
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import URLInputFile
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("socketio").setLevel(logging.ERROR)
logging.getLogger("engineio").setLevel(logging.ERROR)

# --- Configuration constants (kept inline as requested) ---
SECRET_KEY = "secret_safe_key_123"
# NOTE: token provided by the project owner for production use.
TELEGRAM_BOT_TOKEN = "8229885598:AAHBHbuV2c5WBjcNjsxiLiIVj8oBs1IBkRg"
TELEGRAM_ADMIN_ID = 8258050467
DAILY_SALT = "LetovoCorpSecretSalt"
SESSION_DURATION = 2 * 60 * 60
POINT_DURATION = 7 * 60
TOKENS_FILE = "tokens.json"

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
bot_loop: asyncio.AbstractEventLoop | None = None


@dataclass
class SafeMapState:
    """Thread-safe state storage for sessions, tokens, users, and dangers."""

    valid_tokens: Dict[str, float] = field(default_factory=dict)
    sessions: Dict[str, str] = field(default_factory=dict)
    users: Dict[str, Dict[str, float]] = field(default_factory=dict)
    dangers: List[Dict] = field(default_factory=list)
    current_access_code: str = ""
    current_date_str: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def load_tokens(self) -> None:
        """Load persisted tokens from disk, ignoring errors."""
        if os.path.exists(TOKENS_FILE):
            try:
                with open(TOKENS_FILE, "r", encoding="utf-8") as file:
                    data = json.load(file)
                with self.lock:
                    self.valid_tokens = {k: float(v) for k, v in data.items()}
                logging.info("Loaded %s active tokens", len(self.valid_tokens))
            except Exception as exc:  # pragma: no cover - defensive
                logging.error("Failed to load tokens: %s", exc)
                with self.lock:
                    self.valid_tokens = {}

    def save_tokens(self) -> None:
        """Persist tokens atomically to disk."""
        try:
            with self.lock:
                data = dict(self.valid_tokens)
            temp_path = f"{TOKENS_FILE}.tmp"
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(data, file)
            os.replace(temp_path, TOKENS_FILE)
        except Exception as exc:  # pragma: no cover - defensive
            logging.error("Failed to save tokens: %s", exc)

    def extend_token(self, token: str) -> None:
        """Refresh token TTL if it exists."""
        with self.lock:
            if token in self.valid_tokens:
                self.valid_tokens[token] = time.time()

    def token_valid(self, token: str) -> bool:
        """Check token validity and remove if expired."""
        with self.lock:
            ts = self.valid_tokens.get(token)
            if ts is None:
                return False
            if time.time() - ts > SESSION_DURATION:
                del self.valid_tokens[token]
                return False
            return True

    def remove_session(self, sid: str) -> None:
        """Remove a session and user entry if present."""
        with self.lock:
            self.sessions.pop(sid, None)
            self.users.pop(sid, None)

    def snapshot_users(self) -> Dict[str, Dict[str, float]]:
        """Return a shallow copy of users for broadcasting."""
        with self.lock:
            return dict(self.users)

    def snapshot_dangers(self) -> List[Dict]:
        """Return a shallow copy of dangers for broadcasting."""
        with self.lock:
            return list(self.dangers)


state = SafeMapState()
state.load_tokens()


# --- Daily code generation and Telegram notifications ---

def get_daily_code() -> tuple[str, str]:
    """Generate daily access code from date and salt."""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    raw = f"{today}-{DAILY_SALT}"
    hash_int = int(hashlib.md5(raw.encode()).hexdigest(), 16)
    code_num = str(hash_int)[-12:]
    return today, code_num


async def send_access_info_async(today: str, code: str) -> None:
    """Send access code, barcode, and QR to the admin."""
    try:
        msg_text = f"🔐 *SafeMap Access*\n📅 Дата: `{today}`\n🔢 Код: `{code}`"
        await bot.send_message(TELEGRAM_ADMIN_ID, msg_text, parse_mode=ParseMode.MARKDOWN)
        barcode_url = (
            "https://bwipjs-api.metafloor.com/?bcid=code128&text="
            f"{code}&scale=3&rotate=N&includetext&background=ffffff"
        )
        qr_url = (
            "https://bwipjs-api.metafloor.com/?bcid=qrcode&text="
            f"{code}&scale=3&rotate=N&background=ffffff"
        )
        await bot.send_photo(
            TELEGRAM_ADMIN_ID,
            photo=URLInputFile(barcode_url),
            caption="📷 *Штрих-код*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await bot.send_photo(
            TELEGRAM_ADMIN_ID,
            photo=URLInputFile(qr_url),
            caption="🔳 *QR-код*",
            parse_mode=ParseMode.MARKDOWN,
        )
        logging.info("Telegram codes sent")
    except Exception as exc:  # pragma: no cover - defensive
        logging.error("Failed to send Telegram codes: %s", exc)


def send_access_info(today: str, code: str) -> None:
    """Submit sending to the bot loop."""
    if bot_loop and bot_loop.is_running():
        asyncio.run_coroutine_threadsafe(send_access_info_async(today, code), bot_loop)
    else:
        logging.warning("Bot loop is not running; cannot deliver access code")


@dp.message(Command("code"))
async def cmd_code(message: types.Message) -> None:
    """Admin command to resend the current access code."""
    if message.from_user.id != TELEGRAM_ADMIN_ID:
        await message.answer("⛔ Доступ запрещен.")
        return
    await message.answer("🔄 Генерирую текущий пропуск...")
    await send_access_info_async(state.current_date_str, state.current_access_code)


# --- Bot startup ---

def start_bot_process() -> None:
    """Run aiogram polling in a background thread with auto-restart."""
    global bot_loop
    while True:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_loop = loop
        try:
            loop.run_until_complete(dp.start_polling(bot, handle_signals=False))
        except Exception as exc:  # pragma: no cover - defensive
            logging.error("Bot crashed: %s", exc)
        finally:
            bot_loop = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # pragma: no cover - defensive
                pass
            loop.close()
        # Small delay before restart to avoid tight crash loop
        time.sleep(2)


threading.Thread(target=start_bot_process, daemon=True).start()


# --- Background tasks ---

def update_daily_code_if_needed() -> None:
    """Refresh daily code and notify admin when date changes."""
    today, new_code = get_daily_code()
    with state.lock:
        if today != state.current_date_str:
            state.current_date_str = today
            state.current_access_code = new_code
            logging.info("New daily access code generated")
            send_access_info(today, new_code)


def cleanup_worker() -> None:
    """Periodic cleanup for tokens, sessions, and dangers."""
    time.sleep(5)
    while True:
        try:
            now = time.time()
            update_daily_code_if_needed()

            expired_tokens: List[str] = []
            expired_sids: List[str] = []
            removed_dangers = False

            with state.lock:
                # Expire dangers
                before_len = len(state.dangers)
                state.dangers = [d for d in state.dangers if now - d["ts"] <= POINT_DURATION]
                removed_dangers = len(state.dangers) != before_len

                # Expire tokens
                for token, ts in list(state.valid_tokens.items()):
                    if now - ts > SESSION_DURATION:
                        expired_tokens.append(token)
                        del state.valid_tokens[token]

                # Expire sessions/users
                for sid, token in list(state.sessions.items()):
                    if token in expired_tokens or token not in state.valid_tokens:
                        expired_sids.append(sid)
                        state.sessions.pop(sid, None)
                        state.users.pop(sid, None)

                users_snapshot = dict(state.users)
                dangers_snapshot = list(state.dangers)
                tokens_snapshot = dict(state.valid_tokens)

            if expired_tokens:
                state.save_tokens()

            for sid in expired_sids:
                socketio.emit("session_expired", room=sid)

            if expired_sids:
                socketio.emit("update_users", users_snapshot, broadcast=True)

            if removed_dangers:
                socketio.emit("update_dangers", dangers_snapshot, broadcast=True)

            logging.debug(
                "Cleanup tick: tokens=%s sessions=%s dangers=%s",
                len(tokens_snapshot),
                len(users_snapshot),
                len(dangers_snapshot),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logging.error("Cleanup worker error: %s", exc)
        time.sleep(10)


def start_background_tasks() -> None:
    """Start cleanup background thread."""
    threading.Thread(target=cleanup_worker, daemon=True).start()


start_background_tasks()


# --- HTML template ---
HTML_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Geo Monitor</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.3/dist/leaflet.css" crossorigin="" />
    <script src="https://unpkg.com/leaflet@1.9.3/dist/leaflet.js" crossorigin=""></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js" crossorigin="anonymous"></script>
    <script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
    <style>
        :root {
            --primary: #2a9d8f;
            --danger: #e63946;
            --mask: #1d4ed8;
            --text: #1f2937;
            --bg: #f9fafb;
            --panel: #ffffff;
            --shadow: 0 10px 30px rgba(0,0,0,0.08);
        }
        * { box-sizing: border-box; }
        body, html { margin: 0; padding: 0; height: 100%; width: 100%; font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); }
        #map { height: 100%; width: 100%; filter: saturate(1.05); }
        #overlay {
            position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
            background: linear-gradient(135deg, rgba(42,157,143,0.12), rgba(29,78,216,0.12));
            backdrop-filter: blur(6px); z-index: 999; transition: opacity 0.3s ease;
        }
        #login-card {
            background: var(--panel); padding: 24px; border-radius: 16px; box-shadow: var(--shadow);
            width: min(420px, 90vw); text-align: center; border: 1px solid #e5e7eb;
        }
        #login-card h1 { margin: 0 0 12px; font-size: 24px; }
        #login-card p { margin: 0 0 16px; color: #4b5563; }
        #reader { width: 100%; border-radius: 12px; overflow: hidden; }
        #status { margin-top: 12px; font-weight: 600; color: var(--primary); }
        .hidden { display: none !important; }
        .floating-panel {
            position: fixed; top: 16px; right: 16px; background: var(--panel); padding: 12px 14px;
            border-radius: 12px; box-shadow: var(--shadow); border: 1px solid #e5e7eb;
            display: flex; gap: 10px; align-items: center; z-index: 500;
        }
        .badge { padding: 6px 10px; border-radius: 10px; font-weight: 600; font-size: 12px; }
        .badge.green { background: rgba(42,157,143,0.12); color: #0f5132; }
        .badge.gray { background: rgba(31,41,55,0.1); color: #111827; }
        #add-form {
            position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
            background: var(--panel); padding: 16px; border-radius: 14px; box-shadow: var(--shadow);
            border: 1px solid #e5e7eb; z-index: 500; display: none; min-width: 280px;
        }
        #add-form h3 { margin: 0 0 10px; }
        #add-form label { display: block; font-weight: 600; margin: 8px 0 6px; }
        #add-form select, #add-form textarea { width: 100%; padding: 8px; border-radius: 10px; border: 1px solid #d1d5db; background: #f9fafb; }
        #add-form textarea { resize: vertical; min-height: 60px; }
        #add-form .actions { display: flex; gap: 10px; margin-top: 12px; }
        .btn { border: none; padding: 10px 14px; border-radius: 10px; cursor: pointer; font-weight: 700; transition: transform 0.1s ease, box-shadow 0.2s ease; }
        .btn.primary { background: var(--primary); color: white; box-shadow: 0 10px 20px rgba(42,157,143,0.25); }
        .btn.secondary { background: #e5e7eb; color: #111827; }
        .btn:active { transform: translateY(1px); }
        .leaflet-popup-content-wrapper { border-radius: 12px; box-shadow: var(--shadow); }
        .leaflet-control-zoom { border-radius: 12px; overflow: hidden; }
    </style>
</head>
<body>
    <div id="overlay">
        <div id="login-card">
            <h1>QR-доступ</h1>
            <p>Отсканируйте ваш QR-код для входа. Код получает администратор.</p>
            <div id="reader"></div>
            <div id="camera-wrap" class="hidden" style="margin:10px 0 0;">
                <label for="camera-select" style="display:block; font-weight:600; margin-bottom:6px; text-align:left;">Камера</label>
                <select id="camera-select" style="width:100%; padding:10px; border-radius:10px; border:1px solid #d1d5db; background:#f9fafb;"></select>
            </div>
            <div id="status">Нажмите «Включить камеру», чтобы начать сканирование.</div>
            <button id="start-camera" class="btn primary" style="margin-top:12px;width:100%;">Включить камеру</button>
        </div>
    </div>

    <div class="floating-panel">
        <span class="badge green" id="session-state">Не авторизован</span>
        <span class="badge gray" id="user-id">ID: —</span>
    </div>

    <div id="map"></div>

    <div id="add-form">
        <h3>Добавить точку</h3>
        <label for="danger-type">Категория</label>
        <select id="danger-type">
            <option value="infected">Заражённые</option>
            <option value="mask">Масочники</option>
            <option value="other">Другие</option>
        </select>
        <label for="danger-desc">Комментарий</label>
        <textarea id="danger-desc" placeholder="Краткое описание"></textarea>
        <div class="actions">
            <button class="btn secondary" id="cancel-add">Отмена</button>
            <button class="btn primary" id="save-add">Сохранить</button>
        </div>
    </div>

    <script>
        const socket = io();
        let html5Scanner = null;
        let currentCamera = null;
        let map, addMarker;
        let userMarkers = {};
        let dangerLayers = [];
        let tempPoint = null;
        let watchId = null;
        let sessionActive = false;

        const sessionBadge = document.getElementById('session-state');
        const userBadge = document.getElementById('user-id');
        const overlay = document.getElementById('overlay');
        const statusEl = document.getElementById('status');
        const cameraWrap = document.getElementById('camera-wrap');
        const cameraSelect = document.getElementById('camera-select');
        const startBtn = document.getElementById('start-camera');

        function setCookie(name, value, hours) {
            let expires = '';
            if (hours) {
                const date = new Date();
                date.setTime(date.getTime() + (hours * 60 * 60 * 1000));
                expires = '; expires=' + date.toUTCString();
            }
            document.cookie = name + '=' + (value || '') + expires + '; path=/; SameSite=Lax';
        }

        function getCookie(name) {
            const nameEQ = name + '=';
            const ca = document.cookie.split(';');
            for (let i = 0; i < ca.length; i++) {
                let c = ca[i];
                while (c.charAt(0) === ' ') c = c.substring(1, c.length);
                if (c.indexOf(nameEQ) === 0) return c.substring(nameEQ.length, c.length);
            }
            return null;
        }

        function deleteCookie(name) {
            document.cookie = name + '=; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT;';
        }

        function startScanner() {
            if (html5Scanner) {
                html5Scanner.clear().catch(() => {});
            }
            html5Scanner = new Html5Qrcode("reader");
            Html5Qrcode.getCameras().then(devices => {
                if (devices && devices.length) {
                    cameraWrap.classList.remove('hidden');
                    cameraSelect.innerHTML = '';
                    devices.forEach((cam, idx) => {
                        const opt = document.createElement('option');
                        opt.value = cam.id;
                        opt.text = cam.label || `Камера ${idx + 1}`;
                        cameraSelect.appendChild(opt);
                    });
                    currentCamera = devices[0].id;
                    cameraSelect.value = currentCamera;
                } else {
                    statusEl.textContent = 'Камера не найдена';
                    return;
                }
                html5Scanner.start(
                    { deviceId: { exact: currentCamera } },
                    { fps: 10, qrbox: { width: 250, height: 250 } },
                    onScanSuccess,
                    () => {}
                ).then(() => {
                    statusEl.textContent = 'Сканирование...';
                }).catch(err => {
                    statusEl.textContent = 'Не удалось запустить камеру';
                    console.error(err);
                });
            }).catch(err => {
                statusEl.textContent = 'Ошибка доступа к камере';
                console.error(err);
            });
        }

        cameraSelect.addEventListener('change', () => {
            currentCamera = cameraSelect.value;
            startScanner();
        });

        startBtn.addEventListener('click', () => startScanner());

        function onScanSuccess(decodedText) {
            statusEl.textContent = 'Проверка...';
            socket.emit('login', { code: decodedText });
        }

        socket.on('login_response', data => {
            if (data.success) {
                setCookie('safemap_token', data.token, 2);
                afterLogin();
            } else {
                statusEl.textContent = 'Неверный код';
                setTimeout(() => statusEl.textContent = 'Попробуйте снова', 1500);
            }
        });

        socket.on('restore_response', data => {
            if (data.success) {
                afterLogin();
            } else {
                deleteCookie('safemap_token');
            }
        });

        socket.on('session_expired', () => {
            sessionActive = false;
            deleteCookie('safemap_token');
            stopGeolocation();
            overlay.style.display = 'flex';
            statusEl.textContent = 'Сессия истекла. Запустите камеру.';
        });

        socket.on('update_users', data => {
            if (!sessionActive || !map) return;
            const ids = Object.keys(userMarkers);
            ids.forEach(id => {
                if (!data[id]) {
                    map.removeLayer(userMarkers[id]);
                    delete userMarkers[id];
                }
            });
            Object.entries(data).forEach(([sid, info]) => {
                if (!info.lat || !info.lng) return;
                if (userMarkers[sid]) {
                    userMarkers[sid].setLatLng([info.lat, info.lng]);
                } else {
                    const icon = L.circleMarker([info.lat, info.lng], { radius: 8, color: sid === socket.id ? '#2a9d8f' : '#111827', fillColor: sid === socket.id ? '#2a9d8f' : '#111827', fillOpacity: 0.9 });
                    userMarkers[sid] = icon.addTo(map);
                }
            });
        });

        socket.on('update_dangers', payload => {
            if (!sessionActive || !map) return;
            dangerLayers.forEach(l => map.removeLayer(l));
            dangerLayers = [];
            (payload || []).forEach(p => {
                let color = '#6b7280';
                if (p.type === 'infected') color = '#e63946';
                if (p.type === 'mask') color = '#1d4ed8';
                const marker = L.circleMarker([p.lat, p.lng], { radius: 10, color: '#fff', weight: 2, fillColor: color, fillOpacity: 0.9 }).addTo(map);
                marker.bindPopup(`<strong>${p.type}</strong><br>${p.desc || ''}`);
                dangerLayers.push(marker);
            });
        });

        socket.on('connect', () => {
            const token = getCookie('safemap_token');
            if (token) {
                socket.emit('restore_session', { token });
            }
        });

        function afterLogin() {
            sessionActive = true;
            overlay.style.display = 'none';
            sessionBadge.textContent = 'Авторизован';
            userBadge.textContent = `ID: ${socket.id}`;
            initMap();
            startGeolocation();
        }

        function initMap() {
            if (map) return;
            map = L.map('map').setView([55.751244, 37.618423], 12);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(map);
            map.on('click', e => {
                tempPoint = e.latlng;
                document.getElementById('add-form').style.display = 'block';
            });
        }

        function startGeolocation() {
            if (!navigator.geolocation) return;
            watchId = navigator.geolocation.watchPosition(pos => {
                socket.emit('update_location', { lat: pos.coords.latitude, lng: pos.coords.longitude });
            }, err => console.error(err), { enableHighAccuracy: true });
        }

        function stopGeolocation() {
            if (watchId) navigator.geolocation.clearWatch(watchId);
            watchId = null;
        }

        document.getElementById('cancel-add').addEventListener('click', () => {
            document.getElementById('add-form').style.display = 'none';
            tempPoint = null;
        });

        document.getElementById('save-add').addEventListener('click', () => {
            if (!tempPoint) return;
            const payload = {
                lat: tempPoint.lat,
                lng: tempPoint.lng,
                type: document.getElementById('danger-type').value,
                desc: document.getElementById('danger-desc').value,
            };
            socket.emit('add_danger', payload);
            document.getElementById('add-form').style.display = 'none';
            document.getElementById('danger-desc').value = '';
            tempPoint = null;
        });
    </script>
</body>
</html>
"""


# --- Routes ---
@app.route("/")
def index() -> str:
    """Serve the single-page application."""
    return render_template_string(HTML_TEMPLATE)


# --- Socket.IO event handlers ---
@socketio.on("login")
def handle_login(data):
    code = str(data.get("code", "")).strip()
    sid = request.sid
    update_daily_code_if_needed()

    with state.lock:
        if code != state.current_access_code:
            emit("login_response", {"success": False})
            return

        token = str(uuid.uuid4())
        state.valid_tokens[token] = time.time()
        state.sessions[sid] = token
        users_snapshot = dict(state.users)
        dangers_snapshot = list(state.dangers)

    state.save_tokens()
    emit("login_response", {"success": True, "token": token})
    emit("update_users", users_snapshot, room=sid)
    emit("update_dangers", dangers_snapshot, room=sid)
    socketio.emit("update_users", users_snapshot, broadcast=True)


@socketio.on("restore_session")
def handle_restore(data):
    token = data.get("token")
    sid = request.sid
    update_daily_code_if_needed()

    with state.lock:
        if not token or token not in state.valid_tokens:
            emit("restore_response", {"success": False})
            return
        state.valid_tokens[token] = time.time()
        state.sessions[sid] = token
        users_snapshot = dict(state.users)
        dangers_snapshot = list(state.dangers)

    state.save_tokens()
    emit("restore_response", {"success": True})
    emit("update_users", users_snapshot, room=sid)
    emit("update_dangers", dangers_snapshot, room=sid)


@socketio.on("update_location")
def handle_location(data):
    sid = request.sid
    lat = data.get("lat")
    lng = data.get("lng")

    with state.lock:
        token = state.sessions.get(sid)
        if not token or token not in state.valid_tokens:
            emit("session_expired")
            return
        state.valid_tokens[token] = time.time()
        state.users[sid] = {"lat": lat, "lng": lng, "updated": time.time()}
        users_snapshot = dict(state.users)

    state.save_tokens()
    socketio.emit("update_users", users_snapshot, broadcast=True)


@socketio.on("add_danger")
def handle_danger(data):
    sid = request.sid
    with state.lock:
        token = state.sessions.get(sid)
        if not token or token not in state.valid_tokens:
            emit("session_expired")
            return
        state.valid_tokens[token] = time.time()
        state.dangers.append(
            {
                "id": str(time.time()),
                "lat": data.get("lat"),
                "lng": data.get("lng"),
                "type": data.get("type"),
                "desc": data.get("desc"),
                "ts": time.time(),
            }
        )
        dangers_snapshot = list(state.dangers)

    state.save_tokens()
    socketio.emit("update_dangers", dangers_snapshot, broadcast=True)


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    with state.lock:
        removed = sid in state.users
        state.sessions.pop(sid, None)
        state.users.pop(sid, None)
        users_snapshot = dict(state.users)
    if removed:
        socketio.emit("update_users", users_snapshot, broadcast=True)


# --- Entry point ---
if __name__ == "__main__":
    logging.info("Starting SafeMap on http://0.0.0.0:5000")
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
