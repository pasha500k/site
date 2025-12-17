import time
import threading
import datetime
import hashlib
import asyncio
import os
import uuid
import logging
import json
import sqlite3
import urllib.parse

# Отключаем лишние логи
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('socketio').setLevel(logging.ERROR)
logging.getLogger('engineio').setLevel(logging.ERROR)

from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory
from flask_socketio import SocketIO, emit, disconnect

# AIOGRAM
from aiogram import Bot, Dispatcher, types
from aiogram.types import URLInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret_safe_key_123'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

DEFAULT_TELEGRAM_BOT_TOKEN = "8522303446:AAGwzzKZF-vbCArx-_D5NJCG_A0b1KS-KIo"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or DEFAULT_TELEGRAM_BOT_TOKEN


def _parse_admin_ids():
    env_value = os.getenv("TELEGRAM_ADMIN_IDS") or os.getenv("TELEGRAM_ADMIN_ID")
    admins: list[int] = [8258050467, 944178740]
    if not env_value:
        return admins

    for raw in env_value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            admins.append(int(raw))
        except ValueError:
            logging.warning("Ignore invalid TELEGRAM_ADMIN_ID value: %s", raw)
    return list(dict.fromkeys(admins))


TELEGRAM_ADMIN_IDS = _parse_admin_ids()
BOT_ENABLED = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS)

bot: Bot | None = None
dp = Dispatcher()
bot_loop = None

if BOT_ENABLED:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
else:
    logging.warning("Telegram bot is disabled because TELEGRAM_BOT_TOKEN or TELEGRAM_ADMIN_ID is not set.")

# Константы
DEBUG_MODE = True  # <--- ВКЛЮЧАЕТ ДОСТУП К /debug
SESSION_DURATION = 2 * 60 * 60
POINT_DURATION = 7 * 60
DAILY_SALT = "LetovoCorpSecretSalt"
TOKENS_FILE = "tokens.json"
DB_FILE = "safemap.db"

# Хранилище (в памяти)
valid_tokens = {}
sessions = {}
users = {}
dangers = []
buildings = []

# Настройки (загружаются из БД)
categories_config = {
    'danger': [],
    'building': []
}
access_codes = []

current_daily_code = ""
current_date_str = ""
daily_code_initialized = False

# ==========================================
# 1. БАЗА ДАННЫХ
# ==========================================

def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    c = conn.cursor()
    # Данные карты
    c.execute('''CREATE TABLE IF NOT EXISTS dangers
                 (id TEXT PRIMARY KEY, lat REAL, lng REAL, type TEXT, desc TEXT, ts REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS buildings
                 (id TEXT PRIMARY KEY, lat REAL, lng REAL, name TEXT, type TEXT, desc TEXT, coords TEXT)''')

    # Настройки категорий
    c.execute('''CREATE TABLE IF NOT EXISTS categories
                 (id TEXT PRIMARY KEY, group_type TEXT, name TEXT, color TEXT)''')

    # Дополнительные коды доступа
    c.execute('''CREATE TABLE IF NOT EXISTS access_codes
                 (code TEXT PRIMARY KEY, desc TEXT)''')

    conn.commit()
    conn.close()

    # Загружаем дефолтные категории, если пусто
    check_defaults()

def check_defaults():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM categories")
    if c.fetchone()[0] == 0:
        defaults = [
            ('infected', 'danger', '☣️ Заражённый', '#ef4444'),
            ('masked', 'danger', '😷 Масочник', '#2563eb'),
            ('other', 'danger', '❓ Другое', '#7f8c8d'),
            ('base', 'building', '🏠 База', '#ff9f43'),
            ('field', 'building', '⚽ Поле', '#2ecc71'),
            ('zone', 'building', '🚧 Зона', '#e74c3c'),
            ('storage', 'building', '📦 Склад', '#9b59b6')
        ]
        c.executemany("INSERT INTO categories VALUES (?,?,?,?)", defaults)
        conn.commit()
        print("[DB] Default categories created.")
    conn.close()

def load_data_from_db():
    global dangers, buildings, categories_config, access_codes
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Опасности
        c.execute("SELECT * FROM dangers")
        dangers = [dict(row) for row in c.fetchall()]

        # Здания
        c.execute("SELECT * FROM buildings")
        raw_buildings = c.fetchall()
        buildings = []
        for row in raw_buildings:
            b = dict(row)
            if b.get('coords'):
                try:
                    b['coords'] = json.loads(b['coords'])
                except Exception:
                    b['coords'] = []
            buildings.append(b)

        # Категории
        c.execute("SELECT * FROM categories")
        cats = c.fetchall()
        categories_config['danger'] = [dict(row) for row in cats if row['group_type'] == 'danger']
        categories_config['building'] = [dict(row) for row in cats if row['group_type'] == 'building']

        # Коды
        c.execute("SELECT * FROM access_codes")
        custom_codes = [dict(row) for row in c.fetchall()]
        # Обновляем глобальный список
        access_codes = custom_codes

        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Load failed: {e}")

def db_exec(query, args=()):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(query, args)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return False

def add_danger_to_db(obj):
    db_exec("INSERT INTO dangers VALUES (?,?,?,?,?,?)",
            (obj['id'], obj['lat'], obj['lng'], obj['type'], obj['desc'], obj['ts']))

def add_building_to_db(obj):
    coords_json = json.dumps(obj.get('coords', []))
    db_exec("INSERT INTO buildings (id, lat, lng, name, type, desc, coords) VALUES (?,?,?,?,?,?,?)",
            (obj['id'], obj['lat'], obj['lng'], obj['name'], obj['type'], obj['desc'], coords_json))


def delete_building_from_db(id):
    db_exec("DELETE FROM buildings WHERE id = ?", (id,))

def cleanup_db_dangers(cutoff):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM dangers WHERE ts < ?", (cutoff,))
    count = c.rowcount
    conn.commit()
    conn.close()
    return count

def load_tokens():
    global valid_tokens
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, 'r') as f:
                valid_tokens = json.load(f)
        except Exception:
            valid_tokens = {}

def save_tokens():
    try:
        with open(TOKENS_FILE, 'w') as f:
            json.dump(valid_tokens, f)
    except Exception:
        pass

init_db()
load_data_from_db()
load_tokens()

# ==========================================
# 2. TELEGRAM & AUTH
# ==========================================

def get_daily_code():
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    raw = f"{today}-{DAILY_SALT}"
    code = str(int(hashlib.md5(raw.encode()).hexdigest(), 16))[-12:]
    return today, code

async def send_tg_async(today, code, target_ids):
    if not BOT_ENABLED or not bot:
        logging.info("Skipping Telegram notification: bot disabled")
        return
    try:
        safe_code = urllib.parse.quote(code)
        msg_text = f"🔐 *SafeMap Access*\n📅 Дата: `{today}`\n🔢 Код: `{code}`"
        barcode_url = f"https://bwipjs-api.metafloor.com/?bcid=code128&text={safe_code}&scale=3&rotate=N&includetext&background=ffffff"
        qr_url = f"https://bwipjs-api.metafloor.com/?bcid=qrcode&text={safe_code}&scale=3&rotate=N&background=ffffff"

        for admin_id in target_ids:
            await bot.send_message(admin_id, msg_text, parse_mode=ParseMode.MARKDOWN)
            await bot.send_photo(admin_id, photo=URLInputFile(barcode_url), caption="📷 *Штрих-код*", parse_mode=ParseMode.MARKDOWN)
            await bot.send_photo(admin_id, photo=URLInputFile(qr_url), caption="🔳 *QR-код*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

@dp.message(Command("code"))
async def cmd_code(msg: types.Message):
    if not BOT_ENABLED or not bot:
        await msg.answer("Бот выключен: не настроены TELEGRAM_BOT_TOKEN / TELEGRAM_ADMIN_ID")
        return
    if msg.from_user.id in TELEGRAM_ADMIN_IDS:
        target_admin = [msg.from_user.id]
        await msg.answer("Отправляю актуальный код вам в личные сообщения.")
        await send_tg_async(current_date_str, current_daily_code, target_admin)
    else:
        await msg.answer("Недостаточно прав.")


def send_tg_sync(t, c, targets):
    if bot_loop:
        asyncio.run_coroutine_threadsafe(send_tg_async(t, c, targets), bot_loop)


def update_code():
    global current_daily_code, current_date_str
    t, c = get_daily_code()
    if t != current_date_str:
        current_date_str = t
        current_daily_code = c
        send_tg_sync(t, c, TELEGRAM_ADMIN_IDS)


def init_daily_code():
    """Set today's code without sending a message so startup is silent."""
    global current_daily_code, current_date_str, daily_code_initialized
    if daily_code_initialized:
        return
    current_date_str, current_daily_code = get_daily_code()
    daily_code_initialized = True


init_daily_code()


def bot_thread_func():
    if not BOT_ENABLED or not bot:
        logging.info("Telegram bot thread not started (disabled)")
        return
    global bot_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_loop = loop
    try:
        loop.run_until_complete(dp.start_polling(bot, handle_signals=False))
    except Exception as exc:
        logging.error("Telegram bot polling failed: %s", exc)


if BOT_ENABLED:
    threading.Thread(target=bot_thread_func, daemon=True).start()

# ==========================================
# 3. BACKGROUND TASKS
# ==========================================

def bg_task():
    global dangers, sessions, users
    time.sleep(5)
    while True:
        ts = time.time()
        update_code()

        if cleanup_db_dangers(ts - POINT_DURATION) > 0:
            load_data_from_db()
            socketio.emit('update_dangers', dangers)

        expired = [t for t, created in valid_tokens.items() if ts - created > SESSION_DURATION]
        if expired:
            for t in expired:
                del valid_tokens[t]
            save_tokens()

        to_kick = [s for s, t in sessions.items() if t not in valid_tokens]
        for s in to_kick:
            socketio.emit('session_expired', room=s)
            sessions.pop(s, None)
            users.pop(s, None)
            socketio.emit('update_users', users)

        time.sleep(10)

threading.Thread(target=bg_task, daemon=True).start()

# ==========================================
# 4. ШАБЛОНЫ
# ==========================================

DEBUG_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>SafeMap DEBUG</title>
    <style>
        body { font-family: sans-serif; background: #222; color: #fff; padding: 20px; }
        h1, h2 { color: #ff4422; }
        .section { background: #333; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 8px; border-bottom: 1px solid #444; text-align: left; vertical-align: middle; }
        input, select, button { padding: 8px; border-radius: 5px; border: none; margin: 2px; }
        button { cursor: pointer; font-weight: bold; }
        .btn-green { background: #2ecc71; color: white; }
        .btn-red { background: #e74c3c; color: white; }
        .code-display { font-family: monospace; font-size: 1.2em; background: #111; padding: 5px 10px; border-radius: 5px; }
        .color-box { display: inline-block; width: 15px; height: 15px; border: 1px solid white; vertical-align: middle; }
        .qr-img { background: white; padding: 5px; border-radius: 5px; height: 50px; }
    </style>
</head>
<body>
    <h1>🛠️ SafeMap DEBUG Panel</h1>
    <a href="/" style="color:#aaa;">← Вернуться на карту</a>

    <div class="section">
        <h2>🔑 Коды доступа</h2>
        <table>
            <tr><th>Тип</th><th>Код</th><th>Описание</th><th>QR</th><th>Действие</th></tr>
            <tr>
                <td>📅 Daily</td>
                <td><span class="code-display">{{ daily_code }}</span></td>
                <td>Автоматический (меняется раз в сутки)</td>
                <td><img src="https://bwipjs-api.metafloor.com/?bcid=qrcode&text={{ daily_code }}&scale=3" class="qr-img"></td>
                <td>-</td>
            </tr>
            {% for c in custom_codes %}
            <tr>
                <td>🔧 Custom</td>
                <td><span class="code-display">{{ c.code }}</span></td>
                <td>{{ c.desc }}</td>
                <td><img src="https://bwipjs-api.metafloor.com/?bcid=qrcode&text={{ c.code }}&scale=3" class="qr-img"></td>
                <td>
                    <button class="btn-red" onclick="delCode('{{ c.code }}')">Удалить</button>
                </td>
            </tr>
            {% endfor %}
        </table>
        <div style="margin-top:10px;">
            <input id="new_code" placeholder="Новый код (напр. ADMIN)">
            <input id="new_code_desc" placeholder="Описание">
            <button class="btn-green" onclick="addCode()">Добавить код</button>
        </div>
    </div>

    <div class="section">
        <h2>🏷️ Категории (Метки и Здания)</h2>
        <table>
            <tr><th>ID</th><th>Тип</th><th>Название</th><th>Цвет</th><th>Действие</th></tr>
            {% for cat in categories %}
            <tr>
                <td>{{ cat.id }}</td>
                <td>{{ '🏢 Здание' if cat.group_type == 'building' else '⚠️ Метка' }}</td>
                <td>{{ cat.name }}</td>
                <td><span class="color-box" style="background:{{ cat.color }}"></span> {{ cat.color }}</td>
                <td><button class="btn-red" onclick="delCat('{{ cat.id }}')">Удалить</button></td>
            </tr>
            {% endfor %}
        </table>
        <div style="margin-top:10px;">
            <input id="cat_id" placeholder="ID (лат, напр. car)">
            <select id="cat_type">
                <option value="danger">⚠️ Временная метка</option>
                <option value="building">🏢 Постоянное здание</option>
            </select>
            <input id="cat_name" placeholder="Название (напр. Машина)">
            <input id="cat_color" type="color">
            <button class="btn-green" onclick="addCat()">Добавить категорию</button>
        </div>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
    <script>
        const socket = io();
        function addCode() {
            const c = document.getElementById('new_code').value;
            const d = document.getElementById('new_code_desc').value;
            if(c) socket.emit('debug_add_code', {code:c, desc:d});
        }
        function delCode(c) {
            if(confirm('Удалить код?')) socket.emit('debug_del_code', {code:c});
        }
        function addCat() {
            const id = document.getElementById('cat_id').value;
            const type = document.getElementById('cat_type').value;
            const name = document.getElementById('cat_name').value;
            const color = document.getElementById('cat_color').value;
            if(id && name) socket.emit('debug_add_cat', {id, type, name, color});
        }
        function delCat(id) {
            if(confirm('Удалить категорию?')) socket.emit('debug_del_cat', {id});
        }
        socket.on('debug_refresh', () => location.reload());
    </script>
</body>
</html>
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SafeMap | LETOVO CORP.</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
    <style>
        :root { --p: #ff4422; --d: #121212; --w: #fff; --g: rgba(255,255,255,0.85); --rad: 16px; }
        body { margin: 0; font-family: 'Inter', sans-serif; overflow: hidden; background: #eee; }
        #login-overlay { position: fixed; top:0; left:0; width:100%; height:100%; background: #111; z-index: 9999; display: flex; align-items: center; justify-content: center; flex-direction: column; }
        .card { background: var(--w); padding: 30px; border-radius: 24px; width: 90%; max-width: 350px; text-align: center; }
        #reader { width: 100%; height: 250px; background: #000; border-radius: 12px; margin-bottom: 15px; }
        .btn { background: var(--d); color: var(--w); border: none; padding: 12px; border-radius: 10px; width: 100%; font-weight: 600; cursor: pointer; margin-top: 10px; }
        #map { height: 100vh; width: 100vw; }
        .logo { position: absolute; top: 20px; left: 20px; z-index: 1000; }
        .logo img { height: 50px; width: auto; }
        .tools-panel { position: absolute; top: 80px; left: 20px; z-index: 1000; display: flex; flex-direction: column; gap: 10px; }
        .tool-btn { width: 44px; height: 44px; background: var(--w); border-radius: 10px; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 10px rgba(0,0,0,0.2); cursor: pointer; color: #333; font-size: 18px; transition: 0.2s; }
        .tool-btn.active { background: var(--p); color: #fff; }
        #save-poly-btn { display: none; background: #2ecc71; color: white; animation: popIn 0.3s; }
        @keyframes popIn { from { transform: scale(0); } to { transform: scale(1); } }
        #modal { position: fixed; bottom: -100%; left: 0; width: 100%; background: var(--g); backdrop-filter: blur(15px); padding: 25px; border-radius: 25px 25px 0 0; box-sizing: border-box; transition: 0.3s; z-index: 2000; }
        #modal.open { bottom: 0; }
        .tabs { display: flex; gap: 10px; margin-bottom: 15px; background: rgba(0,0,0,0.05); padding: 5px; border-radius: 12px; }
        .tab { flex: 1; padding: 10px; text-align: center; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; color: #666; }
        .tab.active { background: #fff; color: #000; shadow: 0 2px 5px rgba(0,0,0,0.1); }
        input, select { width: 100%; padding: 14px; margin-bottom: 10px; border-radius: 10px; border: 1px solid #ccc; box-sizing: border-box; }
        .actions { display: flex; gap: 10px; margin-top: 10px; }
        .ui-panel { position: absolute; top: 20px; right: 20px; background: var(--g); padding: 10px 15px; border-radius: 12px; z-index: 1000; font-size: 12px; backdrop-filter: blur(10px); }
        .legend-item { display: flex; align-items: center; margin-bottom: 5px; }
        .dot { width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }
        .pulse { width: 14px; height: 14px; background: #10b981; border: 3px solid #fff; border-radius: 50%; box-shadow: 0 0 0 rgba(16,185,129,0.4); animation: p 2s infinite; }
        .other { width: 10px; height: 10px; background: #333; border: 2px solid #fff; border-radius: 50%; }
        .build-icon { border: 2px solid #fff; width: 14px; height: 14px; border-radius: 3px; box-shadow: 0 2px 5px rgba(0,0,0,0.3); }
        @keyframes p { 0% { box-shadow: 0 0 0 0 rgba(16,185,129,0.7); } 70% { box-shadow: 0 0 0 10px transparent; } }
        .leaflet-control-layers { border-radius: 12px; border: none; box-shadow: 0 4px 10px rgba(0,0,0,0.2); font-family: 'Inter'; font-weight: 600; }
        
        /* CREDITS FOOTER */
        .credits-panel {
            position: absolute; bottom: 10px; right: 10px;
            z-index: 900;
            font-size: 10px;
            color: rgba(0, 0, 0, 0.5);
            text-align: right;
            font-weight: 600;
            text-shadow: 0 0 2px rgba(255,255,255,0.8);
            pointer-events: none;
        }
    </style>
</head>
<body>

<div id="login-overlay">
    <div class="card">
        <h2>SafeMap</h2>
        <div id="reader"></div>
        <p id="log-stat">Сканируйте код</p>
        <button class="btn" onclick="document.getElementById('f').click()">📁 Загрузить фото</button>
        <input type="file" id="f" style="display:none" onchange="loadFile(this)">
    </div>
</div>
<div id="hidden-qr" style="display:none"></div>

<div id="map"></div>

<!-- CREDITS -->
<div class="credits-panel">
    Создатель: Павел<br>
    денежная поддержка PeninsulaPatrol
</div>

<div class="logo">
    <img src="/logo.png" alt="Логотип">
</div>

    <div class="ui-panel">
        <div id="legend-content"></div>
        <div style="margin-top:5px; color:#888;">Live Sync</div>
    </div>

<div class="tools-panel">
    <div class="tool-btn" id="ruler-btn" onclick="toggleRuler()" title="Рулетка"><i class="fas fa-ruler-combined"></i></div>
    <div class="tool-btn" id="save-poly-btn" onclick="openPolyModal()" title="Сохранить зону"><i class="fas fa-save"></i></div>
</div>

<div id="modal">
    <h3 id="modal-title" style="margin:0 0 15px;">Добавить объект</h3>
    <div class="tabs" id="modal-tabs">
        <div class="tab active" onclick="setMode('danger')" id="tab-danger">⚠️ Временный</div>
        <div class="tab" onclick="setMode('building')" id="tab-build" style="display:none">🏢 Здание</div>
    </div>

    <div id="form-danger">
        <select id="d-type"></select>
        <input id="d-desc" placeholder="Комментарий (7 мин)">
    </div>

    <div id="form-build" style="display:none">
        <input id="b-name" placeholder="Название">
        <select id="b-type"></select>
        <input id="b-desc" placeholder="Описание">
    </div>

    <div class="actions">
        <button class="btn" style="background:#ddd; color:#000" onclick="closeModal()">Отмена</button>
        <button class="btn" onclick="submitItem()">Добавить</button>
    </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
<script src="https://unpkg.com/html5-qrcode" type="text/javascript"></script>

<script>
    const socket = io();
    const IS_DEBUG = {{ debug|tojson }};
    const CATS = {{ categories|tojson }};
    
    let map;
    let markers = { users: {}, dangers: [], buildings: [], polygons: [] };
    let tempCoords = null;
    let activeMode = 'danger';
    let isAuth = false;
    let isPolySaveMode = false;
    let rulerMode = false, rulerPoints = [], rulerLine = null, rulerPopup = null;
    let currentBuildings = [];

    function setC(n,v){ document.cookie=n+"="+v+";path=/;max-age=7200"; }
    function getC(n){ let m=document.cookie.match(new RegExp("(^| )"+n+"=([^;]+)")); return m?m[2]:null; }

    window.onload = function() {
        renderUI();
        if (IS_DEBUG) document.getElementById('tab-build').style.display = 'block';
        const scanner = new Html5QrcodeScanner("reader", { fps:10, qrbox:250 });
        scanner.render((t)=>{ socket.emit('login',{code:t}) });
        const tok = getC('sm_token');
        if(tok) socket.emit('restore_session', {token:tok});
    };

    function renderUI() {
        const leg = document.getElementById('legend-content');
        leg.innerHTML = '';
        CATS.danger.forEach(c => {
            leg.innerHTML += `<div class="legend-item"><span class="dot" style="background:${c.color}"></span> ${c.name}</div>`;
        });
        const dSel = document.getElementById('d-type');
        dSel.innerHTML = '';
        CATS.danger.forEach(c => dSel.innerHTML += `<option value="${c.id}">${c.name}</option>`);
        const bSel = document.getElementById('b-type');
        bSel.innerHTML = '';
        CATS.building.forEach(c => bSel.innerHTML += `<option value="${c.id}">${c.name}</option>`);
    }

    function loadFile(inp) {
        if(!inp.files.length) return;
        const sc = new Html5Qrcode("hidden-qr");
        sc.scanFile(inp.files[0], true).then(t => socket.emit('login',{code:t})).catch(()=>alert("Нет кода"));
    }

    socket.on('login_response', (d) => {
        if(d.success) { setC('sm_token', d.token); startApp(); }
        else alert("Неверный код");
    });
    
    socket.on('restore_response', (d) => {
        if(d.success) startApp();
        else document.getElementById('login-overlay').style.display='flex';
    });

    function startApp() {
        document.getElementById('login-overlay').style.display='none';
        isAuth = true;
        initMap();
        if(navigator.geolocation) {
            navigator.geolocation.watchPosition(p => {
                socket.emit('update_location', {lat:p.coords.latitude, lng:p.coords.longitude});
            }, null, {enableHighAccuracy:true});
        }
    }

    function initMap() {
        if(map) return;
        const streets = L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', { maxZoom: 19 });
        const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 });
        map = L.map('map', {zoomControl:false, attributionControl:false, layers: [streets]}).setView([55.75, 37.61], 13);
        if (IS_DEBUG) L.control.layers({"Карта": streets, "Спутник": satellite}, null, {position: 'topright'}).addTo(map);
        map.on('click', (e) => {
            if (rulerMode) handleRulerClick(e.latlng);
            else {
                isPolySaveMode = false;
                tempCoords = e.latlng;
                document.getElementById('modal-title').innerText = "Добавить объект";
                document.getElementById('modal-tabs').style.display = 'flex';
                setMode('danger');
                document.getElementById('modal').classList.add('open');
            }
        });
    }

    function getColor(id, type) {
        const list = type === 'danger' ? CATS.danger : CATS.building;
        const item = list.find(x => x.id === id);
        return item ? item.color : '#777';
    }

    socket.on('update_users', (data) => {
        if(!isAuth) return;
        for(let id in markers.users) { if(!data[id]) { map.removeLayer(markers.users[id]); delete markers.users[id]; } }
        for(let id in data) {
            const u = data[id];
            if(markers.users[id]) markers.users[id].setLatLng([u.lat, u.lng]);
            else {
                const isMe = id === socket.id;
                if(isMe) map.setView([u.lat, u.lng], 16); 
                const cls = isMe ? 'pulse' : 'other';
                const icon = L.divIcon({className:'x', html:`<div class="${cls}"></div>`, iconSize:[14,14]});
                markers.users[id] = L.marker([u.lat, u.lng], {icon}).addTo(map);
            }
        }
    });

    socket.on('update_dangers', (list) => {
        if(!isAuth) return;
        markers.dangers.forEach(l => map.removeLayer(l));
        markers.dangers = [];
        list.forEach(d => {
            let col = getColor(d.type, 'danger');
            const c = L.circleMarker([d.lat, d.lng], {radius:10, fillColor:col, color:'white', weight:2, fillOpacity:0.9}).addTo(map);
            c.bindPopup(`<b>${d.type}</b><br>${d.desc||''}`);
            markers.dangers.push(c);
        });
    });

    socket.on('update_buildings', (list) => {
        if(!isAuth) return;
        currentBuildings = list;
        markers.buildings.forEach(l => map.removeLayer(l));
        markers.polygons.forEach(l => map.removeLayer(l));
        markers.buildings = [];
        markers.polygons = [];
        list.forEach(b => {
            let col = getColor(b.type, 'building');
            let popupContent = `<b>🏢 ${b.name}</b><br>${b.desc||''}`;
            if (IS_DEBUG) popupContent += `<br><button onclick="deleteObject('${b.id}')" style="margin-top:5px;background:#e74c3c;color:white;border:none;padding:4px 8px;border-radius:4px;cursor:pointer;width:100%;font-size:11px;">Удалить</button>`;

            if (b.coords && b.coords.length > 2) {
                const poly = L.polygon(b.coords, {color: col, weight: 2, fillColor: col, fillOpacity: 0.2}).addTo(map);
                const center = poly.getBounds().getCenter();
                const icon = L.divIcon({className: 'b-label', html: `<div style="text-shadow:0 0 3px #fff; font-weight:800; font-size:11px; text-align:center; color:${col};">${b.name}</div>`, iconSize: [100, 20], iconAnchor: [50, 10]});
                const label = L.marker(center, {icon, interactive:false}).addTo(map);
                poly.bindPopup(popupContent);
                markers.polygons.push(poly); markers.polygons.push(label);
            } else {
                const icon = L.divIcon({className: 'b-icon', html: `<div class="build-icon" style="background:${col}"></div><div style="font-size:10px; font-weight:bold; text-align:center; background:white; padding:1px 3px; border-radius:4px; margin-top:2px;">${b.name}</div>`, iconSize: [20, 20], iconAnchor: [10, 10]});
                const m = L.marker([b.lat, b.lng], {icon}).addTo(map);
                m.bindPopup(popupContent);
                markers.buildings.push(m);
            }
        });
    });

    window.setMode = function(m) {
        activeMode = m;
        document.getElementById('tab-danger').className = m=='danger'?'tab active':'tab';
        document.getElementById('tab-build').className = m=='building'?'tab active':'tab';
        document.getElementById('form-danger').style.display = m=='danger'?'block':'none';
        document.getElementById('form-build').style.display = m=='building'?'block':'none';
    }
    window.closeModal = function() { document.getElementById('modal').classList.remove('open'); }
    window.openPolyModal = function() {
        if (rulerPoints.length < 3) { alert("Нужно минимум 3 точки!"); return; }
        isPolySaveMode = true;
        document.getElementById('modal-tabs').style.display = 'none';
        document.getElementById('modal-title').innerText = "Сохранить зону";
        setMode('building');
        document.getElementById('modal').classList.add('open');
    }
    window.submitItem = function() {
        if (isPolySaveMode) {
            const center = rulerLine.getBounds().getCenter();
            socket.emit('add_building', { lat: center.lat, lng: center.lng, name: document.getElementById('b-name').value || 'Зона', type: document.getElementById('b-type').value, desc: document.getElementById('b-desc').value, coords: rulerPoints });
            toggleRuler();
        } else {
            if(!tempCoords) return;
            if(activeMode === 'danger') {
                socket.emit('add_danger', { lat: tempCoords.lat, lng: tempCoords.lng, type: document.getElementById('d-type').value, desc: document.getElementById('d-desc').value });
            } else {
                socket.emit('add_building', { lat: tempCoords.lat, lng: tempCoords.lng, name: document.getElementById('b-name').value || 'Здание', type: document.getElementById('b-type').value, desc: document.getElementById('b-desc').value });
            }
        }
        closeModal();
    }

    window.deleteObject = function(id) {
        if(confirm("Удалить объект?")) socket.emit('delete_building', { id: id });
    }

    window.toggleRuler = function() {
        rulerMode = !rulerMode;
        const btn = document.getElementById('ruler-btn');
        const saveBtn = document.getElementById('save-poly-btn');
        if(rulerMode) { btn.classList.add('active'); rulerPoints = []; clearRulerLayers(); }
        else { btn.classList.remove('active'); saveBtn.style.display = 'none'; clearRulerLayers(); rulerPoints = []; isPolySaveMode = false; }
    }
    function clearRulerLayers() { if(rulerLine) map.removeLayer(rulerLine); if(rulerPopup) map.removeLayer(rulerPopup); }
    function handleRulerClick(latlng) {
        rulerPoints.push(latlng);
        if(rulerLine) map.removeLayer(rulerLine);
        if (rulerPoints.length > 1) {
            rulerLine = L.polyline(rulerPoints, {color: 'black', dashArray: '5, 10'}).addTo(map);
            let dist = 0;
            for(let i=0; i<rulerPoints.length-1; i++) dist += rulerPoints[i].distanceTo(rulerPoints[i+1]);
            if(rulerPopup) map.removeLayer(rulerPopup);
            rulerPopup = L.popup().setLatLng(latlng).setContent(`<b>${Math.round(dist)} м</b>`).openOn(map);
            if (IS_DEBUG && rulerPoints.length > 2) document.getElementById('save-poly-btn').style.display = 'flex';
        } else {
            rulerLine = L.circleMarker(latlng, {radius:3, color:'black'}).addTo(map);
        }
    }
</script>
</body>
</html>
"""

# ==========================================
# 5. SERVER HANDLERS
# ==========================================

@app.route('/')
def index():
    # Объединяем категории для передачи в шаблон
    all_cats = categories_config
    return render_template_string(HTML_TEMPLATE, debug=DEBUG_MODE, categories=all_cats)

@app.route('/debug')
def debug_route():
    if not DEBUG_MODE:
        return "Debug mode is OFF", 403
    # Объединяем все категории в один список для таблицы
    flat_cats = categories_config['danger'] + categories_config['building']
    return render_template_string(DEBUG_TEMPLATE, categories=flat_cats, daily_code=current_daily_code, custom_codes=access_codes)

@app.route('/logo.png')
def serve_logo():
    if os.path.exists('logo.png'):
        return send_from_directory('.', 'logo.png')
    return '', 404

@socketio.on('login')
def on_login(d):
    sid = request.sid
    code = d.get('code', '').strip()

    # Проверка: или это код дня, или это один из кастомных кодов
    is_valid = (code == current_daily_code) or any(c['code'] == code for c in access_codes)

    if is_valid:
        t = str(uuid.uuid4())
        valid_tokens[t] = time.time()
        sessions[sid] = t
        save_tokens()
        emit('login_response', {'success': True, 'token': t})
        emit('update_users', users)
        emit('update_dangers', dangers)
        emit('update_buildings', buildings)
    else:
        emit('login_response', {'success': False})


@socketio.on('restore_session')
def on_restore(d):
    sid, t = request.sid, d.get('token')
    if t in valid_tokens:
        sessions[sid] = t
        emit('restore_response', {'success': True})
        emit('update_users', users)
        emit('update_dangers', dangers)
        emit('update_buildings', buildings)
    else:
        emit('restore_response', {'success': False})


@socketio.on('update_location')
def on_loc(d):
    sid = request.sid
    if sid in sessions:
        users[sid] = {'lat': d['lat'], 'lng': d['lng'], 'updated': time.time()}
        emit('update_users', users, broadcast=True)


@socketio.on('add_danger')
def on_add_danger(d):
    if request.sid in sessions:
        obj = {'id': str(time.time()), 'lat': d['lat'], 'lng': d['lng'], 'type': d['type'], 'desc': d['desc'], 'ts': time.time()}
        dangers.append(obj)
        add_danger_to_db(obj)
        emit('update_dangers', dangers, broadcast=True)


@socketio.on('add_building')
def on_add_building(d):
    if not DEBUG_MODE:
        return
    if request.sid in sessions:
        obj = {
            'id': str(uuid.uuid4()),
            'lat': d['lat'], 'lng': d['lng'],
            'name': d['name'], 'type': d['type'], 'desc': d['desc'],
            'coords': d.get('coords', [])
        }
        buildings.append(obj)
        add_building_to_db(obj)
        emit('update_buildings', buildings, broadcast=True)


@socketio.on('delete_building')
def on_delete_building(d):
    if not DEBUG_MODE:
        return
    global buildings
    bid = d.get('id')
    buildings = [b for b in buildings if b['id'] != bid]
    delete_building_from_db(bid)
    emit('update_buildings', buildings, broadcast=True)
    


# DEBUG HANDLERS
@socketio.on('debug_add_cat')
def on_debug_add_cat(d):
    if not DEBUG_MODE:
        return
    db_exec("INSERT INTO categories VALUES (?,?,?,?)", (d['id'], d['type'], d['name'], d['color']))
    load_data_from_db()
    emit('debug_refresh')


@socketio.on('debug_del_cat')
def on_debug_del_cat(d):
    if not DEBUG_MODE:
        return
    db_exec("DELETE FROM categories WHERE id = ?", (d['id'],))
    load_data_from_db()
    emit('debug_refresh')


@socketio.on('debug_add_code')
def on_debug_add_code(d):
    if not DEBUG_MODE:
        return
    db_exec("INSERT INTO access_codes VALUES (?,?)", (d['code'], d['desc']))
    load_data_from_db()
    emit('debug_refresh')


@socketio.on('debug_del_code')
def on_debug_del_code(d):
    if not DEBUG_MODE:
        return
    db_exec("DELETE FROM access_codes WHERE code = ?", (d['code'],))
    load_data_from_db()
    emit('debug_refresh')


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in users:
        users.pop(sid)
        emit('update_users', users, broadcast=True)


if __name__ == '__main__':
    cert, key = 'cert.pem', 'key.pem'
    ctx = (cert, key) if os.path.exists(cert) and os.path.exists(key) else 'adhoc'
    print(f"Starting on 5000 with {ctx}. DEBUG_MODE={DEBUG_MODE}")
    try:
        socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True, ssl_context=ctx)
    except Exception:
        socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
