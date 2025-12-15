# pip install fastapi uvicorn sqlalchemy jinja2 passlib[bcrypt] cryptography python-multipart
import base64
import os
import secrets
import time
from datetime import datetime
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware import Middleware
from fastapi.middleware.sessions import SessionMiddleware
from jinja2 import Environment, DictLoader, select_autoescape
from passlib.hash import bcrypt
from sqlalchemy import Column, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import declarative_base, sessionmaker, Session as DBSession
from starlette.datastructures import URL
from starlette.status import HTTP_303_SEE_OTHER
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Configuration
password = "CHANGE_ME_STRONG"
SESSION_COOKIE_SECURE = False  # set True in production with HTTPS
SESSION_SECRET = secrets.token_hex(32)
LOGIN_RATE_LIMIT = 8  # attempts per minute per IP
HASHED_PASSWORD = bcrypt.hash(password)

# Master key setup
raw_master = os.getenv("VAULT_MASTER_KEY")
if not raw_master:
    raise RuntimeError("VAULT_MASTER_KEY is required and must be urlsafe base64-encoded 32 bytes")
try:
    master_key = base64.urlsafe_b64decode(raw_master)
except Exception as exc:  # pragma: no cover
    raise RuntimeError("Invalid VAULT_MASTER_KEY format") from exc
if len(master_key) != 32:
    raise RuntimeError("VAULT_MASTER_KEY must decode to 32 bytes")

engine = create_engine("sqlite:///./vault.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class VaultItem(Base):
    __tablename__ = "vault_items"

    id: int = Column(Integer, primary_key=True, index=True)
    title: str = Column(String(255), nullable=False)
    category: str = Column(String(50), nullable=False, default="other")
    url: Optional[str] = Column(String(512))
    login: Optional[str] = Column(String(255))
    tags: Optional[str] = Column(String(255))
    secret_nonce: Optional[str] = Column(String(255))
    secret_ciphertext: Optional[str] = Column(String(2048))
    notes_nonce: Optional[str] = Column(String(255))
    notes_ciphertext: Optional[str] = Column(String(4096))
    created_at: datetime = Column(DateTime, default=datetime.utcnow)
    updated_at: datetime = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


Base.metadata.create_all(bind=engine)


# Templates
TEMPLATES: Dict[str, str] = {
    "base.html": """
<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">
<title>Vault</title>
<style>
:root { color-scheme: dark; }
body { background: radial-gradient(circle at 20% 20%, rgba(99,102,241,.25), transparent 25%),
              radial-gradient(circle at 80% 0%, rgba(236,72,153,.2), transparent 25%),
              #0b1220; min-height: 100vh; color: #e5e7eb; }
.glass { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
         box-shadow: 0 10px 40px rgba(0,0,0,0.35); backdrop-filter: blur(12px);
         border-radius: 16px; }
.navbar { background: rgba(13,16,35,0.8); backdrop-filter: blur(10px); }
a { color: #a5b4fc; }
.btn-primary { background: linear-gradient(135deg,#7c3aed,#2563eb); border: none; }
.form-control, .form-select { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: #e5e7eb; }
.table { color: #e5e7eb; }
.card { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); }
.badge { background: rgba(255,255,255,0.08); }
.toast-container { z-index: 2000; }
</style>
</head>
<body>
<nav class=\"navbar navbar-expand-lg navbar-dark mb-4\">
  <div class=\"container-fluid\">
    <a class=\"navbar-brand fw-bold\" href=\"/items\">Vault</a>
    <button class=\"navbar-toggler\" type=\"button\" data-bs-toggle=\"collapse\" data-bs-target=\"#nav\">\n      <span class=\"navbar-toggler-icon\"></span>\n    </button>
    <div class=\"collapse navbar-collapse\" id=\"nav\">
      <ul class=\"navbar-nav me-auto\">
        <li class=\"nav-item\"><a class=\"nav-link\" href=\"/items\">Items</a></li>
      </ul>
      {% if session.get('user') %}
      <form method=\"post\" action=\"/logout\" class=\"d-flex\">
        <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf_token }}\">
        <button class=\"btn btn-outline-light btn-sm\" type=\"submit\">Logout</button>
      </form>
      {% endif %}
    </div>
  </div>
</nav>
<div class=\"container mb-5\">
  {% block content %}{% endblock %}
</div>
<div class=\"toast-container position-fixed bottom-0 end-0 p-3\">
  {% for message in flashes %}
  <div class=\"toast align-items-center text-bg-primary border-0 mb-2\" role=\"alert\" data-bs-delay=\"3500\" aria-live=\"assertive\" aria-atomic=\"true\">
    <div class=\"d-flex\">
      <div class=\"toast-body\">{{ message }}</div>
      <button type=\"button\" class=\"btn-close btn-close-white me-2 m-auto\" data-bs-dismiss=\"toast\" aria-label=\"Close\"></button>
    </div>
  </div>
  {% endfor %}
</div>
<script src=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js\"></script>
<script>
document.querySelectorAll('.toast').forEach(t => new bootstrap.Toast(t).show());
function copyToClipboard(text, btn){ navigator.clipboard.writeText(text); btn.innerText='Copied'; setTimeout(()=>btn.innerText='Copy',1200); }
</script>
</body>
</html>
""",
    "login.html": """
{% extends 'base.html' %}
{% block content %}
<div class=\"row justify-content-center\">
  <div class=\"col-md-5\">
    <div class=\"glass p-4\">
      <h4 class=\"mb-3\">Sign in</h4>
      <form method=\"post\" action=\"/login\">\n        <div class=\"mb-3\">\n          <label class=\"form-label\">Username</label>\n          <input class=\"form-control\" name=\"username\" required autofocus>\n        </div>\n        <div class=\"mb-3\">\n          <label class=\"form-label\">Password</label>\n          <input class=\"form-control\" type=\"password\" name=\"password\" required>\n        </div>\n        <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf_token }}\">\n        <button class=\"btn btn-primary w-100\" type=\"submit\">Login</button>\n      </form>
    </div>
  </div>
</div>
{% endblock %}
""",
    "items.html": """
{% extends 'base.html' %}
{% block content %}
<div class=\"d-flex justify-content-between align-items-center mb-3\">
  <div>
    <h4 class=\"mb-0\">Vault Items</h4>
    <p class=\"text-muted\">Securely manage your secrets</p>
  </div>
  <a class=\"btn btn-primary\" href=\"/items/new\">New Item</a>
</div>
<form class=\"row g-2 align-items-end glass p-3 mb-3\" method=\"get\" action=\"/items\">\n  <div class=\"col-md-4\">\n    <label class=\"form-label\">Search</label>\n    <input class=\"form-control\" name=\"q\" value=\"{{ q }}\" placeholder=\"Title or tags\">\n  </div>\n  <div class=\"col-md-3\">\n    <label class=\"form-label\">Category</label>\n    <select class=\"form-select\" name=\"category\">\n      <option value=\"\">All</option>\n      {% for c in ['password','license','note','other'] %}\n      <option value=\"{{c}}\" {% if category==c %}selected{% endif %}>{{ c|capitalize }}</option>\n      {% endfor %}\n    </select>\n  </div>\n  <div class=\"col-md-2\">\n    <label class=\"form-label\">Per Page</label>\n    <select class=\"form-select\" name=\"limit\">\n      {% for l in [5,10,20] %}<option {% if limit==l %}selected{% endif %}>{{l}}</option>{% endfor %}\n    </select>\n  </div>\n  <div class=\"col-md-3\">\n    <button class=\"btn btn-outline-light mt-4 w-100\" type=\"submit\">Apply</button>\n  </div>\n</form>
<div class=\"row row-cols-1 row-cols-md-2 g-3\">
  {% for item in items %}
  <div class=\"col\">
    <div class=\"card h-100 p-3\">
      <div class=\"d-flex justify-content-between\">
        <div>
          <h5 class=\"card-title mb-1\"><a href=\"/items/{{item.id}}\" class=\"text-decoration-none text-light\">{{ item.title }}</a></h5>
          <span class=\"badge bg-secondary\">{{ item.category|capitalize }}</span>
        </div>
        <small class=\"text-muted\">{{ item.updated_at.strftime('%Y-%m-%d') }}</small>
      </div>
      <p class=\"text-muted small mt-2\">{{ item.tags or '' }}</p>
      <div class=\"mt-auto\">
        <a class=\"btn btn-sm btn-outline-light me-2\" href=\"/items/{{item.id}}/edit\">Edit</a>
        <form method=\"post\" action=\"/items/{{item.id}}/delete\" class=\"d-inline\" onsubmit=\"return confirm('Delete?');\">\n          <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf_token }}\">\n          <button class=\"btn btn-sm btn-outline-danger\" type=\"submit\">Delete</button>\n        </form>
      </div>
    </div>
  </div>
  {% endfor %}
</div>
<nav class=\"mt-4\">\n  <ul class=\"pagination justify-content-center\">\n    <li class=\"page-item {% if page<=1 %}disabled{% endif %}\">\n      <a class=\"page-link\" href=\"{{ page_url(page-1) }}\">Prev</a>\n    </li>\n    <li class=\"page-item\"><span class=\"page-link bg-transparent text-light border-0\">Page {{page}}</span></li>\n    <li class=\"page-item {% if not has_more %}disabled{% endif %}\">\n      <a class=\"page-link\" href=\"{{ page_url(page+1) }}\">Next</a>\n    </li>\n  </ul>\n</nav>
{% endblock %}
""",
    "form.html": """
{% extends 'base.html' %}
{% block content %}
<div class=\"row justify-content-center\">
  <div class=\"col-lg-8\">
    <div class=\"glass p-4\">
      <h4 class=\"mb-3\">{{ heading }}</h4>
      <form method=\"post\">\n        <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf_token }}\">\n        <div class=\"row g-3\">\n          <div class=\"col-md-8\">\n            <label class=\"form-label\">Title</label>\n            <input class=\"form-control\" name=\"title\" required value=\"{{ item.title or '' }}\">\n          </div>\n          <div class=\"col-md-4\">\n            <label class=\"form-label\">Category</label>\n            <select class=\"form-select\" name=\"category\">\n              {% for c in ['password','license','note','other'] %}\n              <option value=\"{{c}}\" {% if item.category==c %}selected{% endif %}>{{ c|capitalize }}</option>\n              {% endfor %}\n            </select>\n          </div>\n          <div class=\"col-md-6\">\n            <label class=\"form-label\">URL</label>\n            <input class=\"form-control\" name=\"url\" value=\"{{ item.url or '' }}\" placeholder=\"https://...\">\n          </div>\n          <div class=\"col-md-6\">\n            <label class=\"form-label\">Login</label>\n            <input class=\"form-control\" name=\"login\" value=\"{{ item.login or '' }}\">\n          </div>\n          <div class=\"col-md-6\">\n            <label class=\"form-label\">Tags</label>\n            <input class=\"form-control\" name=\"tags\" value=\"{{ item.tags or '' }}\" placeholder=\"tag1,tag2\">\n          </div>\n          <div class=\"col-md-6\">\n            <label class=\"form-label\">Secret</label>\n            <input class=\"form-control\" name=\"secret\" value=\"{{ secret or '' }}\">\n          </div>\n          <div class=\"col-12\">\n            <label class=\"form-label\">Notes</label>\n            <textarea class=\"form-control\" rows=\"4\" name=\"notes\">{{ notes or '' }}</textarea>\n          </div>\n        </div>\n        <div class=\"d-flex justify-content-end gap-2 mt-3\">\n          <a class=\"btn btn-outline-light\" href=\"/items\">Cancel</a>\n          <button class=\"btn btn-primary\" type=\"submit\">Save</button>\n        </div>\n      </form>
    </div>
  </div>
</div>
{% endblock %}
""",
    "detail.html": """
{% extends 'base.html' %}
{% block content %}
<div class=\"glass p-4\">
  <div class=\"d-flex justify-content-between align-items-start\">
    <div>
      <h4>{{ item.title }}</h4>
      <span class=\"badge bg-secondary\">{{ item.category|capitalize }}</span>
    </div>
    <div>
      <a class=\"btn btn-outline-light btn-sm me-2\" href=\"/items/{{item.id}}/edit\">Edit</a>
      <form method=\"post\" action=\"/items/{{item.id}}/delete\" class=\"d-inline\" onsubmit=\"return confirm('Delete?');\">\n        <input type=\"hidden\" name=\"csrf_token\" value=\"{{ csrf_token }}\">\n        <button class=\"btn btn-outline-danger btn-sm\" type=\"submit\">Delete</button>\n      </form>
    </div>
  </div>
  <div class=\"row mt-3 g-3\">
    <div class=\"col-md-6\">
      <div class=\"card p-3\">
        <div class=\"d-flex justify-content-between\"><span class=\"text-muted\">URL</span><a href=\"{{ item.url }}\" target=\"_blank\">{{ item.url }}</a></div>
        <hr>
        <div class=\"d-flex justify-content-between align-items-center\">
          <div>
            <div class=\"text-muted\">Login</div>
            <div class=\"fw-semibold\">{{ item.login or '' }}</div>
          </div>
          <button class=\"btn btn-outline-light btn-sm\" onclick=\"copyToClipboard('{{ item.login or '' }}', this)\">Copy</button>
        </div>
      </div>
    </div>
    <div class=\"col-md-6\">
      <div class=\"card p-3\">
        <div class=\"d-flex justify-content-between align-items-center\">
          <div>
            <div class=\"text-muted\">Secret</div>
            <div class=\"fw-semibold\">{{ secret }}</div>
          </div>
          <button class=\"btn btn-outline-light btn-sm\" onclick=\"copyToClipboard('{{ secret }}', this)\">Copy</button>
        </div>
        <hr>
        <div>
          <div class=\"text-muted\">Notes</div>
          <div style=\"white-space:pre-wrap\">{{ notes }}</div>
        </div>
      </div>
    </div>
  </div>
  <div class=\"mt-3 text-muted small\">Updated {{ item.updated_at.strftime('%Y-%m-%d %H:%M') }}</div>
</div>
{% endblock %}
""",
}

env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(["html", "xml"]))


def render_template(name: str, request: Request, **context: Any) -> HTMLResponse:
    session = request.session
    flashes = session.pop("flash", []) if "flash" in session else []
    csrf_token = ensure_csrf(request)
    template = env.get_template(name)
    html = template.render(request=request, session=session, csrf_token=csrf_token, flashes=flashes, **context)
    return HTMLResponse(html)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def add_flash(request: Request, message: str) -> None:
    request.session.setdefault("flash", []).append(message)


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request) -> None:
    token = request.session.get("csrf_token")
    form_token = request.form().get("csrf_token") if isinstance(request, Request) else None
    if not token or not form_token or not secrets.compare_digest(token, form_token):
        raise HTTPException(status_code=400, detail="Invalid CSRF token")


def csrf_protect(func):
    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        form = await request.form()
        token = request.session.get("csrf_token")
        if not token or not form.get("csrf_token") or not secrets.compare_digest(token, form.get("csrf_token")):
            raise HTTPException(status_code=400, detail="Invalid CSRF token")
        return await func(request, *args, **kwargs)

    return wrapper


def require_auth(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return None


login_attempts: Dict[str, List[float]] = {}

def rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = login_attempts.setdefault(ip, [])
    attempts[:] = [t for t in attempts if now - t < 60]
    if len(attempts) >= LOGIN_RATE_LIMIT:
        return False
    attempts.append(now)
    return True


def encrypt_value(item_id: int, field: str, value: str) -> Tuple[str, str]:
    aes = AESGCM(master_key)
    nonce = os.urandom(12)
    aad = f"vaultitem|{item_id}|{field}".encode()
    ct = aes.encrypt(nonce, value.encode(), aad)
    return base64.b64encode(nonce).decode(), base64.b64encode(ct).decode()


def decrypt_value(item_id: int, field: str, nonce_b64: Optional[str], ct_b64: Optional[str]) -> str:
    if not nonce_b64 or not ct_b64:
        return ""
    aes = AESGCM(master_key)
    nonce = base64.b64decode(nonce_b64)
    ct = base64.b64decode(ct_b64)
    aad = f"vaultitem|{item_id}|{field}".encode()
    try:
        pt = aes.decrypt(nonce, ct, aad)
        return pt.decode()
    except Exception:
        return "[decryption error]"


def security_headers(request: Request, response: Response) -> None:
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    csp = "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline';"
    response.headers["Content-Security-Policy"] = csp


middleware = [Middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="vault_session", https_only=SESSION_COOKIE_SECURE, same_site="lax", max_age=60 * 60 * 8)]
app = FastAPI(middleware=middleware)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)
    if request.method == "POST":
        form = await request.form()
        token = request.session.get("csrf_token")
        if not token or not form.get("csrf_token") or not secrets.compare_digest(token, form.get("csrf_token")):
            return PlainTextResponse("Invalid CSRF token", status_code=400)
    if request.url.path not in ["/login", "/"] and not request.session.get("user"):
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    response = await call_next(request)
    security_headers(request, response)
    return response


@app.get("/")
async def root(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/items", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/items", status_code=HTTP_303_SEE_OTHER)
    return render_template("login.html", request)


@app.post("/login")
async def login(request: Request, username: str = Form(...), password_input: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limit(client_ip):
        return PlainTextResponse("Too many attempts, slow down", status_code=429)
    if username != "admin" or not bcrypt.verify(password_input, HASHED_PASSWORD):
        add_flash(request, "Invalid credentials")
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    request.session["user"] = "admin"
    add_flash(request, "Welcome back")
    return RedirectResponse("/items", status_code=HTTP_303_SEE_OTHER)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    response = RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return response


def get_pagination_url(request: Request, page: int) -> str:
    url = URL(str(request.url))
    query = dict(request.query_params)
    query["page"] = str(max(page, 1))
    return str(url.replace_query_params(**query))


@app.get("/items", response_class=HTMLResponse)
async def list_items(request: Request, db: DBSession = Depends(get_db), q: str = "", category: str = "", page: int = 1, limit: int = 10):
    limit = max(1, min(limit, 50))
    stmt = select(VaultItem)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where((VaultItem.title.ilike(pattern)) | (VaultItem.tags.ilike(pattern)))
    if category:
        stmt = stmt.where(VaultItem.category == category)
    total_stmt = stmt.order_by(VaultItem.updated_at.desc())
    items = db.execute(total_stmt.offset((page - 1) * limit).limit(limit + 1)).scalars().all()
    has_more = len(items) > limit
    items = items[:limit]
    return render_template(
        "items.html",
        request,
        items=items,
        q=q,
        category=category,
        limit=limit,
        page=page,
        has_more=has_more,
        page_url=lambda p: get_pagination_url(request, p),
    )


@app.get("/items/new", response_class=HTMLResponse)
async def new_item(request: Request):
    return render_template("form.html", request, heading="New Item", item=VaultItem(), secret="", notes="")


@app.post("/items/new")
async def create_item(
    request: Request,
    db: DBSession = Depends(get_db),
    title: str = Form(...),
    category: str = Form(...),
    url: str = Form("") ,
    login_field: str = Form("", alias="login"),
    tags: str = Form("") ,
    secret: str = Form("") ,
    notes: str = Form(""),
):
    item = VaultItem(title=title, category=category, url=url or None, login=login_field or None, tags=tags or None)
    db.add(item)
    db.flush()
    item_id = item.id
    if secret:
        nonce, ct = encrypt_value(item_id, "secret", secret)
        item.secret_nonce = nonce
        item.secret_ciphertext = ct
    if notes:
        nonce_n, ct_n = encrypt_value(item_id, "notes", notes)
        item.notes_nonce = nonce_n
        item.notes_ciphertext = ct_n
    db.commit()
    add_flash(request, "Item created")
    return RedirectResponse(f"/items/{item.id}", status_code=HTTP_303_SEE_OTHER)


@app.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int, db: DBSession = Depends(get_db)):
    item = db.get(VaultItem, item_id)
    if not item:
        raise HTTPException(status_code=404)
    secret = decrypt_value(item.id, "secret", item.secret_nonce, item.secret_ciphertext)
    notes = decrypt_value(item.id, "notes", item.notes_nonce, item.notes_ciphertext)
    return render_template("detail.html", request, item=item, secret=secret, notes=notes)


@app.get("/items/{item_id}/edit", response_class=HTMLResponse)
async def edit_item(request: Request, item_id: int, db: DBSession = Depends(get_db)):
    item = db.get(VaultItem, item_id)
    if not item:
        raise HTTPException(status_code=404)
    secret = decrypt_value(item.id, "secret", item.secret_nonce, item.secret_ciphertext)
    notes = decrypt_value(item.id, "notes", item.notes_nonce, item.notes_ciphertext)
    return render_template("form.html", request, heading="Edit Item", item=item, secret=secret, notes=notes)


@app.post("/items/{item_id}/edit")
async def update_item(
    request: Request,
    item_id: int,
    db: DBSession = Depends(get_db),
    title: str = Form(...),
    category: str = Form(...),
    url: str = Form("") ,
    login_field: str = Form("", alias="login"),
    tags: str = Form("") ,
    secret: str = Form("") ,
    notes: str = Form(""),
):
    item = db.get(VaultItem, item_id)
    if not item:
        raise HTTPException(status_code=404)
    item.title = title
    item.category = category
    item.url = url or None
    item.login = login_field or None
    item.tags = tags or None
    if secret:
        nonce, ct = encrypt_value(item.id, "secret", secret)
        item.secret_nonce = nonce
        item.secret_ciphertext = ct
    if notes or item.notes_ciphertext:
        nonce_n, ct_n = encrypt_value(item.id, "notes", notes)
        item.notes_nonce = nonce_n
        item.notes_ciphertext = ct_n
    db.commit()
    add_flash(request, "Item updated")
    return RedirectResponse(f"/items/{item.id}", status_code=HTTP_303_SEE_OTHER)


@app.post("/items/{item_id}/delete")
async def delete_item(request: Request, item_id: int, db: DBSession = Depends(get_db)):
    item = db.get(VaultItem, item_id)
    if not item:
        raise HTTPException(status_code=404)
    db.delete(item)
    db.commit()
    add_flash(request, "Item deleted")
    return RedirectResponse("/items", status_code=HTTP_303_SEE_OTHER)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 400:
        return PlainTextResponse("Bad request", status_code=400)
    if exc.status_code == 404:
        return PlainTextResponse("Not found", status_code=404)
    return PlainTextResponse("Error", status_code=exc.status_code)


@app.on_event("startup")
async def on_startup():
    Base.metadata.create_all(bind=engine)
    bcrypt.hash(password)

