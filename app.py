import os
import time
import threading
from typing import Dict, List

import requests
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret')
socketio = SocketIO(app, async_mode='threading')

ACCESS_CODE = "ACCESS123"
SESSION_LIFETIME = 2 * 60 * 60  # 2 hours in seconds
DANGER_LIFETIME = 7 * 60  # 7 minutes in seconds
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8229885598:AAE3m3Chvaob6aqCacz35CMIoyfN5arOX7c")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8258050467")

sessions: Dict[str, float] = {}
user_locations: Dict[str, Dict[str, float]] = {}
danger_points: List[Dict] = []

session_lock = threading.Lock()
danger_lock = threading.Lock()
location_lock = threading.Lock()


def send_telegram_message(text: str):
    """Send notification to admin via Telegram bot."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            timeout=5,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        )
    except Exception:
        # Silently ignore Telegram issues to avoid breaking the app
        pass


def notify_admin_async(message: str):
    threading.Thread(target=send_telegram_message, args=(message,), daemon=True).start()


def session_valid(user_id: str) -> bool:
    """Check if a session is still valid and update state."""
    with session_lock:
        ts = sessions.get(user_id)
        if not ts:
            return False
        if time.time() - ts > SESSION_LIFETIME:
            del sessions[user_id]
            return False
        return True


def mark_session(user_id: str):
    with session_lock:
        sessions[user_id] = time.time()


def cleanup_expired_sessions():
    while True:
        time.sleep(30)
        now = time.time()
        expired = []
        with session_lock:
            for user_id, ts in list(sessions.items()):
                if now - ts > SESSION_LIFETIME:
                    del sessions[user_id]
                    expired.append(user_id)
        for uid in expired:
            socketio.emit('session_expired', room=uid)


def cleanup_expired_dangers():
    while True:
        time.sleep(60)
        now = time.time()
        removed = False
        with danger_lock:
            before = len(danger_points)
            danger_points[:] = [p for p in danger_points if now - p['timestamp'] <= DANGER_LIFETIME]
            removed = len(danger_points) != before
        if removed:
            socketio.emit('update_dangers', {'points': danger_points})


def start_background_tasks():
    session_thread = threading.Thread(target=cleanup_expired_sessions, daemon=True)
    danger_thread = threading.Thread(target=cleanup_expired_dangers, daemon=True)
    session_thread.start()
    danger_thread.start()


@app.route('/')
def index():
    return render_template_string(
        """
        <!doctype html>
        <html lang="ru">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Geo Monitor</title>
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.3/dist/leaflet.css" integrity="sha256-sA+e2atLYY1WZLx3gG1JQbSazB7Bs8LJpYtXuX2v6L8=" crossorigin=""/>
            <script src="https://unpkg.com/leaflet@1.9.3/dist/leaflet.js" integrity="sha256-o9N1j7kPtv8HkHfPfhANeV6wB8Cib89s4gE2f2YB04Q=" crossorigin=""></script>
            <script src="https://cdn.socket.io/4.7.5/socket.io.min.js" integrity="sha384-wfI5qX6Ch12yvDqOiiMHDL/95B2S/bRMyCV2wAPOQgpdnH3UX0eD+s/COM24hHJ5" crossorigin="anonymous"></script>
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
                <label for="danger-photo">Фото (необязательно)</label>
                <input type="file" id="danger-photo" accept="image/*" style="width:100%; padding:6px 0;" />
                <div class="actions">
                    <button class="btn secondary" id="cancel-add">Отмена</button>
                    <button class="btn primary" id="submit-add">Добавить</button>
                </div>
            </div>

            <script>
                const ACCESS_CODE = '{{ access_code }}';
                const socket = io();
                const SESSION_LIFETIME = {{ session_lifetime }} * 1000;

                let map, addMarker = null, addLatLng = null, addForm = document.getElementById('add-form');
                let dangerMarkers = [];
                let userMarkers = {};
                let geoTimer = null;
                let sessionActive = false;
                let userId = localStorage.getItem('geo_user_id') || `u-${crypto.randomUUID()}`;
                localStorage.setItem('geo_user_id', userId);
                document.getElementById('user-id').innerText = `ID: ${userId}`;

                const statusEl = document.getElementById('status');
                const overlay = document.getElementById('overlay');
                const sessionBadge = document.getElementById('session-state');
                let html5Scanner = null;
                const cameraSelect = document.getElementById('camera-select');
                const cameraWrap = document.getElementById('camera-wrap');
                let startingScanner = false;
                let cameraReady = false;

                function initMap() {
                    map = L.map('map').setView([55.751244, 37.618423], 12);
                    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                        maxZoom: 19,
                        attribution: '&copy; OpenStreetMap'
                    }).addTo(map);

                    map.on('click', (e) => {
                        if (!sessionActive) return;
                        openAddForm(e.latlng);
                    });
                }

                function openAddForm(latlng) {
                    addLatLng = latlng;
                    addForm.style.display = 'block';
                }

                function closeAddForm() {
                    addForm.style.display = 'none';
                    addLatLng = null;
                    document.getElementById('danger-desc').value = '';
                    document.getElementById('danger-photo').value = '';
                }

                async function startScanner(cameraId = null) {
                    if (startingScanner) return;
                    startingScanner = true;
                    cameraReady = false;
                    statusEl.innerText = 'Запрашиваем камеру...';
                    document.getElementById('start-camera').style.display = 'none';
                    try {
                        const cameras = await Html5Qrcode.getCameras();
                        if (!cameras || cameras.length === 0) {
                            statusEl.innerText = 'Камера не найдена';
                            startingScanner = false;
                            return;
                        }
                        cameraWrap.classList.remove('hidden');
                        cameraSelect.innerHTML = '';
                        cameras.forEach((cam, idx) => {
                            const opt = document.createElement('option');
                            opt.value = cam.id;
                            opt.textContent = cam.label || `Камера ${idx + 1}`;
                            cameraSelect.appendChild(opt);
                        });
                        if (cameraId) {
                            cameraSelect.value = cameraId;
                        }
                        // Prefer back camera when available
                        const preferred = cameras.find(c => /back|rear|environment/i.test(c.label || ''));
                        const selectedId = cameraSelect.value || preferred?.id || cameras[0].id;
                        const config = { fps: 10, qrbox: { width: 240, height: 240 } };
                        if (html5Scanner) {
                            try { await html5Scanner.stop(); } catch (e) {}
                        }
                        html5Scanner = new Html5Qrcode("reader");
                        await html5Scanner.start(selectedId, config, onScanSuccess);
                        statusEl.innerText = 'Наведите камеру на QR-код';
                        cameraReady = true;
                    } catch (err) {
                        statusEl.innerText = 'Разрешите доступ к камере и попробуйте снова';
                        document.getElementById('start-camera').style.display = 'block';
                    } finally {
                        startingScanner = false;
                        document.getElementById('start-camera').style.display = 'block';
                    }
                }

                function onScanSuccess(decodedText) {
                    statusEl.innerText = 'Код распознан, проверяем...';
                    if (decodedText.trim() === ACCESS_CODE) {
                        socket.emit('login', { code: decodedText.trim(), user_id: userId });
                    } else {
                        statusEl.innerText = 'Неверный QR-код';
                    }
                }

                function setSessionState(active) {
                    sessionActive = active;
                    if (active) {
                        overlay.classList.add('hidden');
                        sessionBadge.innerText = 'Доступ активен';
                        sessionBadge.classList.remove('gray');
                        sessionBadge.classList.add('green');
                        startGeoLoop();
                    } else {
                        overlay.classList.remove('hidden');
                        sessionBadge.innerText = 'Не авторизован';
                        sessionBadge.classList.add('gray');
                        sessionBadge.classList.remove('green');
                        stopGeoLoop();
                        resetMapState();
                        statusEl.innerText = 'Нажмите «Включить камеру», чтобы начать сканирование.';
                        if (html5Scanner) {
                            html5Scanner.stop().catch(() => {});
                        }
                    }
                }

                function startGeoLoop() {
                    if (geoTimer) return;
                    geoTimer = setInterval(() => {
                        if (!sessionActive) return;
                        navigator.geolocation.getCurrentPosition((pos) => {
                            const { latitude, longitude } = pos.coords;
                            socket.emit('location_update', { user_id: userId, lat: latitude, lng: longitude });
                        }, () => {}, { enableHighAccuracy: true, timeout: 5000, maximumAge: 0 });
                    }, 2000);
                }

                function stopGeoLoop() {
                    if (geoTimer) {
                        clearInterval(geoTimer);
                        geoTimer = null;
                    }
                }

                function resetMapState() {
                    Object.values(userMarkers).forEach(m => m.remove());
                    userMarkers = {};
                    dangerMarkers.forEach(m => m.remove());
                    dangerMarkers = [];
                }

                function renderDangers(points) {
                    dangerMarkers.forEach(m => m.remove());
                    dangerMarkers = points.map(p => {
                        const color = p.type === 'infected' ? '#e63946' : (p.type === 'mask' ? '#1d4ed8' : '#777');
                        const marker = L.circleMarker([p.lat, p.lng], {
                            radius: 10,
                            color,
                            weight: 2,
                            fillColor: color,
                            fillOpacity: 0.35
                        }).addTo(map);
                        const imgHtml = p.image ? `<div style="margin-top:8px;"><img src="${p.image}" alt="Фото" style="max-width:180px;border-radius:10px;box-shadow:0 6px 16px rgba(0,0,0,0.12);" /></div>` : '';
                        marker.bindPopup(`<strong>${labelForType(p.type)}</strong><br>${p.desc || 'Без комментария'}${imgHtml}`);
                        return marker;
                    });
                }

                function labelForType(type) {
                    if (type === 'infected') return 'Заражённые';
                    if (type === 'mask') return 'Масочники';
                    return 'Другие';
                }

                function renderLocations(data) {
                    Object.entries(data).forEach(([uid, pos]) => {
                        const color = uid === userId ? '#2a9d8f' : '#0ea5e9';
                        if (!userMarkers[uid]) {
                            userMarkers[uid] = L.circleMarker([pos.lat, pos.lng], {
                                radius: uid === userId ? 9 : 7,
                                color,
                                fillColor: color,
                                fillOpacity: 0.6,
                                weight: 2
                            }).addTo(map).bindTooltip(uid, { permanent: false });
                        } else {
                            userMarkers[uid].setLatLng([pos.lat, pos.lng]);
                        }
                    });
                }

                const toBase64 = (file) => {
                    return new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = (error) => reject(error);
                        reader.readAsDataURL(file);
                    });
                };

                document.getElementById('submit-add').addEventListener('click', async () => {
                    if (!addLatLng || !sessionActive) return;
                    const type = document.getElementById('danger-type').value;
                    const desc = document.getElementById('danger-desc').value.trim();
                    const fileInput = document.getElementById('danger-photo');
                    const file = fileInput.files[0];
                    let image = null;
                    if (file) {
                        if (file.size > 2 * 1024 * 1024) {
                            alert('Размер файла не должен превышать 2 МБ');
                            return;
                        }
                        try {
                            image = await toBase64(file);
                        } catch (e) {
                            alert('Не удалось прочитать файл');
                            return;
                        }
                    }
                    socket.emit('add_danger', { user_id: userId, lat: addLatLng.lat, lng: addLatLng.lng, type, desc, image });
                    fileInput.value = '';
                    document.getElementById('danger-desc').value = '';
                    closeAddForm();
                });

                document.getElementById('cancel-add').addEventListener('click', () => closeAddForm());

                cameraSelect.addEventListener('change', () => {
                    if (cameraReady || html5Scanner) {
                        startScanner(cameraSelect.value);
                    }
                });

                document.getElementById('start-camera').addEventListener('click', () => {
                    startScanner(cameraSelect.value);
                });

                socket.on('connect', () => {
                    // Register presence for session rooms
                    socket.emit('register', { user_id: userId });
                });

                socket.on('login_result', (payload) => {
                    if (payload.success) {
                        statusEl.innerText = 'Доступ предоставлен';
                        setSessionState(true);
                        socket.emit('request_initial');
                    } else {
                        statusEl.innerText = payload.message || 'Ошибка авторизации';
                        setSessionState(false);
                    }
                });

                socket.on('session_expired', () => {
                    alert('Доступ истёк. Необходимо повторно отсканировать QR-код.');
                    setSessionState(false);
                });

                socket.on('update_dangers', (payload) => {
                    renderDangers(payload.points || []);
                });

                socket.on('locations', (payload) => {
                    renderLocations(payload.locations || {});
                });

                socket.on('initial_state', (payload) => {
                    renderDangers(payload.points || []);
                    renderLocations(payload.locations || {});
                });

                function init() {
                    initMap();
                }

                init();
            </script>
        </body>
        </html>
        """,
        access_code=ACCESS_CODE,
        session_lifetime=SESSION_LIFETIME,
    )


@socketio.on('register')
def register(data):
    user_id = (data or {}).get('user_id')
    if user_id:
        join_room(user_id)


@socketio.on('login')
def handle_login(data):
    user_id = (data or {}).get('user_id')
    code = (data or {}).get('code')
    if not user_id:
        emit('login_result', {'success': False, 'message': 'Отсутствует идентификатор'})
        return
    if code != ACCESS_CODE:
        emit('login_result', {'success': False, 'message': 'Неверный QR-код'})
        return
    mark_session(user_id)
    join_room(user_id)
    notify_admin_async(f"Пользователь {user_id} вошел с кодом {ACCESS_CODE}")
    emit('login_result', {'success': True})
    with danger_lock:
        points_copy = list(danger_points)
    with location_lock:
        locations_copy = dict(user_locations)
    emit('initial_state', {'points': points_copy, 'locations': locations_copy})


def ensure_session_or_expire(user_id: str) -> bool:
    if not user_id:
        return False
    if not session_valid(user_id):
        socketio.emit('session_expired', room=user_id)
        return False
    return True


@socketio.on('location_update')
def handle_location(data):
    user_id = (data or {}).get('user_id')
    lat = (data or {}).get('lat')
    lng = (data or {}).get('lng')
    if not ensure_session_or_expire(user_id):
        return
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return
    with location_lock:
        user_locations[user_id] = {'lat': lat, 'lng': lng, 'ts': time.time()}
    socketio.emit('locations', {'locations': user_locations})


@socketio.on('add_danger')
def handle_add_danger(data):
    user_id = (data or {}).get('user_id')
    if not ensure_session_or_expire(user_id):
        return
    lat = (data or {}).get('lat')
    lng = (data or {}).get('lng')
    dtype = (data or {}).get('type')
    desc = (data or {}).get('desc', '')
    image = (data or {}).get('image')
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return
    if dtype not in {'infected', 'mask', 'other'}:
        dtype = 'other'
    if image and isinstance(image, str):
        # Basic size guard (~3MB of base64 text)
        if len(image) > 4_000_000:
            image = None
    else:
        image = None
    point = {'lat': lat, 'lng': lng, 'type': dtype, 'desc': desc, 'timestamp': time.time(), 'image': image}
    with danger_lock:
        danger_points.append(point)
        points_copy = list(danger_points)
    socketio.emit('update_dangers', {'points': points_copy})


@socketio.on('request_initial')
def handle_request_initial():
    with danger_lock:
        points_copy = list(danger_points)
    with location_lock:
        locations_copy = dict(user_locations)
    emit('initial_state', {'points': points_copy, 'locations': locations_copy})


if __name__ == '__main__':
    start_background_tasks()
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
