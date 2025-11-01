# -*- coding: utf-8 -*-
"""Single-file video site with account-backed likes and favorites."""
import os
import re
import json
import base64
import random
import tempfile
import subprocess
import asyncio
import time
import hashlib
import hmac
import sqlite3
from collections import deque
from functools import wraps
from typing import List, Dict, Tuple, Optional

from flask import (
    Flask, render_template_string, request, url_for, abort,
    Response, send_file, redirect, jsonify, make_response, stream_with_context,
    session, g
)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APP_SECRET_KEY", "change-me")

# -------------------------
# CONFIG
# -------------------------
VIDEO_ROOT    = r"C:\Users\pavel\PycharmProjects\PH_Dowloader_TG+WEB\downloads"
PREVIEW_ROOT  = os.path.join(VIDEO_ROOT, "__previews__")
ALLOWED_EXT   = {".mp4"}

ADMIN_SECRET_PLAIN = os.environ.get("ADMIN_DEFAULT_PASSWORD", "Hehetoto123")
ADMIN_USERNAME = os.environ.get("ADMIN_DEFAULT_USERNAME", "admin")

# секрет для подписи access-токенов (HMAC)
ACCESS_SIGN_SECRET = (ADMIN_SECRET_PLAIN + "::access").encode("utf-8")
ACCESS_TOKEN_TTL_SEC = 10 * 60  # 10 минут

DATABASE_PATH = os.path.join(VIDEO_ROOT, "app.db")

os.makedirs(PREVIEW_ROOT, exist_ok=True)
os.makedirs(VIDEO_ROOT, exist_ok=True)

# -------------------------
# METADATA FILES
# -------------------------
TRANSL_CACHE_PATH = os.path.join(VIDEO_ROOT, "translations.json")
DUR_CACHE_PATH    = os.path.join(VIDEO_ROOT, "durations.json")
VIEWS_PATH        = os.path.join(VIDEO_ROOT, "views.json")
PROTECTED_PATH    = os.path.join(VIDEO_ROOT, "protected_folders.json")  # {"rel/path": "sha256_hash"}

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_trans_cache: Dict[str, str] = load_json(TRANSL_CACHE_PATH, {})
_dur_cache: Dict[str, float]  = load_json(DUR_CACHE_PATH, {})
_views: Dict[str, int]        = load_json(VIEWS_PATH, {})
_protected = load_json(PROTECTED_PATH, {})

# in-memory buckets for lightweight rate limiting / bot protection
_rate_buckets: Dict[str, deque] = {}


def save_trans_cache(): save_json(TRANSL_CACHE_PATH, _trans_cache)

def save_dur_cache():   save_json(DUR_CACHE_PATH, _dur_cache)

def save_views():       save_json(VIEWS_PATH, _views)

def save_protected():   save_json(PROTECTED_PATH, _protected)


# -------------------------
# DATABASE
# -------------------------

def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reactions (
                user_id INTEGER NOT NULL,
                video_path TEXT NOT NULL,
                reaction TEXT NOT NULL CHECK (reaction IN ('like','dislike')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, video_path),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                video_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, video_path),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS view_events (
                fingerprint TEXT NOT NULL,
                video_path TEXT NOT NULL,
                user_id INTEGER,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(fingerprint, video_path),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS folder_access (
                user_id INTEGER NOT NULL,
                folder_path TEXT NOT NULL,
                granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, folder_path),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        conn.commit()
        cur = conn.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
                (ADMIN_USERNAME, generate_password_hash(ADMIN_SECRET_PLAIN))
            )
            conn.commit()
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _rate_key(prefix: str) -> str:
    ident = request.remote_addr or "unknown"
    user = getattr(g, "user", None)
    if user is not None:
        ident = f"user:{user['id']}"
    return f"{prefix}:{ident}"


def allow_rate(prefix: str, limit: int, window_sec: int) -> bool:
    key = _rate_key(prefix)
    bucket = _rate_buckets.setdefault(key, deque())
    now = time.time()
    while bucket and now - bucket[0] > window_sec:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    # prevent unbounded growth in long-running processes
    while len(bucket) > limit:
        bucket.popleft()
    return True


def viewer_identity(resp=None) -> Tuple[str, Optional[int]]:
    user = getattr(g, "user", None)
    if user is not None:
        return f"user:{user['id']}", user["id"]
    uid = get_user_cookie(resp)
    return f"anon:{uid}", None


def register_view_if_new(video_path: str, resp=None) -> bool:
    fingerprint, user_id = viewer_identity(resp)
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM view_events WHERE fingerprint = ? AND video_path = ?",
        (fingerprint, video_path)
    ).fetchone()
    if row:
        return False
    db.execute(
        "INSERT INTO view_events (fingerprint, video_path, user_id) VALUES (?, ?, ?)",
        (fingerprint, video_path, user_id)
    )
    db.commit()
    return True


def user_has_persistent_access(scope_path: Optional[str]) -> bool:
    user = getattr(g, "user", None)
    if not scope_path or user is None:
        return False
    normalized = scope_path.strip("/")
    if not normalized:
        return False
    parts = normalized.split("/")
    db = get_db()
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i])
        row = db.execute(
            "SELECT 1 FROM folder_access WHERE user_id = ? AND folder_path = ?",
            (user["id"], candidate)
        ).fetchone()
        if row:
            return True
    return False


def remember_folder_access(folder_path: str) -> None:
    user = getattr(g, "user", None)
    if user is None:
        return
    normalized = folder_path.strip("/")
    if not normalized:
        return
    db = get_db()
    db.execute(
        "INSERT INTO folder_access (user_id, folder_path) VALUES (?, ?) "
        "ON CONFLICT(user_id, folder_path) DO NOTHING",
        (user["id"], normalized)
    )
    db.commit()


def clear_folder_access(folder_path: str) -> None:
    normalized = folder_path.strip("/")
    if not normalized:
        return
    db = get_db()
    db.execute("DELETE FROM folder_access WHERE folder_path = ?", (normalized,))
    db.commit()


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        db = get_db()
        row = db.execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row:
            g.user = {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}
        else:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.url))
        return view(**kwargs)
    return wrapped_view


init_db()

# -------------------------
# UI TEXTS
# -------------------------
UI_TEXT = {
    "en": {
        "root": "Root", "categories": "Categories", "videos": "Videos",
        "search_placeholder": "Search the whole site...", "random": "Random",
        "random_settings": "Random settings", "random_from": "Pick folders for Random",
        "save": "Save", "download": "Download", "back": "Back", "delete": "Delete",
        "checkfix": "Check & Fix", "similar_here": "Similar in this folder",
        "similar_global": "From other folders", "not_found": "No videos in this folder.",
        "nothing_found": "Nothing found.", "lang_btn": "RUS", "title_main": "Categories",
        "favorites": "Favorites", "views": "views", "liked": "Liked", "disliked": "Disliked",
        "saved_ok": "Saved", "fixed_ok": "Checked / fixed", "like": "Like", "dislike": "Dislike",
        "locked": "Locked", "enter_pass": "Enter password", "open": "Open", "wrong_pass": "Wrong password",
        "quality": "Quality", "original": "Original", "login": "Login", "logout": "Logout",
        "register": "Register", "account": "Account", "too_many_attempts": "Too many attempts, try later"
    },
    "ru": {
        "root": "Корень", "categories": "Категории", "videos": "Видео",
        "search_placeholder": "Поиск по всему сайту...", "random": "Случайное",
        "random_settings": "Настройки рандома", "random_from": "Выбери папки для «Случайного»",
        "save": "Сохранить", "download": "Скачать", "back": "Назад", "delete": "Удалить",
        "checkfix": "Проверить/исправить", "similar_here": "Похожие из этой категории",
        "similar_global": "Из других категорий", "not_found": "Нет видео в этой папке.",
        "nothing_found": "Ничего не найдено.", "lang_btn": "EN", "title_main": "Категории",
        "favorites": "Избранное", "views": "просмотров", "liked": "Нравится", "disliked": "Не нравится",
        "saved_ok": "Сохранено", "fixed_ok": "Проверено/исправлено", "like": "Лайк", "dislike": "Дизлайк",
        "locked": "Закрыта", "enter_pass": "Введите пароль", "open": "Открыть", "wrong_pass": "Неверный пароль",
        "quality": "Качество", "original": "Оригинал", "login": "Войти", "logout": "Выйти",
        "register": "Регистрация", "account": "Аккаунт", "too_many_attempts": "Слишком много попыток, попробуйте позже"
    }
}

# -------------------------
# TRANSLATOR (googletrans) with retries
# -------------------------
translator = None
try:
    from googletrans import Translator
    translator = Translator(service_urls=['translate.googleapis.com','translate.google.com'])
except Exception:
    translator = None


def contains_cyrillic(text: str) -> bool:
    return any('А' <= ch <= 'я' or ch in 'Ёё' for ch in text)


def contains_latin(text: str) -> bool:
    return any('A' <= ch <= 'Z' or 'a' <= ch <= 'z' for ch in text)


def split_name_ext(name: str) -> Tuple[str, str]:
    base, ext = os.path.splitext(name)
    return base, ext


def should_translate_title(name: str, lang: str) -> bool:
    if lang not in {"ru", "en"}:
        return False
    base, _ = split_name_ext(name)
    if not base.strip():
        return False
    if lang == "ru":
        return contains_latin(base) and not contains_cyrillic(base)
    if lang == "en":
        return contains_cyrillic(base) and not contains_latin(base)
    return False


def cached_translation(name: str, lang: str) -> Optional[str]:
    entry = _trans_cache.get(name)
    if isinstance(entry, dict):
        return entry.get(lang)
    if isinstance(entry, str) and lang == "ru":
        return entry
    return None


def cache_translation(name: str, lang: str, translated: str) -> None:
    entry = _trans_cache.get(name)
    if isinstance(entry, dict):
        entry[lang] = translated
    elif isinstance(entry, str):
        if lang == "ru":
            _trans_cache[name] = translated
        else:
            _trans_cache[name] = {"ru": entry, lang: translated}
    else:
        if lang == "ru":
            _trans_cache[name] = translated
        else:
            _trans_cache[name] = {lang: translated}
    save_trans_cache()


def perform_translation(text: str, dest_lang: str) -> Optional[str]:
    if not translator:
        return None
    for _ in range(3):
        try:
            result = translator.translate(text, dest=dest_lang)
            if asyncio.iscoroutine(result):
                try:
                    result = asyncio.get_event_loop().run_until_complete(result)
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(result)
                    loop.close()
            translated = (getattr(result, "text", None) or "").strip()
            if translated:
                return translated
        except Exception:
            continue
    return None


def translate_title_if_needed(name: str, lang: str) -> str:
    cached = cached_translation(name, lang)
    if cached:
        return cached
    if not should_translate_title(name, lang):
        return name
    base, ext = split_name_ext(name)
    translated_base = perform_translation(base, lang) or base
    translated = f"{translated_base}{ext}"
    cache_translation(name, lang, translated)
    return translated

# -------------------------
# ACCESS TOKEN HELPERS (no cookies, HMAC query)
# -------------------------
def hmac_b64(data: str) -> str:
    sig = hmac.new(ACCESS_SIGN_SECRET, data.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def mk_access_signature(scope_path: str, exp_ts: int) -> str:
    payload = f"{scope_path}|{exp_ts}"
    return hmac_b64(payload)


def verify_access_signature(scope_path: str, exp_ts: str, sig: str) -> bool:
    try:
        exp_i = int(exp_ts)
    except Exception:
        return False
    if exp_i < int(time.time()):
        return False
    expected = mk_access_signature(scope_path, exp_i)
    def norm(x: str) -> str: return (x or "").rstrip("=")
    return hmac.compare_digest(norm(expected), norm(sig))


def get_protected_root_for(rel_path: str) -> Optional[str]:
    parts = rel_path.split("/")
    for i in range(len(parts), 0, -1):
        p = "/".join(parts[:i])
        if p in _protected:
            return p
    return None


def with_grant(url: str, scope: Optional[str]) -> str:
    if not scope:
        return url
    if is_admin_request() or user_has_persistent_access(scope):
        return url
    exp = int(time.time()) + ACCESS_TOKEN_TTL_SEC
    sig = mk_access_signature(scope, exp)
    delim = "&" if ("?" in url) else "?"
    return f"{url}{delim}exp={exp}&sig={sig}"


def is_admin_request(req=None) -> bool:
    user = getattr(g, "user", None)
    if user and user.get("is_admin"):
        return True
    return False


# -------------------------
# UTILITIES
# -------------------------
def safe_join(root: str, subpath: str) -> str:
    full = os.path.abspath(os.path.join(root, subpath))
    if not full.startswith(os.path.abspath(root)):
        abort(403)
    return full


def get_lang() -> Tuple[str, Dict[str, str]]:
    lang = (request.cookies.get("lang") or "ru").lower()
    if lang not in ("ru","en"):
        lang = "ru"
    return lang, UI_TEXT[lang]


def get_user_cookie(resp=None) -> str:
    uid = request.cookies.get("uid")
    if uid:
        return uid
    uid = base64.b16encode(os.urandom(8)).decode().lower()
    if resp is None:
        resp = make_response()
    resp.set_cookie("uid", uid, max_age=60*60*24*365, path="/")
    return uid


def ffprobe_duration(video_path: str) -> float:
    rel = os.path.relpath(video_path, VIDEO_ROOT).replace("\\","/")
    if rel in _dur_cache:
        return _dur_cache[rel]
    try:
        out = subprocess.check_output(
            ["ffprobe","-v","error","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1", video_path],
            stderr=subprocess.STDOUT
        )
        dur = float(out.strip())
    except Exception:
        dur = 60.0
    _dur_cache[rel] = dur
    save_dur_cache()
    return dur


def probe_video_size(video_path: str) -> Tuple[int,int]:
    """Return (width,height) using ffprobe; fallback (1280,720)."""
    try:
        out = subprocess.check_output(
            ["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height",
             "-of","csv=p=0:s=x", video_path],
            stderr=subprocess.STDOUT
        )
        s = out.decode().strip()
        if "x" in s:
            w,h = s.split("x")
            return int(w), int(h)
    except Exception:
        pass
    return 1280, 720


def format_duration(seconds: float) -> str:
    s = int(round(seconds))
    h = s // 3600; s %= 3600
    m = s // 60; s %= 60
    if h > 0: return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def thumbnail_timestamp(video_path: str) -> float:
    duration = ffprobe_duration(video_path)
    if duration <= 0:
        return 1.0
    midpoint = duration / 2.0
    return max(0.0, midpoint)


def generate_thumbnail(video_path: str) -> str:
    thumb_path = os.path.splitext(video_path)[0] + ".jpg"
    if not os.path.exists(thumb_path):
        ts = thumbnail_timestamp(video_path)
        try:
            subprocess.run(
                [
                    "ffmpeg","-y","-ss",f"{ts:.3f}","-i",video_path,
                    "-frames:v","1","-q:v","2",thumb_path
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
    return thumb_path

# preview settings
PREVIEW_CLIPS = 3
PREVIEW_CLIP_SEC = 2.5
PREVIEW_WIDTH = 720


def ensure_preview(video_path: str) -> str:
    rel = os.path.relpath(video_path, VIDEO_ROOT)
    rel_preview = os.path.splitext(rel)[0] + ".preview.mp4"
    preview_path = os.path.join(PREVIEW_ROOT, rel_preview)
    os.makedirs(os.path.dirname(preview_path), exist_ok=True)
    if os.path.exists(preview_path):
        return preview_path

    dur = max(10.0, ffprobe_duration(video_path))
    positions = [0.20, 0.50, 0.80]
    starts = [max(0.0, dur * p - PREVIEW_CLIP_SEC/2) for p in positions][:PREVIEW_CLIPS]

    tmpdir = tempfile.mkdtemp(dir=os.path.dirname(preview_path))
    segs = []
    try:
        for i, st in enumerate(starts, 1):
            seg = os.path.join(tmpdir, f"seg{i}.mp4")
            subprocess.run(
                ["ffmpeg","-y","-ss",str(st),"-t",str(PREVIEW_CLIP_SEC),"-i",video_path,
                 "-vf",f"scale={PREVIEW_WIDTH}:-2:flags=bicubic",
                 "-an","-c:v","libx264","-preset","veryfast","-crf","23",
                 "-movflags","+faststart", seg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if os.path.exists(seg):
                segs.append(seg)
        concat_list = os.path.join(tmpdir,"list.txt")
        with open(concat_list,"w",encoding="utf-8") as f:
            for p in segs:
                f.write("file '{}'\n".format(p.replace('\\','/')))
        subprocess.run(
            ["ffmpeg","-y","-f","concat","-safe","0","-i",concat_list,"-c","copy","-movflags","+faststart", preview_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    finally:
        try:
            for p in segs:
                if os.path.exists(p): os.remove(p)
            if os.path.exists(concat_list): os.remove(concat_list)
            os.rmdir(tmpdir)
        except Exception:
            pass

    return preview_path

# -------------------------
# Directory walkers
# -------------------------
def list_subfolders(dir_abs: str) -> List[Dict]:
    res = []
    if not os.path.isdir(dir_abs):
        return res
    for name in os.listdir(dir_abs):
        if name.startswith("__"):
            continue
        full = os.path.join(dir_abs, name)
        if os.path.isdir(full) and os.path.abspath(full) != os.path.abspath(PREVIEW_ROOT):
            rel = os.path.relpath(full, VIDEO_ROOT).replace("\\","/")
            disp_name = name
            if rel in _protected:
                disp_name = f"🔒 {name}"
            res.append({"name": disp_name, "path": rel, "raw_path": rel})
    res.sort(key=lambda x: x["name"].lower())
    return res


def is_allowed_video_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in ALLOWED_EXT


def build_video_entry(full_path: str, lang: str) -> Optional[Dict]:
    if not os.path.isfile(full_path):
        return None
    name = os.path.basename(full_path)
    if not is_allowed_video_file(name):
        return None
    rel = os.path.relpath(full_path, VIDEO_ROOT).replace("\\", "/")
    display_name = translate_title_if_needed(name, lang)
    duration = format_duration(ffprobe_duration(full_path))
    thumb = os.path.relpath(generate_thumbnail(full_path), VIDEO_ROOT).replace("\\", "/")
    return {
        "name": name,
        "display": display_name,
        "path": rel,
        "thumb": thumb,
        "duration": duration
    }


def list_videos_in_dir(dir_abs: str, lang: str) -> List[Dict]:
    res: List[Dict] = []
    if not os.path.isdir(dir_abs):
        return res
    for name in os.listdir(dir_abs):
        if name.startswith("__"):
            continue
        full = os.path.join(dir_abs, name)
        if os.path.isdir(full):
            continue
        entry = build_video_entry(full, lang)
        if entry:
            res.append(entry)
    res.sort(key=lambda x: x["display"].lower())
    return res


def iter_video_files(root_dir: str):
    for dirpath, dirs, files in os.walk(root_dir):
        rel_dir = os.path.relpath(dirpath, root_dir)
        if "__previews__" in rel_dir.split(os.sep):
            continue
        for filename in files:
            if is_allowed_video_file(filename):
                yield os.path.join(dirpath, filename)


def list_all_videos(lang: str) -> List[Dict]:
    res: List[Dict] = []
    for full_path in iter_video_files(VIDEO_ROOT):
        entry = build_video_entry(full_path, lang)
        if entry:
            res.append(entry)
    return res


def list_all_top_dirs() -> List[str]:
    out = []
    for name in os.listdir(VIDEO_ROOT):
        full = os.path.join(VIDEO_ROOT, name)
        if os.path.isdir(full) and not name.startswith("__") and os.path.abspath(full) != os.path.abspath(PREVIEW_ROOT):
            out.append(name)
    return sorted(out, key=str.lower)


def breadcrumbs_for(subpath: str) -> List[Dict[str, str]]:
    crumbs = []
    if not subpath: return crumbs
    parts = subpath.split("/")
    for i in range(len(parts)):
        p = "/".join(parts[:i+1])
        crumbs.append({"name": parts[i], "path": p})
    return crumbs


# -------------------------
# Quality helpers (ladder)
# -------------------------
COMMON_HEIGHTS = [2160, 1440, 1080, 1024, 960, 900, 864, 846, 810, 768, 720, 704, 576, 540, 480, 432, 404, 360, 288, 240, 180]


def available_heights_for(video_path: str) -> List[int]:
    w,h = probe_video_size(video_path)
    # отдаём только те высоты, что не больше источника, и не меньше 180
    hs = [x for x in COMMON_HEIGHTS if 180 <= x <= h]
    # гарантируем включение «почти исходной» высоты, даже если нестандартная (например 854)
    if 180 <= h and h not in hs:
        hs = sorted(set(hs + [h]), reverse=True)
    return hs


def ffmpeg_stream_cmd(input_path: str, height: int) -> List[str]:
    # Для совместимости: H.264 + AAC, фрагментированный MP4
    vf = f"scale=-2:{height}:flags=bicubic"
    return [
        "ffmpeg","-hide_banner","-loglevel","error","-nostdin",
        "-reconnect","1","-reconnect_streamed","1","-reconnect_on_network_error","1",
        "-i", input_path,
        "-vf", vf, "-pix_fmt","yuv420p",
        "-c:v","libx264","-preset","veryfast","-crf","22",
        "-c:a","aac","-b:a","160k",
        "-movflags","+frag_keyframe+empty_moov+faststart",
        "-f","mp4","pipe:1"
    ]


# -------------------------
# Player mode (always custom improved)
# -------------------------
def get_player_mode():
    return "custom"


# -------------------------
# Helpers for reactions / favorites
# -------------------------
def reaction_counts(paths: List[str]) -> Dict[str, Dict[str, int]]:
    res = {p: {"likes": 0, "dislikes": 0} for p in paths}
    if not paths:
        return res
    db = get_db()
    placeholders = ",".join(["?"] * len(paths))
    rows = db.execute(
        f"""
        SELECT video_path,
               SUM(CASE WHEN reaction='like' THEN 1 ELSE 0 END) AS likes,
               SUM(CASE WHEN reaction='dislike' THEN 1 ELSE 0 END) AS dislikes
        FROM reactions
        WHERE video_path IN ({placeholders})
        GROUP BY video_path
        """,
        paths
    ).fetchall()
    for row in rows:
        res[row["video_path"]] = {
            "likes": row["likes"] or 0,
            "dislikes": row["dislikes"] or 0
        }
    return res


def user_reaction_for(path: str) -> Optional[str]:
    if g.user is None:
        return None
    db = get_db()
    row = db.execute(
        "SELECT reaction FROM reactions WHERE user_id = ? AND video_path = ?",
        (g.user["id"], path)
    ).fetchone()
    return row["reaction"] if row else None


def is_favorite(path: str) -> bool:
    if g.user is None:
        return False
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM favorites WHERE user_id = ? AND video_path = ?",
        (g.user["id"], path)
    ).fetchone()
    return bool(row)


def enrich_cards_with_stats(videos: List[Dict]):
    paths = [v["path"] for v in videos]
    counts = reaction_counts(paths)
    for v in videos:
        path = v["path"]
        c = counts.get(path, {"likes": 0, "dislikes": 0})
        v["views"] = _views.get(path, 0)
        v["likes"] = c.get("likes", 0)
        v["dislikes"] = c.get("dislikes", 0)

# -------------------------
# TEMPLATES
# -------------------------
TEMPLATE_ACCESS = """<!doctype html>
<html lang="{{ 'ru' if lang=='ru' else 'en' }}">
<head>
<meta charset="utf-8">
<title>🔒 {{ ui['locked'] }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{background:#0d1117;color:#e6edf3;font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#11151b;padding:26px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35);max-width:360px;width:100%}
h2{margin:0 0 12px}
input{width:100%;margin-top:10px;padding:10px;border-radius:10px;border:1px solid #2a3440;background:#151b23;color:#e6edf3}
button{margin-top:12px;width:100%;background:#238636;color:#fff;border:0;padding:10px;border-radius:10px;cursor:pointer}
button:hover{background:#2ea043}
.msg{color:#ff6666;margin-top:8px}
a{color:#58a6ff;text-decoration:none}
</style>
</head>
<body>
<div class=\"card\">
  <h2>🔒 {{ path }}</h2>
  <form method=\"post\">
    <input type=\"password\" name=\"password\" placeholder=\"{{ ui['enter_pass'] }}\" required>
    <button type=\"submit\">{{ ui['open'] }}</button>
  </form>
  {% if error %}<div class=\"msg\">{{ error }}</div>{% endif %}
  <div style=\"margin-top:10px\"><a href=\"{{ url_for('browse', subpath='') }}\">← {{ ui['back'] }}</a></div>
</div>
</body>
</html>
"""

TEMPLATE_MAIN = """<!doctype html>
<html lang="{{ 'ru' if lang=='ru' else 'en' }}">
<head>
<meta charset="utf-8">
<title>🎬 {{ ui['title_main'] }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
:root{--bg:#0d1117;--card:#11151b;--card2:#151b23;--text:#e6edf3;--muted:#9aa4b2;--link:#58a6ff;--accent:#238636;--accent2:#2ea043;--shadow:0 10px 30px rgba(0,0,0,.35)}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;margin:0}
.container{max-width:100%;margin:auto;padding:30px 50px}
.header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}
.breadcrumbs{font-size:14px;color:var(--muted)}
.breadcrumbs a{color:var(--link);text-decoration:none}
h1{margin:0;font-weight:800;font-size:22px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.input{background:#151b23;border:1px solid #242c37;color:var(--text);padding:11px 14px;border-radius:12px;min-width:280px;outline:none;box-shadow:var(--shadow)}
.button{background:var(--accent);color:#fff;border:0;padding:11px 14px;border-radius:12px;text-decoration:none;display:inline-flex;gap:8px;align-items:center;box-shadow:var(--shadow);transition:.25s}
.button:hover{background:var(--accent2);transform:scale(1.03)}
.lang{background:#30363d}
.section-title{margin:18px 0 10px;font-size:18px;font-weight:800}
.grid{display:grid;gap:22px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--card);border-radius:16px;overflow:hidden;transition:.25s;box-shadow:var(--shadow)}
.card:hover{transform:scale(1.02);background:var(--card2)}
.thumb-wrap{position:relative}
.thumb, video.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;border-radius:12px;background:#000}
.dur{position:absolute;right:10px;bottom:10px;background:rgba(0,0,0,.7);color:#fff;padding:2px 6px;border-radius:8px;font-size:12px}
.meta{display:flex;justify-content:center;gap:14px;margin:6px 0 2px;color:#9aa4b2;font-size:12px}
.title{padding:6px 12px 12px;font-size:15px;text-align:center;font-weight:600;color:var(--text);min-height:46px;display:flex;align-items:center;justify-content:center}
.muted{color:var(--muted)}
.lock{font-size:12px;color:#9aa4b2;margin-left:6px}
.hidden{display:none!important}
@media(max-width:900px){.container{padding:20px}.grid{gap:16px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}}
.badge{display:inline-flex;align-items:center;gap:6px;padding:10px 12px;border-radius:12px;background:#1c2129;color:var(--muted)}
</style>

<script>
let aborter=null;
async function doSearch(q){
  const sectionCats=document.getElementById('section-cats');
  const gridVids=document.getElementById('grid-vids');
  if(!q){
    sectionCats.classList.remove('hidden');
    gridVids.innerHTML=window.__initialVideosHTML||gridVids.innerHTML;
    setupPreviews();
    return;
  }
  sectionCats.classList.add('hidden');
  try{
    if(aborter) aborter.abort();
    aborter=new AbortController();
    const res=await fetch('/api/search?q='+encodeURIComponent(q),{signal:aborter.signal});
    const data=await res.json();
    gridVids.innerHTML=(data.results||[]).map(v=>`
      <div class=\"card\">
        <a href=\"/watch/${v.path}\">
          <div class=\"thumb-wrap\">
            <video class=\"thumb hovervid\" muted playsinline preload=\"none\"
                   poster=\"/files/${v.thumb}\" data-preview=\"/preview/${v.path}\"></video>
            <div class=\"dur\">${v.duration||''}</div>
          </div>
          <div class=\"meta\">
            <span>👁 ${v.views||0}</span>
            <span>👍 ${v.likes||0}</span>
            <span>👎 ${v.dislikes||0}</span>
          </div>
          <div class=\"title\">${v.display||v.name}</div>
        </a>
      </div>`).join('') || '<p class=\"muted\">{{ ui["nothing_found"] }}</p>';
    setupPreviews();
  }catch(e){}
}
function setupPreviews(){
  const isTouch=('ontouchstart' in window)||(navigator.maxTouchPoints>0);
  const vids=document.querySelectorAll('video.hovervid');
  if(!isTouch){
    vids.forEach(v=>{
      v.addEventListener('mouseenter',()=>{ if(!v.src){v.src=v.dataset.preview;} v.currentTime=0; v.play().catch(()=>{}); });
      v.addEventListener('mouseleave',()=>{ v.pause(); v.removeAttribute('src'); v.load(); });
    });
  }else{
    const io=new IntersectionObserver((entries)=>{
      entries.forEach(entry=>{
        const v=entry.target;
        if(entry.isIntersecting && entry.intersectionRatio>0.6){
          if(!v.src){ v.src=v.dataset.preview; }
          v.currentTime=0; v.play().catch(()=>{});
        }else{
          v.pause(); v.removeAttribute('src'); v.load();
        }
      });
    },{threshold:[0,0.25,0.6,1]});
    vids.forEach(v=>io.observe(v));
  }
}
document.addEventListener('DOMContentLoaded',()=>{
  const grid=document.getElementById('grid-vids');
  window.__initialVideosHTML=grid?grid.innerHTML:'';
  setupPreviews();
});
</script>
</head>
<body>
<div class=\"container\">
  <div class=\"header\">
    <div>
      <div class=\"breadcrumbs\">
        {% if crumbs %}
          {% for c in crumbs %}
            {% if not loop.last %}
              <a href=\"{{ url_for('browse', subpath=c.path) }}\">{{ c.name }}</a> /
            {% else %}
              <span class=\"muted\">{{ c.name }}</span>
            {% endif %}
          {% endfor %}
        {% else %}
          <span class=\"muted\">{{ ui['root'] }}</span>
        {% endif %}
      </div>
      <h1>{{ title }}</h1>
    </div>
    <div class=\"toolbar\">
      <input class=\"input\" placeholder=\"{{ ui['search_placeholder'] }}\" oninput=\"doSearch(this.value)\">
      <a class=\"button\" href=\"{{ url_for('random_video') }}\">🎲 {{ ui['random'] }}</a>
      <a class=\"button\" href=\"{{ url_for('random_settings') }}\">🎛 {{ ui['random_settings'] }}</a>
      <a class=\"button\" href=\"{{ url_for('favorites_page') }}\">❤️ {{ ui['favorites'] }}</a>
      {% if current_user %}
        <span class=\"badge\">{{ ui['account'] }}: {{ current_user['username'] }}</span>
        <a class=\"button\" href=\"{{ url_for('logout') }}\">🚪 {{ ui['logout'] }}</a>
      {% else %}
        <a class=\"button\" href=\"{{ url_for('login') }}\">🔐 {{ ui['login'] }}</a>
        <a class=\"button\" href=\"{{ url_for('register') }}\">🆕 {{ ui['register'] }}</a>
      {% endif %}
      <a class=\"button lang\" href=\"{{ url_for('set_lang', code=('en' if lang=='ru' else 'ru')) }}\">{{ ui['lang_btn'] }}</a>
    </div>
  </div>

  <div id=\"section-cats\">
    {% if subfolders %}
      <div class=\"section-title\">{{ ui['categories'] }}</div>
      <div class=\"grid\">
        {% for folder in subfolders %}
          <div class=\"card\">
            {% if folder['raw_path'] in protected and not is_admin %}
              <a href=\"{{ url_for('access_folder', subpath=folder['raw_path']) }}\">
                <div class=\"title\">{{ folder['name'] }} <span class=\"lock\">({{ ui['locked'] }})</span></div>
              </a>
            {% else %}
              <a href=\"{{ url_for('browse', subpath=folder['raw_path']) }}\">
                <div class=\"title\">{{ folder['name'] }}</div>
              </a>
            {% endif %}
          </div>
        {% endfor %}
      </div>
    {% endif %}
  </div>

  <div class=\"section-title\">{{ ui['videos'] }}</div>
  <div id=\"grid-vids\" class=\"grid\">
    {% if videos %}
      {% for v in videos %}
        <div class=\"card\">
          <a href=\"{{ url_for('watch_video', filepath=v['path']) }}\">
            <div class=\"thumb-wrap\">
              <video class=\"thumb hovervid\" muted playsinline preload=\"none\"
                     poster=\"{{ url_for('serve_file', filepath=v['thumb']) }}\"
                     data-preview=\"{{ url_for('preview_file', filepath=v['path']) }}\"></video>
              <div class=\"dur\">{{ v['duration'] }}</div>
            </div>
            <div class=\"meta\">
              <span>👁 {{ v.get('views', 0) }}</span>
              <span>👍 {{ v.get('likes', 0) }}</span>
              <span>👎 {{ v.get('dislikes', 0) }}</span>
            </div>
            <div class=\"title\">{{ v['display'] }}</div>
          </a>
        </div>
      {% endfor %}
    {% else %}
      <p class=\"muted\">{{ ui['not_found'] }}</p>
    {% endif %}
  </div>
</div>
</body>
</html>
"""

TEMPLATE_VIDEO = """<!doctype html>
<html lang="{{ 'ru' if lang=='ru' else 'en' }}">
<head>
<meta charset="utf-8">
<title>{{ video_name }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
:root {
  --bg:#0d1117;
  --card:#11151b;
  --text:#e6edf3;
  --muted:#9aa4b2;
  --shadow:0 10px 30px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{
  background:var(--bg);
  color:var(--text);
  font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;
  margin:0;
}
.container{
  max-width:1200px;
  margin:auto;
  padding:16px;
}
h1{
  margin:8px 0 14px;
  text-align:center;
  font-weight:800;
}
.topbar{
  display:flex;
  gap:10px;
  justify-content:center;
  flex-wrap:wrap;
  margin-bottom:12px;
}
.btn{
  background:#222b35;
  color:#fff;
  border:0;
  border-radius:10px;
  padding:8px 12px;
  text-decoration:none;
  cursor:pointer;
  transition:.2s;
}
.btn:hover{background:#2a3440}
.btn.del{background:#dc3545}
.btn.del:hover{background:#ff4757}
.badge{color:var(--muted)}
.stats{
  display:flex;
  gap:14px;
  justify-content:center;
  margin:12px 0;
  color:var(--muted);
  flex-wrap:wrap;
}
.grid{
  display:grid;
  gap:14px;
  grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  margin-top:18px;
}
.card{
  background:var(--card);
  border-radius:12px;
  overflow:hidden;
  box-shadow:var(--shadow);
  transition:.2s;
}
.card:hover{transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,0,0,.45)}
.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}
.title{padding:8px 10px;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

video {
  width:100%;
  max-height:75vh;
  border-radius:16px;
  background:#000;
  display:block;
}
@media(max-width:900px){
  .container{padding:8px}
  video{max-height:60vh}
}
.disabled{opacity:0.6;cursor:not-allowed}
.active{background:#238636}
</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <a class="btn" href="{{ url_for('set_lang', code=('en' if lang=='ru' else 'ru')) }}">{{ ui['lang_btn'] }}</a>
    <a class="btn" href="{{ random_url }}">🎲 {{ ui['random'] }}</a>
    <a class="btn" href="{{ back_url }}">← {{ ui['back'] }}</a>
    <a class="btn" href="{{ download_original_url }}" download="{{ download_original_name }}">{{ ui['download'] }}</a>
    {% if is_admin %}
      <a class="btn" href="{{ checkfix_url }}" onclick="return confirm('Run check & fix? / Запустить проверку и исправление?')">🩺 {{ ui['checkfix'] }}</a>
      <a class="btn del" href="{{ delete_url }}" onclick="return confirm('Delete? / Удалить?')">🗑 {{ ui['delete'] }}</a>
    {% endif %}
    {% if current_user %}
      <span class="btn" style="pointer-events:none;">{{ ui['account'] }}: {{ current_user['username'] }}</span>
      <a class="btn" href="{{ url_for('logout', next=request_path) }}">🚪 {{ ui['logout'] }}</a>
    {% else %}
      <a class="btn" href="{{ url_for('login', next=request_path) }}">🔐 {{ ui['login'] }}</a>
      <a class="btn" href="{{ url_for('register', next=request_path) }}">🆕 {{ ui['register'] }}</a>
    {% endif %}
  </div>

  <h1>{{ video_name }}</h1>

  <!-- встроенный HTML5 плеер -->
  <video controls preload="metadata" playsinline webkit-playsinline poster="{{ thumb_url if thumb_url else '' }}">
    <source src="{{ file_url }}" type="video/mp4">
    Ваш браузер не поддерживает видео.
  </video>

  <div class="stats">
    <span>👁 <span id="views">{{ views }}</span> {{ ui['views'] }}</span>
    <button class="btn" id="like">👍 <span id="likes">{{ likes }}</span></button>
    <button class="btn" id="dislike">👎 <span id="dislikes">{{ dislikes }}</span></button>
    <button class="btn" id="fav"><span id="favLabel">❤️ {% if fav %}★{% endif %}</span></button>
    <span class="badge">{{ duration }}</span>
  </div>

  <!-- 📂 Похожие видео из этой категории -->
  <h2>📁 {{ ui['similar_here'] }}</h2>
  <div class="grid">
    {% for v in related_same %}
      <div class="card">
        <a href="{{ v['watch_url'] }}">
          <img class="thumb" src="{{ v['thumb_url'] }}" alt="{{ v['display'] }}">
          <div class="title">{{ v['display'] }}</div>
        </a>
      </div>
    {% endfor %}
  </div>

  <!-- 🌍 Похожие видео из других категорий -->
  <h2 style="margin-top:18px">🌍 {{ ui['similar_global'] }}</h2>
  <div class="grid">
    {% for v in related_global %}
      <div class="card">
        <a href="{{ v['watch_url'] }}">
          <img class="thumb" src="{{ v['thumb_url'] }}" alt="{{ v['display'] }}">
          <div class="title">{{ v['display'] }}</div>
        </a>
      </div>
    {% endfor %}
  </div>
</div>
<script>
(function(){
  const likeBtn=document.getElementById('like');
  const dislikeBtn=document.getElementById('dislike');
  const favBtn=document.getElementById('fav');
  const favLabel=document.getElementById('favLabel');
  const loginRedirect={{ url_for('login', next=request_path)|tojson }};
  const registerRedirect={{ url_for('register', next=request_path)|tojson }};
  const stateUrl={{ url_for('api_state')|tojson }};
  const videoPath={{ filepath|tojson }};

  async function refreshState(){
    try{
      const res=await fetch(`${stateUrl}?path=${encodeURIComponent(videoPath)}`);
      if(!res.ok) return;
      const data=await res.json();
      document.getElementById('likes').textContent=data.likes;
      document.getElementById('dislikes').textContent=data.dislikes;
      if(data.favorite){
        favLabel.textContent='❤️ ★';
      }else{
        favLabel.textContent='❤️';
      }
      if(data.user_reaction==='like'){
        likeBtn.classList.add('active');
        dislikeBtn.classList.remove('active');
      }else if(data.user_reaction==='dislike'){
        dislikeBtn.classList.add('active');
        likeBtn.classList.remove('active');
      }else{
        likeBtn.classList.remove('active');
        dislikeBtn.classList.remove('active');
      }
      if(!data.authenticated){
        likeBtn.classList.add('disabled');
        dislikeBtn.classList.add('disabled');
        favBtn.classList.add('disabled');
      }else{
        likeBtn.classList.remove('disabled');
        dislikeBtn.classList.remove('disabled');
        favBtn.classList.remove('disabled');
      }
    }catch(e){console.error(e);}
  }

  async function postJSON(url){
    const res=await fetch(url,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path: videoPath})
    });
    if(res.status===401){
      window.location=loginRedirect;
      return null;
    }
    if(!res.ok){
      return null;
    }
    return res.json();
  }

  likeBtn.addEventListener('click', async()=>{
    if(likeBtn.classList.contains('disabled')){window.location=loginRedirect;return;}
    const data=await postJSON('{{ url_for('api_like') }}');
    if(data){
      document.getElementById('likes').textContent=data.likes;
      document.getElementById('dislikes').textContent=data.dislikes;
      refreshState();
    }
  });

  dislikeBtn.addEventListener('click', async()=>{
    if(dislikeBtn.classList.contains('disabled')){window.location=loginRedirect;return;}
    const data=await postJSON('{{ url_for('api_dislike') }}');
    if(data){
      document.getElementById('likes').textContent=data.likes;
      document.getElementById('dislikes').textContent=data.dislikes;
      refreshState();
    }
  });

  favBtn.addEventListener('click', async()=>{
    if(favBtn.classList.contains('disabled')){window.location=registerRedirect;return;}
    const isFav=favLabel.textContent.includes('★');
    const url=isFav?'{{ url_for('api_unfavorite') }}':'{{ url_for('api_favorite') }}';
    const data=await postJSON(url);
    if(data){
      if(typeof data.favorite!=='undefined'){
        favLabel.textContent=data.favorite?'❤️ ★':'❤️';
      }
      refreshState();
    }
  });

  refreshState();
})();
</script>
</body>
</html>
"""

TEMPLATE_RANDOM_SETTINGS = """<!doctype html>
<html lang="{{ 'ru' if lang=='ru' else 'en' }}">
<head>
<meta charset="utf-8">
<title>🎛 {{ ui['random_settings'] }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{background:#0d1117;color:#e6edf3;font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;margin:0}
.container{max-width:900px;margin:auto;padding:26px}
h1{margin:0 0 16px;font-weight:800}
.card{background:#11151b;border-radius:12px;padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:12px}
.item{background:#151b23;padding:10px;border-radius:10px}
.btn{display:inline-block;margin-top:16px;background:#238636;color:#fff;text-decoration:none;padding:10px 14px;border-radius:10px}
.btn:hover{background:#2ea043}
.badge{color:#9aa4b2;margin-left:8px}
</style>
</head>
<body>
<div class="container">
  <h1>🎛 {{ ui['random_settings'] }}</h1>
  <div class="card">
    <p>{{ ui['random_from'] }} <span class="badge">({{ ui['save'] }} ⇒ cookie)</span></p>
    <form method="post">
      <div class="grid">
        {% for d in top_dirs %}
          <label class="item">
            <input type="checkbox" name="dir" value="{{ d }}" {% if d in selected %}checked{% endif %}>
            <span style="margin-left:6px">{{ d }}</span>
          </label>
        {% endfor %}
      </div>
      <button class="btn" type="submit">✅ {{ ui['save'] }}</button>
      <a class="btn" href="{{ url_for('browse', subpath='') }}" style="background:#30363d;margin-left:6px">← {{ ui['back'] }}</a>
    </form>
  </div>
</div>
</body>
</html>
"""

TEMPLATE_FAVORITES = """<!doctype html>
<html lang="{{ 'ru' if lang=='ru' else 'en' }}">
<head>
<meta charset="utf-8">
<title>❤️ {{ ui['favorites'] }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{background:#0d1117;color:#e6edf3;font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;margin:0}
.container{max-width:1200px;margin:auto;padding:26px}
h1{margin:0 0 16px;font-weight:800}
.grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{background:#11151b;border-radius:12px;overflow:hidden}
.thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}
.title{padding:10px 12px;text-align:center}
.muted{color:#9aa4b2}
.notice{margin-bottom:16px}
</style>
</head>
<body>
<div class="container">
  <h1>❤️ {{ ui['favorites'] }}</h1>
  {% if current_user %}
    <div class="notice">{{ ui['account'] }}: {{ current_user['username'] }}</div>
  {% endif %}
  {% if items %}
    <div class="grid">
      {% for v in items %}
        <div class="card">
          <a href="{{ v['watch_url'] }}">
            <video class="thumb" muted playsinline preload="none"
                   poster="{{ v['thumb_url'] }}"
                   data-preview="{{ v['preview_url'] }}"></video>
            <div class="title">{{ v['display'] }}</div>
          </a>
        </div>
      {% endfor %}
    </div>
  {% else %}
    <p class="muted">—</p>
  {% endif %}
</div>
</body>
</html>
"""

TEMPLATE_PROTECT = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>🔐 Защищённые папки</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{background:#0d1117;color:#e6edf3;font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;margin:0}
.container{max-width:900px;margin:auto;padding:26px}
h1{margin:0 0 16px;font-weight:800}
table{width:100%;border-collapse:collapse;margin-top:10px}
th,td{padding:8px;border-bottom:1px solid #2a3440;text-align:left}
input[type=password]{background:#151b23;color:#e6edf3;border:1px solid #2a3440;border-radius:8px;padding:6px}
button{background:#238636;color:#fff;border:0;padding:6px 10px;border-radius:8px;cursor:pointer}
button:hover{background:#2ea043}
form{display:inline}
.badge{color:#9aa4b2}
a{color:#58a6ff;text-decoration:none}
</style>
</head>
<body>
<div class="container">
  <h1>🔐 Управление доступом к папкам</h1>
  <a href="/" style="color:#58a6ff;text-decoration:none;">← Назад</a>
  <table>
    <tr><th>Папка</th><th>Статус</th><th>Пароль / Действие</th></tr>
    {% for folder in folders %}
      <tr>
        <td>{{ folder }}</td>
        {% if folder in protected %}
          <td>🔒 Защищена</td>
          <td>
            <form method="post" style="display:inline">
              <input type="hidden" name="action" value="remove">
              <input type="hidden" name="folder" value="{{ folder }}">
              <button type="submit">Снять защиту</button>
            </form>
          </td>
        {% else %}
          <td>—</td>
          <td>
            <form method="post" style="display:inline">
              <input type="hidden" name="action" value="add">
              <input type="hidden" name="folder" value="{{ folder }}">
              <input type="password" name="password" placeholder="Пароль" required>
              <button type="submit">Поставить</button>
            </form>
          </td>
        {% endif %}
      </tr>
    {% endfor %}
  </table>
</div>
</body>
</html>
"""

TEMPLATE_AUTH = """<!doctype html>
<html lang="{{ 'ru' if lang=='ru' else 'en' }}">
<head>
<meta charset="utf-8">
<title>{{ title }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body{background:#0d1117;color:#e6edf3;font-family:\"Inter\",\"Segoe UI\",Arial,sans-serif;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#11151b;padding:30px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.35);max-width:360px;width:100%}
h1{margin:0 0 18px;font-size:22px;text-align:center}
label{display:block;margin-bottom:10px;font-size:14px;color:#9aa4b2}
input{width:100%;padding:10px;border-radius:10px;border:1px solid #2a3440;background:#151b23;color:#e6edf3;margin-bottom:12px}
button{width:100%;background:#238636;color:#fff;border:0;padding:10px;border-radius:10px;cursor:pointer;font-size:15px}
button:hover{background:#2ea043}
.error{color:#ff6b6b;margin-bottom:12px;text-align:center}
.alt{margin-top:12px;text-align:center}
.alt a{color:#58a6ff;text-decoration:none}
</style>
</head>
<body>
<div class="card">
  <h1>{{ title }}</h1>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post">
    <input type="hidden" name="next" value="{{ next_url }}">
    <label>{{ username_label }}</label>
    <input type="text" name="username" required autofocus>
    <label>{{ password_label }}</label>
    <input type="password" name="password" required>
    {% if show_confirm %}
      <label>{{ confirm_label }}</label>
      <input type="password" name="password_confirm" required>
    {% endif %}
    <button type="submit">{{ submit_label }}</button>
  </form>
  <div class="alt">{{ alt_text|safe }}</div>
  <div class="alt" style="margin-top:8px"><a href="{{ url_for('browse', subpath='') }}">← {{ ui['back'] }}</a></div>
</div>
</body>
</html>
"""
# -------------------------
# ROUTES
# -------------------------
@app.route("/set-lang/<code>")
def set_lang(code):
    code = (code or "ru").lower()
    if code not in ("ru","en"): code = "ru"
    ref = request.headers.get("Referer") or url_for("browse", subpath="")
    resp = make_response(redirect(ref))
    resp.set_cookie("lang", code, max_age=60*60*24*365, path="/")
    get_user_cookie(resp)
    return resp


@app.route("/settings/random", methods=["GET","POST"])
def random_settings():
    lang, ui = get_lang()
    top_dirs = list_all_top_dirs()
    raw = request.cookies.get("random_dirs") or "[]"
    try:
        selected = json.loads(raw)
    except Exception:
        selected = []
    if request.method == "POST":
        selected = request.form.getlist("dir")
        ref = url_for("random_settings")
        resp = make_response(redirect(ref))
        resp.set_cookie("random_dirs", json.dumps(selected, ensure_ascii=False), max_age=60*60*24*365, path="/")
        get_user_cookie(resp)
        return resp
    return render_template_string(TEMPLATE_RANDOM_SETTINGS, top_dirs=top_dirs, selected=selected, lang=lang, ui=ui)


@app.route("/random-settings")
def random_settings_alias():
    return redirect(url_for("random_settings"))


def get_random_dirs_from_cookie() -> List[str]:
    raw = request.cookies.get("random_dirs") or "[]"
    try:
        arr = json.loads(raw)
        if isinstance(arr, list): return [str(x) for x in arr]
    except Exception:
        pass
    return []


def require_access_for(rel_path: str) -> Optional[Response]:
    scope = get_protected_root_for(rel_path)
    if scope is None or is_admin_request(request) or user_has_persistent_access(scope):
        return None
    exp = request.args.get("exp")
    sig = request.args.get("sig")
    if not (exp and sig and verify_access_signature(scope, exp, sig)):
        return abort(403)
    return None


@app.route("/", defaults={"subpath": ""}, methods=["GET","POST"], endpoint="browse")
@app.route("/<path:subpath>", methods=["GET","POST"], endpoint="browse")
def browse(subpath):
    lang, ui = get_lang()
    is_admin = is_admin_request(request)

    dir_abs = safe_join(VIDEO_ROOT, subpath)
    if not os.path.exists(dir_abs) or not os.path.isdir(dir_abs): abort(404)

    if subpath in _protected and not is_admin and not user_has_persistent_access(subpath):
        if request.method == "POST":
            pw = request.form.get("password", "")
            if hashlib.sha256(pw.encode()).hexdigest() == _protected.get(subpath):
                remember_folder_access(subpath)
            else:
                return render_template_string(TEMPLATE_ACCESS, error=ui["wrong_pass"], path=subpath, lang=lang, ui=ui)
        else:
            return render_template_string(TEMPLATE_ACCESS, error=None, path=subpath, lang=lang, ui=ui)

    subfolders = list_subfolders(dir_abs)
    videos     = list_videos_in_dir(dir_abs, lang)
    enrich_cards_with_stats(videos)

    title = ui["title_main"] if not subpath else subpath
    crumbs = breadcrumbs_for(subpath)

    return render_template_string(TEMPLATE_MAIN,
        title=title, subfolders=subfolders, videos=videos,
        crumbs=crumbs, lang=lang, ui=ui,
        protected=_protected, is_admin=is_admin,
        current_user=g.user
    )


@app.route("/access/<path:subpath>", methods=["GET", "POST"])
def access_folder(subpath):
    lang, ui = get_lang()
    if subpath not in _protected:
        return redirect(url_for("browse", subpath=subpath))
    if is_admin_request(request) or user_has_persistent_access(subpath):
        return redirect(url_for("browse", subpath=subpath))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hashlib.sha256(pw.encode()).hexdigest() == _protected.get(subpath):
            remember_folder_access(subpath)
            dir_abs = safe_join(VIDEO_ROOT, subpath)
            if not os.path.isdir(dir_abs): abort(404)
            subfolders = list_subfolders(dir_abs)
            videos = list_videos_in_dir(dir_abs, lang)
            enrich_cards_with_stats(videos)
            title = ui["title_main"] if not subpath else subpath
            crumbs = breadcrumbs_for(subpath)
            return render_template_string(TEMPLATE_MAIN,
                title=title, subfolders=subfolders, videos=videos,
                crumbs=crumbs, lang=lang, ui=ui,
                protected=_protected, is_admin=is_admin_request(request),
                current_user=g.user
            )
        else:
            return render_template_string(TEMPLATE_ACCESS, error=ui["wrong_pass"], path=subpath, lang=lang, ui=ui)
    return render_template_string(TEMPLATE_ACCESS, error=None, path=subpath, lang=lang, ui=ui)

@app.route("/watch/<path:filepath>")
def watch_video(filepath):
    lang, ui = get_lang()
    is_admin = is_admin_request(request)

    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.isfile(full): abort(404)

    scope = get_protected_root_for(filepath)
    if scope and not (is_admin or user_has_persistent_access(scope)):
        exp = request.args.get("exp"); sig = request.args.get("sig")
        if not (exp and sig and verify_access_signature(scope, exp, sig)):
            return redirect(url_for("access_folder", subpath=scope))

    resp = make_response()

    if register_view_if_new(filepath, resp):
        _views[filepath] = _views.get(filepath, 0) + 1
        save_views()

    current_dir_abs   = os.path.dirname(full)
    current_dir_rel   = os.path.relpath(current_dir_abs, VIDEO_ROOT).replace("\\","/")
    current_dir_prefix= (current_dir_rel + "/") if current_dir_rel != "." else ""

    same_dir = [v for v in list_videos_in_dir(current_dir_abs, lang) if v["path"] != filepath]
    enrich_cards_with_stats(same_dir)
    related_same = random.sample(same_dir, min(5,len(same_dir))) if same_dir else []

    all_vids = list_all_videos(lang)
    enrich_cards_with_stats(all_vids)
    global_pool = [v for v in all_vids if v["path"] != filepath and not (current_dir_prefix and v["path"].startswith(current_dir_prefix))]
    related_global = random.sample(global_pool, min(5,len(global_pool))) if global_pool else []

    counts = reaction_counts([filepath]).get(filepath, {"likes": 0, "dislikes": 0})
    likes, dislikes = counts.get("likes", 0), counts.get("dislikes", 0)

    get_user_cookie(resp)

    fav = is_favorite(filepath)

    video_name_disp = translate_title_if_needed(os.path.basename(full), lang)
    duration_disp   = format_duration(ffprobe_duration(full))

    file_url         = with_grant(url_for('serve_file', filepath=filepath), scope)
    back_url         = with_grant(url_for('browse', subpath=os.path.dirname(filepath)), scope)
    random_url       = with_grant(url_for('random_video'), scope)
    delete_url       = with_grant(url_for('admin_delete', filepath=filepath), scope) if is_admin else ""
    checkfix_url     = with_grant(url_for('admin_checkfix', filepath=filepath), scope) if is_admin else ""

    heights          = available_heights_for(full)
    stream_base      = with_grant(url_for('stream_transcoded', filepath=filepath), scope)
    download_original_url = with_grant(url_for('download_video', filepath=filepath), scope)
    download_height_urls  = {h: with_grant(url_for('download_transcoded', filepath=filepath, h=h), scope) for h in heights}

    base_name = os.path.splitext(os.path.basename(full))[0]
    download_original_name = f"{base_name}.mp4"
    download_height_names = {h: f"{base_name}_{h}p.mp4" for h in heights}

    for v in related_same:
        sc = get_protected_root_for(v['path'])
        v["watch_url"]   = with_grant(url_for('watch_video', filepath=v['path']), sc)
        v["thumb_url"]   = with_grant(url_for('serve_file', filepath=v['thumb']), sc if sc else None)
        v["preview_url"] = with_grant(url_for('preview_file', filepath=v['path']), sc if sc else None)
    for v in related_global:
        sc = get_protected_root_for(v['path'])
        v["watch_url"]   = with_grant(url_for('watch_video', filepath=v['path']), sc)
        v["thumb_url"]   = with_grant(url_for('serve_file', filepath=v['thumb']), sc if sc else None)
        v["preview_url"] = with_grant(url_for('preview_file', filepath=v['path']), sc if sc else None)

    html = render_template_string(
        TEMPLATE_VIDEO,
        video_name=video_name_disp, filepath=filepath, back_url=back_url, random_url=random_url,
        download_original_url=download_original_url, download_height_urls=download_height_urls,
        download_original_name=download_original_name, download_height_names=download_height_names,
        delete_url=delete_url, checkfix_url=checkfix_url, heights=heights,
        is_admin=is_admin, related_same=related_same, related_global=related_global,
        lang=lang, ui=ui, views=_views.get(filepath,0), likes=likes, dislikes=dislikes, fav=fav,
        duration=duration_disp, file_url=file_url, stream_base=stream_base,
        thumb_url=with_grant(url_for('serve_file', filepath=os.path.relpath(generate_thumbnail(full), VIDEO_ROOT).replace("\\","/")), scope if scope else None),
        current_user=g.user, request_path=request.full_path if request.query_string else request.path
    )
    resp.set_data(html)
    return resp


@app.route("/files/<path:filepath>")
def serve_file(filepath):
    rel = filepath.replace("\\","/")
    need = require_access_for(rel)
    if need is not None: return need

    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.exists(full): abort(404)

    size = os.path.getsize(full)
    range_header = request.headers.get("Range")
    byte1, byte2 = 0, None

    if range_header:
        m = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            g1, g2 = m.groups()
            byte1 = int(g1)
            if g2: byte2 = int(g2)

    length = size - byte1
    if byte2 is not None: length = byte2 - byte1 + 1

    def generate_chunks(path, start, length, chunk=8192):
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data: break
                remaining -= len(data)
                yield data

    status = 206 if range_header else 200
    resp = Response(stream_with_context(generate_chunks(full, byte1, length)), status, mimetype="video/mp4", direct_passthrough=True)
    resp.headers.add("Accept-Ranges", "bytes")
    resp.headers.add("Content-Length", str(length))
    if range_header:
        resp.headers.add("Content-Range", f"bytes {byte1}-{byte1+length-1}/{size}")
    return resp


@app.route("/preview/<path:filepath>")
def preview_file(filepath):
    rel = filepath.replace("\\","/")
    need = require_access_for(rel)
    if need is not None: return need

    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.isfile(full): abort(404)
    preview_abs = ensure_preview(full)
    rel_from_root = os.path.relpath(preview_abs, VIDEO_ROOT).replace("\\","/")
    return serve_file(rel_from_root)

# ---------- Dynamic TRANSCODE streaming & download ----------
def stream_ffmpeg_process(cmd):
    """Yield ffmpeg stdout in chunks; kill on client disconnect."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        while True:
            chunk = proc.stdout.read(64*1024)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def validate_height_for(full_path: str, h: int) -> int:
    _, src_h = probe_video_size(full_path)
    h = int(h)
    if h > src_h:
        candidates = [x for x in COMMON_HEIGHTS if x <= src_h]
        h = candidates[0] if candidates else src_h
    if h < 180:
        h = 180
    return h


@app.route("/stream/<path:filepath>")
def stream_transcoded(filepath):
    rel = filepath.replace("\\","/")
    scope = get_protected_root_for(rel)
    if scope and not is_admin_request(request):
        exp = request.args.get("exp"); sig = request.args.get("sig")
        if not (exp and sig and verify_access_signature(scope, exp, sig)):
            return abort(403)

    h = request.args.get("h", type=int)
    if not h:
        return redirect(url_for('serve_file', filepath=filepath))
    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.isfile(full): abort(404)

    h = validate_height_for(full, h)
    cmd = ffmpeg_stream_cmd(full, h)

    headers = {
        "Content-Type": "video/mp4",
        "Cache-Control": "no-store",
        "Transfer-Encoding": "chunked",
        "Accept-Ranges": "none"
    }
    return Response(stream_with_context(stream_ffmpeg_process(cmd)), headers=headers)


@app.route("/download/<path:filepath>")
def download_video(filepath):
    rel = filepath.replace("\\","/")
    need = require_access_for(rel)
    if need is not None: return need

    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.isfile(full): abort(404)
    return send_file(full, as_attachment=True)


@app.route("/download_transcoded/<path:filepath>")
def download_transcoded(filepath):
    rel = filepath.replace("\\","/")
    scope = get_protected_root_for(rel)
    if scope and not is_admin_request(request):
        exp = request.args.get("exp"); sig = request.args.get("sig")
        if not (exp and sig and verify_access_signature(scope, exp, sig)):
            return abort(403)

    h = request.args.get("h", type=int)
    if not h:
        return redirect(url_for('download_video', filepath=filepath))
    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.isfile(full): abort(404)

    h = validate_height_for(full, h)
    cmd = ffmpeg_stream_cmd(full, h)
    base = os.path.splitext(os.path.basename(full))[0]
    fname = f"{base}_{h}p.mp4"
    headers = {
        "Content-Type": "video/mp4",
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-store",
        "Transfer-Encoding": "chunked",
        "Accept-Ranges": "none"
    }
    return Response(stream_with_context(stream_ffmpeg_process(cmd)), headers=headers)


# ---------- Likes / Favorites / Search ----------
def require_auth_api():
    if g.user is None:
        return jsonify({"ok": False, "error": "auth_required"}), 401
    return None


@app.route("/api/search")
def api_search():
    lang, _ = get_lang()
    q = (request.args.get("q") or "").strip().lower()
    results = []
    if q:
        all_videos = list_all_videos(lang)
        enrich_cards_with_stats(all_videos)
        for v in all_videos:
            if not is_admin_request(request) and get_protected_root_for(v["path"]) is not None:
                continue
            hay = (v["display"] or v["name"]).lower()
            if q in hay:
                results.append(v)
    return jsonify({"results": results})


@app.route("/api/state")
def api_state():
    resp = make_response()
    get_user_cookie(resp)
    path = request.args.get("path") or ""
    counts = reaction_counts([path]).get(path, {"likes": 0, "dislikes": 0})
    fav = False
    reaction = None
    if g.user is not None and path:
        reaction = user_reaction_for(path)
        fav = is_favorite(path)
    data = {
        "likes": counts.get("likes", 0),
        "dislikes": counts.get("dislikes", 0),
        "user_reaction": reaction,
        "favorite": fav,
        "authenticated": g.user is not None
    }
    resp.set_data(json.dumps(data))
    resp.mimetype = "application/json"
    return resp


@app.route("/api/like", methods=["POST"])
def api_like():
    need = require_auth_api()
    if need: return need
    if not allow_rate("react", 30, 60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False}), 400
    db = get_db()
    row = db.execute(
        "SELECT reaction FROM reactions WHERE user_id = ? AND video_path = ?",
        (g.user["id"], path)
    ).fetchone()
    if row and row["reaction"] == "like":
        db.execute("DELETE FROM reactions WHERE user_id = ? AND video_path = ?", (g.user["id"], path))
    else:
        db.execute(
            "INSERT INTO reactions (user_id, video_path, reaction) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, video_path) DO UPDATE SET reaction=excluded.reaction",
            (g.user["id"], path, "like")
        )
    db.commit()
    counts = reaction_counts([path])[path]
    return jsonify({"ok": True, "likes": counts["likes"], "dislikes": counts["dislikes"], "user_reaction": user_reaction_for(path)})


@app.route("/api/dislike", methods=["POST"])
def api_dislike():
    need = require_auth_api()
    if need: return need
    if not allow_rate("react", 30, 60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False}), 400
    db = get_db()
    row = db.execute(
        "SELECT reaction FROM reactions WHERE user_id = ? AND video_path = ?",
        (g.user["id"], path)
    ).fetchone()
    if row and row["reaction"] == "dislike":
        db.execute("DELETE FROM reactions WHERE user_id = ? AND video_path = ?", (g.user["id"], path))
    else:
        db.execute(
            "INSERT INTO reactions (user_id, video_path, reaction) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, video_path) DO UPDATE SET reaction=excluded.reaction",
            (g.user["id"], path, "dislike")
        )
    db.commit()
    counts = reaction_counts([path])[path]
    return jsonify({"ok": True, "likes": counts["likes"], "dislikes": counts["dislikes"], "user_reaction": user_reaction_for(path)})


@app.route("/api/favorite", methods=["POST"])
def api_favorite():
    need = require_auth_api()
    if need: return need
    if not allow_rate("favorite", 20, 60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False}), 400
    db = get_db()
    db.execute(
        "INSERT INTO favorites (user_id, video_path) VALUES (?, ?) "
        "ON CONFLICT(user_id, video_path) DO NOTHING",
        (g.user["id"], path)
    )
    db.commit()
    return jsonify({"ok": True, "favorite": True})


@app.route("/api/unfavorite", methods=["POST"])
def api_unfavorite():
    need = require_auth_api()
    if need: return need
    if not allow_rate("favorite", 20, 60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    path = data.get("path")
    if not path:
        return jsonify({"ok": False}), 400
    db = get_db()
    db.execute(
        "DELETE FROM favorites WHERE user_id = ? AND video_path = ?",
        (g.user["id"], path)
    )
    db.commit()
    return jsonify({"ok": True, "favorite": False})


@app.route("/favorites")
@login_required
def favorites_page():
    lang, ui = get_lang()
    db = get_db()
    rows = db.execute(
        "SELECT video_path FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
        (g.user["id"],)
    ).fetchall()
    items = []
    for r in rows:
        p = r["video_path"]
        try:
            full = safe_join(VIDEO_ROOT, p)
        except Exception:
            continue
        if os.path.isfile(full):
            disp = translate_title_if_needed(os.path.basename(full), lang)
            sc = get_protected_root_for(p)
            items.append({
                "path": p,
                "display": disp,
                "thumb_url": with_grant(url_for('serve_file', filepath=os.path.relpath(generate_thumbnail(full), VIDEO_ROOT).replace("\\","/")), sc if sc else None),
                "preview_url": with_grant(url_for('preview_file', filepath=p), sc if sc else None),
                "watch_url": with_grant(url_for('watch_video', filepath=p), sc)
            })
    html = render_template_string(TEMPLATE_FAVORITES, items=items, lang=lang, ui=ui, current_user=g.user)
    return html

@app.route("/random")
def random_video():
    lang, _ = get_lang()
    selected_dirs = get_random_dirs_from_cookie()
    all_videos = []

    for root, dirs, files in os.walk(VIDEO_ROOT):
        rel_dir = os.path.relpath(root, VIDEO_ROOT).replace("\\","/")
        if selected_dirs:
            top = rel_dir.split("/")[0] if rel_dir != "." else ""
            if top and top not in selected_dirs:
                continue
        for f in files:
            if f.lower().endswith(".mp4"):
                rel = os.path.relpath(os.path.join(root, f), VIDEO_ROOT).replace("\\", "/")
                if "/__previews__" in rel:
                    continue
                all_videos.append(rel)

    if not all_videos:
        return redirect(url_for("browse", subpath=""))

    video_path = random.choice(all_videos)

    for prot in _protected:
        if video_path.startswith(prot) and not (is_admin_request(request) or user_has_persistent_access(prot)):
            return redirect(url_for("access_folder", subpath=prot))

    return redirect(url_for("watch_video", filepath=video_path))


@app.route("/login", methods=["GET", "POST"])
def login():
    lang, ui = get_lang()
    if g.user:
        return redirect(request.args.get("next") or url_for("browse", subpath=""))
    error = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("browse", subpath="")
    if request.method == "POST":
        if not allow_rate("login", 5, 60):
            error = ui["too_many_attempts"]
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            db = get_db()
            row = db.execute("SELECT id, username, password_hash, is_admin FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
            if row and check_password_hash(row["password_hash"], password):
                session.clear()
                session["user_id"] = row["id"]
                return redirect(next_url)
            else:
                error = "Неверный логин или пароль" if lang == "ru" else "Invalid credentials"
    alt = ui["register"] + f"? <a href=\"{url_for('register', next=next_url)}\">{ui['register']}</a>"
    return render_template_string(
        TEMPLATE_AUTH,
        title=ui["login"], error=error, next_url=next_url,
        username_label="Логин" if lang == "ru" else "Username",
        password_label="Пароль" if lang == "ru" else "Password",
        confirm_label="" , show_confirm=False,
        submit_label=ui["login"], alt_text=alt, ui=ui
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    lang, ui = get_lang()
    if g.user:
        return redirect(request.args.get("next") or url_for("browse", subpath=""))
    error = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("browse", subpath="")
    if request.method == "POST":
        if not allow_rate("register", 3, 300):
            error = ui["too_many_attempts"]
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")
            if not username or not password:
                error = "Заполните все поля" if lang == "ru" else "Fill all fields"
            elif password != password_confirm:
                error = "Пароли не совпадают" if lang == "ru" else "Passwords do not match"
            else:
                db = get_db()
                exists = db.execute("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)).fetchone()
                if exists:
                    error = "Логин уже используется" if lang == "ru" else "Username already taken"
                else:
                    db.execute(
                        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 0)",
                        (username, generate_password_hash(password))
                    )
                    db.commit()
                    row = db.execute("SELECT id FROM users WHERE lower(username)=lower(?)", (username,)).fetchone()
                    session.clear()
                    session["user_id"] = row["id"]
                    return redirect(next_url)
    alt = ui["login"] + f"? <a href=\"{url_for('login', next=next_url)}\">{ui['login']}</a>"
    return render_template_string(
        TEMPLATE_AUTH,
        title=ui["register"], error=error, next_url=next_url,
        username_label="Логин" if lang == "ru" else "Username",
        password_label="Пароль" if lang == "ru" else "Password",
        confirm_label="Повторите пароль" if lang == "ru" else "Confirm password",
        show_confirm=True,
        submit_label=ui["register"], alt_text=alt, ui=ui
    )


@app.route("/logout")
def logout():
    next_url = request.args.get("next") or url_for("browse", subpath="")
    session.clear()
    return redirect(next_url)


# ---------- Admin ----------
@app.route("/admin/protect", methods=["GET", "POST"])
@login_required
def admin_protect():
    if not g.user.get("is_admin"):
        abort(403)
    folders = []
    for root, dirs, files in os.walk(VIDEO_ROOT):
        for d in dirs:
            if d.startswith("__"): continue
            rel = os.path.relpath(os.path.join(root, d), VIDEO_ROOT).replace("\\", "/")
            folders.append(rel)
    folders.sort(key=str.lower)

    if request.method == "POST":
        act = request.form.get("action")
        fld = request.form.get("folder")
        if act == "add":
            pw = request.form.get("password", "")
            if pw:
                clear_folder_access(fld)
                _protected[fld] = hashlib.sha256(pw.encode()).hexdigest(); save_protected()
        elif act == "remove":
            _protected.pop(fld, None); save_protected()
            clear_folder_access(fld)
        return redirect(url_for("admin_protect"))

    return render_template_string(TEMPLATE_PROTECT, folders=folders, protected=_protected)


# ---------- Fix / Delete ----------
def ffmpeg_check(video_path: str) -> bool:
    try:
        res = subprocess.run(
            ["ffmpeg","-v","error","-i",video_path,"-f","null","-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20
        )
        return (res.returncode == 0) and (not res.stderr.strip())
    except Exception:
        return False


def ffmpeg_fix(video_path: str) -> bool:
    tmp = video_path + ".fixed.mp4"
    try:
        subprocess.run(
            ["ffmpeg","-y","-i",video_path,"-c","copy","-movflags","+faststart", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120
        )
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, video_path); return True
    except Exception:
        pass
    try:
        subprocess.run(
            ["ffmpeg","-y","-i",video_path,
             "-c:v","libx264","-preset","veryfast","-crf","20",
             "-c:a","aac","-b:a","192k","-movflags","+faststart", tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600
        )
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, video_path); return True
    except Exception:
        pass
    try:
        if os.path.exists(tmp): os.remove(tmp)
    except Exception:
        pass
    return False


@app.route("/admin/checkfix/<path:filepath>")
@login_required
def admin_checkfix(filepath):
    if not g.user.get("is_admin"):
        abort(403)
    full = safe_join(VIDEO_ROOT, filepath)
    if not os.path.isfile(full): abort(404)
    ok = ffmpeg_check(full)
    if not ok: ffmpeg_fix(full)
    return redirect(url_for("watch_video", filepath=filepath))


@app.route("/admin/delete/<path:filepath>")
@login_required
def admin_delete(filepath):
    if not g.user.get("is_admin"):
        abort(403)
    full = safe_join(VIDEO_ROOT, filepath)
    if os.path.isfile(full):
        try:
            os.remove(full)
            thumb = os.path.splitext(full)[0] + ".jpg"
            if os.path.exists(thumb): os.remove(thumb)
            prv_rel = os.path.splitext(os.path.relpath(full, VIDEO_ROOT))[0] + ".preview.mp4"
            prv_abs = os.path.join(PREVIEW_ROOT, prv_rel)
            if os.path.exists(prv_abs): os.remove(prv_abs)
            _views.pop(filepath, None); save_views()
            db = get_db()
            db.execute("DELETE FROM reactions WHERE video_path = ?", (filepath,))
            db.execute("DELETE FROM favorites WHERE video_path = ?", (filepath,))
            db.execute("DELETE FROM view_events WHERE video_path = ?", (filepath,))
            db.commit()
        except Exception as e:
            print("Ошибка удаления:", e)
    return redirect(url_for("browse", subpath=os.path.dirname(filepath)))


# -------------------------
# START
# -------------------------
if __name__ == "__main__":
    print(f"📂 Видео-каталог: {VIDEO_ROOT}")
    print(f"📂 Превью-каталог: {PREVIEW_ROOT}")
    print(f"🗂 Кэш: translations.json, durations.json, views.json, protected_folders.json")
    print(f"🗄️ SQLite: {DATABASE_PATH}")
    print(f"🔐 Admin username: {ADMIN_USERNAME}")
    print(f"🔏 Access token TTL: {ACCESS_TOKEN_TTL_SEC}s (HMAC in query)")
    print("▶️ Запуск на http://0.0.0.0:8000")
    app.run(host="0.0.0.0", port=8000, debug=True)
