"""
KINO KATALOG sayti (backend).
Botning kino bazasidan (PostgreSQL) o'qiydi. Video botda qoladi (file_id).
Sayt: chiroyli katalog ko'rsatadi, "Botda ko'rish" → botga yo'naltiradi.
Poster: bot orqali Telegram file_id'dan proxy qilinadi.
"""
import os
import logging
import io
import re
import hmac
import hashlib
import time
import json
import html
import threading
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlparse, unquote, quote
import pg8000.dbapi
import requests
from flask import Flask, request, jsonify, send_from_directory, Response, redirect, session

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kino")

DATABASE_URL   = os.getenv("DATABASE_URL", "")
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_USERNAME   = os.getenv("BOT_USERNAME", "")          # botga yo'naltirish + Telegram login uchun
ADMIN_PASSWORD = os.getenv("KINO_ADMIN_PASSWORD", "admin123")
if ADMIN_PASSWORD == "admin123":
    log.warning("OGOHLANTIRISH: KINO_ADMIN_PASSWORD environment o'zgaruvchisi o'rnatilmagan — "
                "standart 'admin123' paroli ishlatilmoqda! Railway'da darhol o'zgartiring.")
ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID", "")    # admin(lar) Telegram ID — yangi so'rov xabari uchun (vergul bilan bir nechta)
REVIEW_COOLDOWN  = int(os.getenv("REVIEW_COOLDOWN", "15"))    # soniya — izoh/javob orasidagi minimal vaqt
REQUEST_COOLDOWN = int(os.getenv("REQUEST_COOLDOWN", "30"))   # soniya — yangi kino so'rovi orasidagi minimal vaqt
TMDB_TOKEN     = os.getenv("TMDB_TOKEN", "")   # TMDB v4 "Read Access Token" (Bearer)
TMDB_KEY       = os.getenv("TMDB_KEY", "")     # TMDB v3 API key (zaxira)
PORT           = int(os.getenv("PORT", "8080"))
BASE_URL       = os.getenv("BASE_URL", "https://astramovie.com").rstrip("/")

app = Flask(__name__, static_folder="static")
# Sessiya imzosi uchun maxfiy kalit (SECRET_KEY bo'lmasa BOT_TOKEN'dan barqaror hosil qilinadi)
app.secret_key = os.getenv("SECRET_KEY") or hashlib.sha256(
    (BOT_TOKEN or "astra-fallback-secret").encode()).hexdigest()

# ── Session cookie xavfsizlik sozlamalari ──────────────────────────────────
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,      # JS orqali o'qib bo'lmaydi (XSS himoyasi)
    SESSION_COOKIE_SECURE=True,        # faqat HTTPS orqali yuboriladi
    SESSION_COOKIE_SAMESITE="Lax",     # CSRF xavfini kamaytiradi
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,  # 12 soat — session.permanent=True bo'lganda
)

import gzip as _gzip   # javoblarni siqish uchun (qo'shimcha kutubxona kerak emas)

# ── Oddiy rate-limit (xotirada, tashqi kutubxonasiz) ──────────────────────────
# Public GET endpointlarni botlab spam qilishdan himoya qiladi.
_rl_lock = threading.Lock()
_rl_hits = defaultdict(deque)   # key -> so'nggi so'rov vaqtlari

def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"

def rate_limit(max_requests=30, window=60):
    """max_requests ta so'rov / window soniya, IP + endpoint bo'yicha."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{_client_ip()}:{fn.__name__}"
            now = time.time()
            with _rl_lock:
                q = _rl_hits[key]
                while q and now - q[0] > window:
                    q.popleft()
                if len(q) >= max_requests:
                    return jsonify({"error": "Juda ko'p so'rov. Birozdan keyin urinib ko'ring."}), 429
                q.append(now)
            return fn(*args, **kwargs)
        return wrapper
    return deco

# ── Admin login uchun brute-force himoyasi (xotirada, IP bo'yicha) ────────────
_login_lock = threading.Lock()
_login_fails = defaultdict(list)   # ip -> [muvaffaqiyatsiz urinish vaqtlari]
_LOGIN_MAX_FAILS = 5                # shu vaqt oralig'ida ruxsat etilgan max noto'g'ri urinish
_LOGIN_WINDOW = 15 * 60             # 15 daqiqa
_LOGIN_LOCKOUT = 15 * 60            # limitdan oshsa, 15 daqiqaga bloklanadi

def _login_blocked(ip):
    now = time.time()
    with _login_lock:
        fails = _login_fails[ip]
        while fails and now - fails[0] > _LOGIN_WINDOW:
            fails.pop(0)
        return len(fails) >= _LOGIN_MAX_FAILS

def _login_register_fail(ip):
    with _login_lock:
        _login_fails[ip].append(time.time())

def _login_clear(ip):
    with _login_lock:
        _login_fails.pop(ip, None)

# ── Poster server keshi (xotirada) — Telegram'ga takror bormaslik uchun ────────
_poster_cache = {}              # poster_id -> (bytes, content_type, timestamp)
_poster_lock = threading.Lock()
_POSTER_TTL = 6 * 3600          # 6 soat
_POSTER_MAX = 300               # eng ko'pi bilan 300 ta poster xotirada

def _yt_id(url):
    """YouTube havola yoki ID'dan 11 belgili video ID ajratadi. Topilmasa ''."""
    if not url:
        return ""
    url = str(url).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|v/|shorts/))([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""

# ── Telegram bot orqali bildirishnoma yuborish ────────────────────────────────
def _tg_send(chat_id, text, buttons=None):
    """Botdan foydalanuvchiga xabar yuboradi.
    Eslatma: foydalanuvchi botni 'Start' qilmagan bo'lsa, Telegram ruxsat bermaydi —
    bunday holatda jimgina o'tib ketamiz (xato chiqarmaymiz)."""
    if not BOT_TOKEN or not chat_id:
        return False
    payload = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json=payload, timeout=10).json()
        if not r.get("ok"):
            log.info("tg_send o'tkazib yuborildi (chat=%s): %s", chat_id, r.get("description"))
        return bool(r.get("ok"))
    except Exception as e:
        log.warning("tg_send: %s", e)
        return False

def _movie_links(movie_id):
    """Kinoga ikki havola: bot deep-link (start) va sayt sahifasi."""
    bot = f"https://t.me/{BOT_USERNAME}?start=movie_{movie_id}" if BOT_USERNAME else ""
    web = f"{BASE_URL}/kino/{movie_id}"
    return bot, web

def _watch_button(movie_id, label="▶️ Ko'rish"):
    bot, web = _movie_links(movie_id)
    target = bot or web
    return [[{"text": label, "url": target}]] if target else None

def _notify_reply(to_uid, from_name, movie_title, movie_id, text):
    """Kimdir izohga javob berganda — izoh egasiga bot orqali xabar (fon oqimida)."""
    snippet = text if len(text) <= 140 else text[:137] + "…"
    head = f"💬 <b>{html.escape(from_name or 'Kimdir')}</b> sizning izohingizga javob berdi"
    if movie_title:
        head += f" — <b>{html.escape(movie_title)}</b>"
    msg = head + ":\n\n" + f"<i>{html.escape(snippet)}</i>"
    btn = _watch_button(movie_id, "💬 Izohni ko'rish")
    threading.Thread(target=_tg_send, args=(to_uid, msg, btn), daemon=True).start()

def _notify_release(user_ids, title, movie_id):
    """'Tez orada' kino qo'shilganda — obuna bo'lganlarga bot orqali xabar (fon oqimida)."""
    user_ids = [u for u in dict.fromkeys(user_ids) if u]   # takrorlarni olib tashlash
    if not user_ids:
        return
    msg = ("🎉 <b>Kutilgan kino qo'shildi!</b>\n\n"
           f"<b>{html.escape(title or 'Kino')}</b> endi tomosha qilish uchun tayyor 👇")
    btn = _watch_button(movie_id, "▶️ Hoziroq ko'rish")
    def worker():
        ok = 0
        for u in user_ids:
            if _tg_send(u, msg, btn):
                ok += 1
            time.sleep(0.05)   # Telegram limitiga ehtiyot (~20-30 xabar/sek)
        log.info("Release bildirishnoma: %s/%s yuborildi (movie=%s)", ok, len(user_ids), movie_id)
    threading.Thread(target=worker, daemon=True).start()

def _all_known_user_ids():
    """Foydalanuvchi ID'larini avval `users` jadvalidan o'qiydi (tez).
    Agar bo'sh bo'lsa (masalan yangi deploy, users hali to'lmagan) —
    eski jadvallardan (favorites, reviews, upcoming_subs, notifications) zaxira sifatida yig'adi."""
    ids = set()
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT user_id FROM users")
                for (uid,) in cur.fetchall():
                    if uid:
                        ids.add(int(uid))
            except Exception as e:
                log.warning("_all_known_user_ids users: %s", e)
            if ids:
                return list(ids)
            # zaxira yo'l — eski usul
            for q in (
                "SELECT DISTINCT user_id FROM favorites",
                "SELECT DISTINCT user_id FROM reviews",
                "SELECT DISTINCT user_id FROM upcoming_subs",
                "SELECT DISTINCT user_id FROM notifications",
            ):
                try:
                    cur.execute(q)
                    for (uid,) in cur.fetchall():
                        if uid:
                            ids.add(int(uid))
                except Exception as e:
                    log.warning("_all_known_user_ids: %s", e)
    except Exception as e:
        log.warning("_all_known_user_ids conn: %s", e)
    return list(ids)

def _broadcast_to_all(text, button_label=None, button_url=None):
    """Barcha ma'lum foydalanuvchilarga bot orqali xabar + saytdagi bildirishnoma yuboradi (fon oqimida)."""
    user_ids = _all_known_user_ids()
    if not user_ids:
        return 0
    buttons = [[{"text": button_label, "url": button_url}]] if (button_label and button_url) else None
    def worker():
        ok = 0
        for u in user_ids:
            if _tg_send(u, text, buttons):
                ok += 1
            _add_notification(u, "broadcast", text)
            time.sleep(0.05)  # Telegram limitiga ehtiyot (~20 xabar/sek)
        log.info("Broadcast: %s/%s foydalanuvchiga yuborildi", ok, len(user_ids))
    threading.Thread(target=worker, daemon=True).start()
    return len(user_ids)

# ── Spam himoyasi: oxirgi amaldan beri yetarlicha vaqt o'tdimi? ────────────────
def _cooldown_left(table, user_col, uid, seconds):
    """Foydalanuvchining oxirgi yozuvidan beri 'seconds' o'tmagan bo'lsa,
    qolgan kutish vaqtini (sekund) qaytaradi; aks holda 0. (table/user_col — kod ichidagi sobit qiymatlar)"""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) FROM {table} WHERE {user_col}=%s",
                (uid,))
            r = cur.fetchone()
            if r and r[0] is not None and float(r[0]) < seconds:
                return int(seconds - float(r[0])) + 1
    except Exception:
        pass
    return 0

# ── Admin(lar)ga yangi kino so'rovi haqida bot xabari ─────────────────────────
def _admin_chat_ids():
    return [x.strip() for x in (ADMIN_CHAT_ID or "").replace(";", ",").split(",") if x.strip()]

def _notify_admins_request(title, who, uid):
    ids = _admin_chat_ids()
    if not ids:
        return
    msg = ("🆕 <b>Yangi kino so'rovi</b>\n\n"
           f"🎬 {html.escape(title)}\n"
           f"👤 {html.escape(who or 'Foydalanuvchi')} (id: {uid})\n\n"
           "Admin panel → «Tez orada» bo'limida ko'rishingiz mumkin.")
    def worker():
        for cid in ids:
            _tg_send(cid, msg)
    threading.Thread(target=worker, daemon=True).start()

# ── Saytdagi bildirishnoma (qo'ng'iroq) yozuvini qo'shish ─────────────────────
def _add_notification(user_id, ntype, text, movie_id=None):
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO notifications (user_id, type, text, movie_id) VALUES (%s,%s,%s,%s)",
                        (int(user_id), ntype, (text or "")[:300], movie_id))
            conn.commit()
    except Exception as e:
        log.warning("add_notification: %s", e)

# ── Baza (pg8000 — sof Python, libpq kerak emas) ──────────────────────────────
def _parse_db_url(url):
    """postgresql://user:pass@host:port/dbname → pg8000 parametrlari."""
    u = urlparse(url)
    return {
        "user": unquote(u.username) if u.username else None,
        "password": unquote(u.password) if u.password else None,
        "host": u.hostname,
        "port": u.port or 5432,
        "database": u.path.lstrip("/") if u.path else None,
        "ssl_context": True,  # Neon/Railway SSL talab qiladi
    }

class _Conn:
    def __enter__(self):
        params = _parse_db_url(DATABASE_URL)
        self.conn = pg8000.dbapi.connect(**params)
        return self.conn
    def __exit__(self, *a):
        try:
            self.conn.close()
        except Exception:
            pass

def get_conn():
    return _Conn()

# Botning movies jadvali allaqachon mavjud — biz faqat o'qiymiz/yozamiz.
# Qo'shimcha: poster uchun tashqi URL ustuni (agar kerak bo'lsa)
def init_db():
    if not DATABASE_URL:
        log.warning("DATABASE_URL yo'q")
        return
    # Har bir DDL alohida, xavfsiz bajariladi — bittasi xato bersa ham
    # qolganlari ishlashda davom etadi (masalan admin_log jadval yaratilmay qolmasin).
    ddls = [
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS poster_url TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS trailer TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS original_title TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS director TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS actors TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS country TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS duration INTEGER",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS age_rating TEXT",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS tmdb_rating NUMERIC",
        "ALTER TABLE movies ADD COLUMN IF NOT EXISTS lang_group TEXT",
        "CREATE INDEX IF NOT EXISTS idx_movies_lang_group ON movies(lang_group)",
        # Mavjud kinolarga (guruhsiz) — har biriga o'zining ID'siga teng noyob guruh beriladi.
        # Shu tufayli kelajakda ularga boshqa til versiyasini bog'lash mumkin bo'ladi.
        "UPDATE movies SET lang_group = 'm' || id::text WHERE lang_group IS NULL OR lang_group = ''",
        """CREATE TABLE IF NOT EXISTS site_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""",
        """CREATE TABLE IF NOT EXISTS favorites (
                user_id BIGINT NOT NULL,
                item_type TEXT NOT NULL,
                item_id TEXT NOT NULL,
                title TEXT,
                extra TEXT,
                added_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (user_id, item_type, item_id)
            )""",
        """CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                movie_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                user_name TEXT,
                user_photo TEXT,
                rating SMALLINT,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id)",
        "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS parent_id BIGINT",
        "CREATE INDEX IF NOT EXISTS idx_reviews_parent ON reviews(parent_id)",
        """CREATE TABLE IF NOT EXISTS upcoming (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                note TEXT,
                poster_url TEXT,
                status TEXT NOT NULL DEFAULT 'soon',
                movie_id BIGINT,
                created_by BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                released_at TIMESTAMP
            )""",
        "CREATE INDEX IF NOT EXISTS idx_upcoming_status ON upcoming(status)",
        """CREATE TABLE IF NOT EXISTS upcoming_subs (
                upcoming_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                user_name TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (upcoming_id, user_id)
            )""",
        """CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type TEXT NOT NULL,
                text TEXT NOT NULL,
                movie_id BIGINT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read)",
        """CREATE TABLE IF NOT EXISTS ads (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                image_url TEXT DEFAULT '',
                link TEXT DEFAULT '',
                placement TEXT DEFAULT 'all',
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        """CREATE TABLE IF NOT EXISTS tv_channels (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                logo_url TEXT DEFAULT '',
                stream_url TEXT DEFAULT '',
                source_type TEXT DEFAULT 'hls',
                category TEXT DEFAULT 'Umumiy',
                description TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "ALTER TABLE tv_channels ADD COLUMN IF NOT EXISTS source_type TEXT DEFAULT 'hls'",
        """CREATE TABLE IF NOT EXISTS tv_viewers (
                channel_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                last_seen TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (channel_id, session_id)
            )""",
        # Ma'lum foydalanuvchilar — login/harakat vaqtida upsert qilinadi.
        """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                username TEXT,
                first_seen TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)",
        # Poster keshi — Telegram file_id'dan olingan rasm baytlarini DB'da saqlaymiz.
        """CREATE TABLE IF NOT EXISTS poster_cache (
                poster_id TEXT PRIMARY KEY,
                content_type TEXT,
                data BYTEA,
                cached_at TIMESTAMP DEFAULT NOW()
            )""",
        # Admin harakatlar jurnali — kim, qachon, nima qildi
        """CREATE TABLE IF NOT EXISTS admin_log (
                id SERIAL PRIMARY KEY,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS idx_admin_log_date ON admin_log(created_at DESC)",
    ]
    ok_count = 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            for ddl in ddls:
                try:
                    cur.execute(ddl)
                    conn.commit()
                    ok_count += 1
                except Exception as e:
                    conn.rollback()
                    log.warning("init_db DDL xato: %s | %s", e, ddl.strip()[:60])
        log.info("Kino baza tayyor (%s/%s DDL bajarildi)", ok_count, len(ddls))
    except Exception as e:
        log.warning("init_db conn: %s", e)

# Fon effektlari — standart holat (admin o'zgartirmaguncha)
FX_DEFAULTS = {
    "fx_glow": "1",
    "fx_bigstars": "1",
    "fx_parallax": "1",
    "fx_planet": "1",
    "fx_stardust": "1",
    "fx_aurora": "1",
    "fx_constellation": "1",
    "fx_orbs": "1",
    "fx_meteors": "1",
    "fx_hueshift": "1",
}

def _read_settings():
    out = dict(FX_DEFAULTS)
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM site_settings")
            for k, v in cur.fetchall():
                out[k] = v
    except Exception:
        pass
    return out

def _save_settings(d):
    with get_conn() as conn:
        cur = conn.cursor()
        for k in FX_DEFAULTS:
            val = "1" if d.get(k) else "0"
            cur.execute("""
                INSERT INTO site_settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (k, val))
        conn.commit()

# ── Sahifa ──
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.after_request
def cors(resp):
    p = request.path or ""
    if p.startswith("/api/admin/"):
        # Admin API — faqat sayt o'zidan (cookie-based session) ishlatiladi,
        # tashqi domenlarga ochiq bo'lishi shart emas.
        resp.headers.pop("Access-Control-Allow-Origin", None)
    else:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    p = request.path or ""
    # Statik aktivlar uchun brauzer keshi (takroriy tashriflar — tezroq)
    if p.startswith("/static/"):
        if p.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff2", ".woff", ".ttf")):
            resp.headers["Cache-Control"] = "public, max-age=604800"   # 7 kun — rasm/font/ikonka
        elif p.endswith((".js", ".css")):
            resp.headers["Cache-Control"] = "public, max-age=3600"      # 1 soat — JS/CSS (deploy'dan keyin tez yangilanadi)
    # Gzip siqish — matn/JS/CSS/JSON/XML javoblarini ~70% kichraytiradi.
    # To'liq himoyalangan: xato bo'lsa javob asl holicha qaytadi.
    try:
        ae = request.headers.get("Accept-Encoding", "") or ""
        ctype = resp.headers.get("Content-Type", "") or ""
        compressible = (ctype.startswith("text/") or "javascript" in ctype
                        or "json" in ctype or "xml" in ctype or "svg" in ctype)
        if ("gzip" in ae.lower() and resp.status_code == 200
                and "Content-Encoding" not in resp.headers and compressible):
            if getattr(resp, "direct_passthrough", False):
                resp.direct_passthrough = False
            data = resp.get_data()
            if data and len(data) > 500:
                comp = _gzip.compress(data, 6)
                resp.set_data(comp)
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Vary"] = "Accept-Encoding"
    except Exception:
        pass
    return resp

# ── Kinolar ro'yxati (filtr/qidiruv bilan) ───────────────────────────────────
@app.route("/api/movies")
@rate_limit(max_requests=60, window=60)
def api_movies():
    q = (request.args.get("q") or "").strip()
    ctype = (request.args.get("type") or "").strip()
    genre = (request.args.get("genre") or "").strip()
    year = (request.args.get("year") or "").strip()
    quality = (request.args.get("quality") or "").strip()
    language = (request.args.get("language") or "").strip()
    sort = (request.args.get("sort") or "new").strip()
    ids = (request.args.get("ids") or "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    per = 24
    offset = (page - 1) * per
    try:
        where = []
        params = []
        if q:
            where.append("(title ILIKE %s OR description ILIKE %s OR actors ILIKE %s OR director ILIKE %s)")
            params += [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"]
        if ctype and ctype != "all":
            where.append("COALESCE(content_type,'movie') = %s")
            params.append(ctype)
        if genre and genre != "all":
            where.append("genre ILIKE %s")
            params.append(f"%{genre}%")
        if year and year != "all":
            try:
                yv = int(year)
                where.append("year = %s")
                params.append(yv)
            except Exception:
                pass
        if quality and quality != "all":
            where.append("quality ILIKE %s")
            params.append(f"%{quality}%")
        if language and language != "all":
            where.append("language ILIKE %s")
            params.append(f"%{language}%")
        if (request.args.get("rated") or "") == "1":
            where.append("rating IS NOT NULL AND rating > 0")
        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:60]
            if id_list:
                ph = ",".join(["%s"] * len(id_list))
                where.append(f"id IN ({ph})")
                params += id_list
        order = {
            "new": "created_at DESC NULLS LAST, id DESC",
            "old": "created_at ASC NULLS LAST, id ASC",
            "popular": "COALESCE(views,0) DESC, id DESC",
            "rating": "rating DESC NULLS LAST, id DESC",
            "title": "title ASC",
        }.get(sort, "created_at DESC NULLS LAST, id DESC")
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        # "ids" bilan aniq id'lar so'ralganda (masalan sevimlilar) guruhlashsiz, aynan shu
        # yozuvlarni qaytaramiz — foydalanuvchi aynan shu tilni saqlagan bo'lishi mumkin.
        skip_group = bool(ids)
        with get_conn() as conn:
            cur = conn.cursor()
            if skip_group:
                cur.execute(f"SELECT COUNT(*) FROM movies{wsql}", params)
                total = cur.fetchone()[0]
                cur.execute(f"""
                    SELECT id, title, genre, year, language, quality,
                           COALESCE(content_type,'movie'), poster_id,
                           COALESCE(views,0), rating, poster_url,
                           COALESCE(is_premium, FALSE), lang_group
                    FROM movies{wsql}
                    ORDER BY {order}
                    LIMIT %s OFFSET %s
                """, params + [per, offset])
                rows = cur.fetchall()
            else:
                # Bir xil lang_group'dagi til versiyalari — bitta kartochkaga birlashtiriladi.
                # Guruh ichida O'zbek tilidagi versiya ustuvor (kartochka shu asosida ko'rsatiladi).
                cur.execute(f"SELECT COUNT(DISTINCT lang_group) FROM movies{wsql}", params)
                total = cur.fetchone()[0]
                cur.execute(f"""
                    SELECT id, title, genre, year, language, quality,
                           content_type, poster_id, views, rating, poster_url,
                           is_premium, lang_group
                    FROM (
                        SELECT DISTINCT ON (lang_group)
                            id, title, genre, year, language, quality,
                            COALESCE(content_type,'movie') AS content_type, poster_id,
                            COALESCE(views,0) AS views, rating, poster_url,
                            COALESCE(is_premium, FALSE) AS is_premium,
                            lang_group, created_at
                        FROM movies{wsql}
                        ORDER BY lang_group,
                            CASE WHEN language ILIKE '%zbek%' THEN 0 ELSE 1 END,
                            id ASC
                    ) picked
                    ORDER BY {order}
                    LIMIT %s OFFSET %s
                """, params + [per, offset])
                rows = cur.fetchall()

            # Ko'rinayotgan kartochkalarning har biri uchun guruhdagi barcha tillarni yig'amiz
            lang_groups = list({r[12] for r in rows if r[12]})
            langs_map = {}
            if lang_groups:
                ph = ",".join(["%s"] * len(lang_groups))
                cur.execute(f"SELECT lang_group, language FROM movies WHERE lang_group IN ({ph})", lang_groups)
                for gr, lg in cur.fetchall():
                    lg = (lg or "").strip()
                    lst = langs_map.setdefault(gr, [])
                    if lg and lg not in lst:
                        lst.append(lg)
        movies = [{
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "has_poster": bool(r[7]), "poster_url": r[10] or "",
            "views": r[8], "rating": float(r[9]) if r[9] else None,
            "is_premium": bool(r[11]),
            "languages": langs_map.get(r[12], [r[4]] if r[4] else []),
        } for r in rows]
        return jsonify({"movies": movies, "total": total, "page": page,
                        "pages": (total + per - 1) // per})
    except Exception as e:
        log.warning("movies: %s", e)
        return jsonify({"movies": [], "total": 0, "error": str(e)})

# ── Bitta kino ──
@app.route("/api/admin/lang/list", methods=["POST"])
def admin_lang_list():
    """Berilgan kinoning lang_group'idagi barcha til versiyalarini qaytaradi.
    Ko'rishlar sonini OSHIRMAYDI (admin panel uchun, /api/movie/<id> dan farqli).
    Body: {password, id}"""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        mid = int(d.get("id"))
    except Exception:
        return jsonify({"error": "id kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT lang_group FROM movies WHERE id=%s", (mid,))
            r = cur.fetchone()
            if not r:
                return jsonify({"error": "Topilmadi"}), 404
            group = r[0]
            langs = []
            if group:
                cur.execute("""
                    SELECT id, language FROM movies
                    WHERE lang_group=%s ORDER BY language
                """, (group,))
                langs = [{"id": lr[0], "language": lr[1] or ""} for lr in cur.fetchall()]
        return jsonify({"languages": langs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/movie/<int:mid>")
@rate_limit(max_requests=60, window=60)
def api_movie(mid):
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, genre, year, language, quality, description,
                       COALESCE(content_type,'movie'), poster_id,
                       COALESCE(views,0), rating, poster_url, trailer, original_title,
                       director, actors, country, duration, age_rating, tmdb_rating, lang_group
                FROM movies WHERE id=%s
            """, (mid,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE movies SET views = COALESCE(views,0)+1 WHERE id=%s", (mid,))
                conn.commit()
            langs = []
            if r and r[20]:
                cur.execute("""
                    SELECT id, language FROM movies
                    WHERE lang_group=%s ORDER BY language
                """, (r[20],))
                langs = [{"id": lr[0], "language": lr[1] or ""} for lr in cur.fetchall()]
        if not r:
            return jsonify({"found": False}), 404
        return jsonify({"found": True, "movie": {
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "description": r[6] or "",
            "type": r[7], "has_poster": bool(r[8]), "poster_url": r[11] or "",
            "views": r[9], "rating": float(r[10]) if r[10] else None,
            "trailer": (r[12] or "") if len(r) > 12 else "",
            "original_title": (r[13] or "") if len(r) > 13 else "",
            "director": (r[14] or "") if len(r) > 14 else "",
            "actors": (r[15] or "") if len(r) > 15 else "",
            "country": (r[16] or "") if len(r) > 16 else "",
            "duration": r[17] if len(r) > 17 else None,
            "age_rating": (r[18] or "") if len(r) > 18 else "",
            "tmdb_rating": float(r[19]) if len(r) > 19 and r[19] else None,
            "lang_group": r[20] or "",
            "languages": langs,   # shu kinoning boshqa til versiyalari (o'zi ham kiradi)
        }})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 500

# ── Poster proxy (Telegram file_id → rasm) ────────────────────────────────────
def _poster_db_get(pid):
    """DB keshdan poster o'qiydi (agar muddati o'tmagan bo'lsa)."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT data, content_type FROM poster_cache
                           WHERE poster_id=%s AND cached_at > NOW() - INTERVAL '7 days'""", (pid,))
            r = cur.fetchone()
            if r and r[0]:
                return bytes(r[0]), r[1] or "image/jpeg"
    except Exception as e:
        log.warning("poster_db_get: %s", e)
    return None

def _poster_db_save(pid, data, ct):
    """Poster baytlarini DB keshga yozadi (fon oqimida — javobni sekinlashtirmaslik uchun)."""
    def worker():
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO poster_cache (poster_id, content_type, data, cached_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (poster_id) DO UPDATE SET
                        content_type = EXCLUDED.content_type,
                        data = EXCLUDED.data,
                        cached_at = NOW()
                """, (pid, ct, data))
                conn.commit()
        except Exception as e:
            log.warning("poster_db_save: %s", e)
    threading.Thread(target=worker, daemon=True).start()

@app.route("/api/poster/<int:mid>")
@rate_limit(max_requests=200, window=60)
def api_poster(mid):
    """Telegram'dagi poster_id rasmni web uchun proxy qiladi.
    Uch qatlamli kesh: xotira (eng tez) → DB (Railway qayta ishga tushsa ham saqlanadi) → Telegram."""
    if not BOT_TOKEN:
        log.warning("Poster: BOT_TOKEN yo'q! Railway Variables'ga BOT_TOKEN qo'shing.")
        return redirect("/static/no-poster.svg")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT poster_id, poster_url FROM movies WHERE id=%s", (mid,))
            r = cur.fetchone()
        if not r:
            return redirect("/static/no-poster.svg")
        # Avval tashqi URL (saytdan qo'shilgan) — ishonchli
        if r[1]:
            return redirect(r[1])
        if not r[0]:
            return redirect("/static/no-poster.svg")
        pid = r[0]
        now = time.time()
        # 1-qatlam: xotira keshi
        hit = _poster_cache.get(pid)
        if hit and (now - hit[2] < _POSTER_TTL):
            return Response(hit[0], mimetype=hit[1],
                            headers={"Cache-Control": "public, max-age=604800"})
        # 2-qatlam: DB keshi (Railway qayta ishga tushganda ham saqlanadi)
        db_hit = _poster_db_get(pid)
        if db_hit:
            data, ct = db_hit
            try:
                with _poster_lock:
                    if len(_poster_cache) >= _POSTER_MAX:
                        oldest = min(_poster_cache, key=lambda k: _poster_cache[k][2])
                        _poster_cache.pop(oldest, None)
                    _poster_cache[pid] = (data, ct, now)
            except Exception:
                pass
            return Response(data, mimetype=ct,
                            headers={"Cache-Control": "public, max-age=604800"})
        # 3-qatlam: Telegram'dan file path olamiz
        fr = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                          params={"file_id": pid}, timeout=10).json()
        if not fr.get("ok"):
            log.warning("Poster getFile xato (id=%s): %s", mid, fr.get("description", fr))
            return redirect("/static/no-poster.svg")
        fpath = fr["result"]["file_path"]
        img = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}", timeout=15)
        if img.status_code != 200:
            log.warning("Poster yuklab bo'lmadi (id=%s): status %s", mid, img.status_code)
            return redirect("/static/no-poster.svg")
        ct = img.headers.get("Content-Type", "image/jpeg")
        # Xotira keshiga saqlaymiz (hajmni cheklab)
        try:
            with _poster_lock:
                if len(_poster_cache) >= _POSTER_MAX:
                    oldest = min(_poster_cache, key=lambda k: _poster_cache[k][2])
                    _poster_cache.pop(oldest, None)
                _poster_cache[pid] = (img.content, ct, now)
        except Exception:
            pass
        # DB keshiga ham saqlaymiz (fon oqimida)
        _poster_db_save(pid, img.content, ct)
        return Response(img.content, mimetype=ct,
                        headers={"Cache-Control": "public, max-age=604800"})
    except Exception as e:
        log.warning("Poster xato (id=%s): %s", mid, e)
        return redirect("/static/no-poster.svg")

# ── Janrlar ro'yxati ──
@app.route("/api/genres")
def api_genres():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT genre FROM movies WHERE genre IS NOT NULL AND genre <> ''")
            rows = cur.fetchall()
        # Har bir kinoning janr satrini vergul bo'yicha ajratamiz va noyob janrlar ro'yxatini tuzamiz
        seen = {}
        for row in rows:
            for part in (row[0] or "").split(","):
                name = part.strip()
                if not name:
                    continue
                key = name.lower()
                if key not in seen:
                    seen[key] = name
        genres = sorted(seen.values(), key=lambda s: s.lower())
        return jsonify({"genres": genres})
    except Exception:
        return jsonify({"genres": []})

# ── Filtr variantlari (yil, sifat, til) ──
@app.route("/api/filters")
def api_filters():
    out = {"years": [], "qualities": [], "languages": []}
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT year FROM movies WHERE year IS NOT NULL ORDER BY year DESC")
            out["years"] = [r[0] for r in cur.fetchall() if r[0]]
            cur.execute("SELECT DISTINCT quality FROM movies WHERE quality IS NOT NULL AND quality <> '' ORDER BY quality")
            out["qualities"] = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT language FROM movies WHERE language IS NOT NULL AND language <> '' ORDER BY language")
            out["languages"] = [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning("filters: %s", e)
    return jsonify(out)

# ── Fon effektlari sozlamalari (ommaviy o'qish) ──
@app.route("/api/settings")
def api_settings():
    return jsonify(_read_settings())

# ── Fon effektlari sozlamalari (admin yozadi) ──
@app.route("/api/admin/settings", methods=["POST"])
def admin_settings():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    if d.get("read"):
        return jsonify(_read_settings())
    try:
        _save_settings(d)
        return jsonify({"ok": True, "settings": _read_settings()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Botga yo'naltirish havolasi ──
@app.route("/api/botlink")
def api_botlink():
    return jsonify({"bot": BOT_USERNAME})

# ── Telegram orqali kirish (login) ────────────────────────────────────────────
def _verify_telegram_auth(data):
    """Telegram Login Widget ma'lumotlarini hash orqali tekshiradi."""
    if not BOT_TOKEN:
        return False
    recv_hash = data.get("hash", "")
    if not recv_hash:
        return False
    pairs = [f"{k}={data[k]}" for k in sorted(data.keys()) if k != "hash"]
    data_check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, recv_hash):
        return False
    try:
        if time.time() - int(data.get("auth_date", "0")) > 86400:
            return False
    except Exception:
        return False
    return True

def _touch_user(uid, name=None, username=None):
    """Foydalanuvchini users jadvalida yaratadi/yangilaydi (login yoki harakat vaqtida)."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO users (user_id, name, username, first_seen, last_seen)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    name = COALESCE(NULLIF(EXCLUDED.name, ''), users.name),
                    username = COALESCE(NULLIF(EXCLUDED.username, ''), users.username),
                    last_seen = NOW()
            """, (int(uid), name or None, username or None))
            conn.commit()
    except Exception as e:
        log.warning("touch_user: %s", e)

@app.route("/api/tg-login", methods=["POST"])
@rate_limit(max_requests=20, window=60)
def tg_login():
    data = request.get_json(silent=True) or {}
    clean = {k: str(v) for k, v in data.items() if v is not None}
    if not _verify_telegram_auth(clean):
        return jsonify({"ok": False, "error": "Tekshiruvdan o'tmadi"}), 403
    name = (clean.get("first_name", "") + " " + clean.get("last_name", "")).strip() \
        or clean.get("username", "Foydalanuvchi")
    session.permanent = True
    session["tg_id"] = int(clean["id"])
    session["tg_name"] = name
    session["tg_photo"] = clean.get("photo_url", "")
    session["tg_username"] = clean.get("username", "")
    _touch_user(session["tg_id"], name, clean.get("username", ""))
    return jsonify({"ok": True, "id": session["tg_id"], "name": name, "photo": session["tg_photo"]})

@app.route("/api/me")
def api_me():
    if session.get("tg_id"):
        return jsonify({"logged_in": True, "id": session["tg_id"],
                        "name": session.get("tg_name", ""),
                        "photo": session.get("tg_photo", ""),
                        "username": session.get("tg_username", ""),
                        "bot": BOT_USERNAME})
    return jsonify({"logged_in": False, "bot": BOT_USERNAME})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})

# ── Sevimlilar (bot bilan umumiy baza, telegram ID bo'yicha) ──────────────────
@app.route("/api/favorites", methods=["GET", "POST", "DELETE"])
def api_favorites():
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"error": "login kerak", "logged_in": False}), 401
    if request.method == "GET":
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT item_id FROM favorites WHERE user_id=%s AND item_type='movie' "
                            "ORDER BY added_at DESC", (uid,))
                ids = [int(r[0]) for r in cur.fetchall() if str(r[0]).isdigit()]
            return jsonify({"ids": ids})
        except Exception as e:
            log.warning("favorites get: %s", e)
            return jsonify({"ids": []})
    data = request.get_json(silent=True) or {}
    mid = data.get("id")
    if mid is None:
        return jsonify({"error": "id kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if request.method == "POST":
                title = str(mid)
                try:
                    cur.execute("SELECT title FROM movies WHERE id=%s", (mid,))
                    r = cur.fetchone()
                    if r and r[0]:
                        title = r[0]
                except Exception:
                    pass
                cur.execute("INSERT INTO favorites (user_id, item_type, item_id, title, extra) "
                            "VALUES (%s, 'movie', %s, %s, '') "
                            "ON CONFLICT (user_id, item_type, item_id) DO NOTHING",
                            (uid, str(mid), title))
            else:  # DELETE
                cur.execute("DELETE FROM favorites WHERE user_id=%s AND item_type='movie' AND item_id=%s",
                            (uid, str(mid)))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("favorites mod: %s", e)
        return jsonify({"error": "xato"}), 500

# ── Kino izohlari (fikr bildirish) ────────────────────────────────────────────
@app.route("/api/reviews/<int:mid>", methods=["GET"])
@rate_limit(max_requests=60, window=60)
def api_reviews_get(mid):
    """Kino izohlari (hammaga ochiq). Javoblar asosiy izoh ostiga joylanadi (thread)."""
    me = session.get("tg_id")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, user_id, user_name, user_photo, rating, text, created_at, parent_id "
                        "FROM reviews WHERE movie_id=%s ORDER BY created_at ASC LIMIT 500", (mid,))
            rows = cur.fetchall()
        # id -> izoh ob'ekti
        items = {}
        order = []
        for r in rows:
            items[r[0]] = {
                "id": r[0], "_uid": int(r[1]),
                "name": r[2] or "Foydalanuvchi", "photo": r[3] or "",
                "rating": r[4] or 0, "text": r[5] or "",
                "date": r[6].strftime("%Y-%m-%d") if r[6] else "",
                "mine": bool(me and int(r[1]) == int(me)),
                "_parent": r[7], "replies": [],
            }
            order.append(r[0])
        tops = []
        for rid in order:
            it = items[rid]
            pid = it["_parent"]
            if pid and pid in items:
                # javobni eng yuqori (asosiy) izoh ostiga biriktiramiz
                anc = items[pid]
                it["reply_to"] = anc["name"]
                while anc.get("_parent") and anc["_parent"] in items:
                    anc = items[anc["_parent"]]
                anc["replies"].append(it)
            else:
                tops.append(it)
        # asosiy izohlar — yangidan eskiga; javoblar — eskidan yangiga (suhbat tartibi)
        tops.sort(key=lambda x: x["id"], reverse=True)
        def _clean(d):
            d.pop("_uid", None); d.pop("_parent", None)
            return d
        out = []
        for t in tops:
            t["replies"] = [_clean(x) for x in t["replies"]]
            out.append(_clean(t))
        return jsonify({"reviews": out, "count": len(rows), "logged_in": bool(me)})
    except Exception as e:
        log.warning("reviews get: %s", e)
        return jsonify({"reviews": [], "count": 0, "logged_in": bool(me)})


@app.route("/api/reviews", methods=["POST"])
def api_reviews_add():
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"error": "login kerak", "logged_in": False}), 401
    data = request.get_json(silent=True) or {}
    mid = data.get("movie_id")
    text = (data.get("text") or "").strip()
    try:
        parent_id = int(data.get("parent_id")) if data.get("parent_id") else None
    except Exception:
        parent_id = None
    try:
        rating = int(data.get("rating") or 0)
    except Exception:
        rating = 0
    rating = max(0, min(5, rating))
    # Javobga reyting qo'yilmaydi (faqat asosiy izohga)
    if parent_id:
        rating = 0
    if not mid or not text:
        return jsonify({"error": "matn kerak"}), 400
    text = text[:1000]
    # Spam himoyasi — izohlar orasida minimal vaqt
    wait = _cooldown_left("reviews", "user_id", uid, REVIEW_COOLDOWN)
    if wait:
        return jsonify({"error": f"Biroz sekinroq — {wait}s dan keyin yana yozishingiz mumkin",
                        "cooldown": wait}), 429
    my_name = session.get("tg_name", "") or "Foydalanuvchi"
    notify_uid = None
    movie_title = ""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO reviews (movie_id, user_id, user_name, user_photo, rating, text, parent_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (int(mid), uid, my_name, session.get("tg_photo", ""),
                         rating, text, parent_id))
            rid = cur.fetchone()[0]
            # Javob bo'lsa — asosiy izoh egasini va kino nomini aniqlaymiz (bildirishnoma uchun)
            if parent_id:
                cur.execute("SELECT user_id FROM reviews WHERE id=%s", (parent_id,))
                pr = cur.fetchone()
                if pr:
                    notify_uid = int(pr[0])
                cur.execute("SELECT title FROM movies WHERE id=%s", (int(mid),))
                mt = cur.fetchone()
                if mt:
                    movie_title = mt[0] or ""
            conn.commit()
        # O'ziga o'zi javob yozsa — xabar bermaymiz
        if parent_id and notify_uid and notify_uid != int(uid):
            _notify_reply(notify_uid, my_name, movie_title, int(mid), text)
            _add_notification(notify_uid, "reply",
                              f"💬 {my_name} izohingizga javob berdi"
                              + (f" — {movie_title}" if movie_title else ""), int(mid))
        return jsonify({"ok": True, "id": rid})
    except Exception as e:
        log.warning("reviews add: %s", e)
        return jsonify({"error": "xato"}), 500


@app.route("/api/reviews/<int:rid>", methods=["DELETE"])
def api_reviews_del(rid):
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"error": "login kerak"}), 401
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # Faqat o'z izohini o'chira oladi
            cur.execute("DELETE FROM reviews WHERE id=%s AND user_id=%s", (rid, uid))
            deleted = cur.rowcount
            # Asosiy izoh HAQIQATDA shu foydalanuvchi tomonidan o'chirilgan bo'lsagina,
            # unga yozilgan javoblarni ham o'chiramiz (aks holda begona izoh/javoblarni
            # o'chirishga urinish orqali boshqa foydalanuvchi thread'ini buzish mumkin edi).
            if deleted:
                cur.execute("DELETE FROM reviews WHERE parent_id=%s", (rid,))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("reviews del: %s", e)
        return jsonify({"error": "xato"}), 500

# ── "Tez orada" (kutilayotgan kinolar) ────────────────────────────────────────
@app.route("/api/upcoming")
def api_upcoming():
    """Ommaviy ro'yxat — saytda ko'rsatiladigan (status='soon') kutilayotgan kinolar."""
    me = session.get("tg_id")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.title, u.note, u.poster_url, u.created_at,
                       (SELECT COUNT(*) FROM upcoming_subs s WHERE s.upcoming_id = u.id) AS subs,
                       (SELECT COUNT(*) FROM upcoming_subs s WHERE s.upcoming_id = u.id AND s.user_id = %s) AS mine
                FROM upcoming u
                WHERE u.status = 'soon'
                ORDER BY u.created_at DESC
                LIMIT 100
            """, (me if me else 0,))
            rows = cur.fetchall()
        items = [{
            "id": r[0], "title": r[1], "note": r[2] or "", "poster_url": r[3] or "",
            "subs": int(r[5] or 0), "subscribed": bool(r[6]),
        } for r in rows]
        return jsonify({"items": items, "logged_in": bool(me)})
    except Exception as e:
        log.warning("upcoming: %s", e)
        return jsonify({"items": [], "logged_in": bool(me)})

@app.route("/api/upcoming/<int:up_id>/subscribe", methods=["POST", "DELETE"])
def api_upcoming_sub(up_id):
    """Foydalanuvchi 'Xabar ber' bossa — obuna bo'ladi; qaytadan bossa — bekor qiladi."""
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"error": "login kerak", "logged_in": False}), 401
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title, status FROM upcoming WHERE id=%s", (up_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "topilmadi"}), 404
            if request.method == "POST":
                cur.execute("INSERT INTO upcoming_subs (upcoming_id, user_id, user_name) "
                            "VALUES (%s, %s, %s) ON CONFLICT (upcoming_id, user_id) DO NOTHING",
                            (up_id, uid, session.get("tg_name", "")))
            else:
                cur.execute("DELETE FROM upcoming_subs WHERE upcoming_id=%s AND user_id=%s", (up_id, uid))
            conn.commit()
        subscribed = (request.method == "POST")
        # Obuna bo'lganini bot orqali tasdiqlaymiz (start qilgan bo'lsa keladi)
        if subscribed:
            title = row[0] or "kino"
            threading.Thread(
                target=_tg_send,
                args=(uid, f"🔔 <b>{html.escape(title)}</b> qo'shilganda sizga birinchi bo'lib xabar beramiz!"),
                daemon=True).start()
        return jsonify({"ok": True, "subscribed": subscribed})
    except Exception as e:
        log.warning("upcoming sub: %s", e)
        return jsonify({"error": "xato"}), 500

@app.route("/api/upcoming/request", methods=["POST"])
def api_upcoming_request():
    """Foydalanuvchi yangi kino so'raydi — admin tasdiqlagunча 'pending' bo'lib turadi.
    So'ragan odam avtomatik obuna bo'ladi (qo'shilganda xabar oladi)."""
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"error": "login kerak", "logged_in": False}), 401
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:200]
    if not title:
        return jsonify({"error": "nom kerak"}), 400
    note = (data.get("note") or "").strip()[:300]
    # Spam himoyasi — so'rovlar orasida minimal vaqt
    wait = _cooldown_left("upcoming", "created_by", uid, REQUEST_COOLDOWN)
    if wait:
        return jsonify({"error": f"Biroz sekinroq — {wait}s dan keyin yana so'rashingiz mumkin",
                        "cooldown": wait}), 429
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            # Shu nomdagi 'pending' yoki 'soon' allaqachon bormi? Bo'lsa — yangi yaratmaymiz,
            # mavjud yozuvga obuna qilamiz (hammasi bitta yozuvda yig'iladi, hammasiga xabar boradi)
            cur.execute("SELECT id FROM upcoming WHERE LOWER(TRIM(title)) = LOWER(TRIM(%s)) "
                        "AND status IN ('pending','soon') ORDER BY id LIMIT 1", (title,))
            row = cur.fetchone()
            if row:
                target_id = row[0]
            else:
                cur.execute("INSERT INTO upcoming (title, note, status, created_by) "
                            "VALUES (%s, %s, 'pending', %s) RETURNING id", (title, note, uid))
                target_id = cur.fetchone()[0]
            cur.execute("INSERT INTO upcoming_subs (upcoming_id, user_id, user_name) "
                        "VALUES (%s, %s, %s) ON CONFLICT (upcoming_id, user_id) DO NOTHING",
                        (target_id, uid, session.get("tg_name", "")))
            conn.commit()
        # Yangi (takror bo'lmagan) so'rov bo'lsa — adminga xabar beramiz
        if not row:
            _notify_admins_request(title, session.get("tg_name", ""), uid)
        return jsonify({"ok": True, "merged": bool(row)})
    except Exception as e:
        log.warning("upcoming request: %s", e)
        return jsonify({"error": "xato"}), 500

# ── "Tez orada" — admin boshqaruvi ────────────────────────────────────────────
@app.route("/api/admin/upcoming/list", methods=["POST"])
def admin_upcoming_list():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.title, u.note, u.poster_url, u.status, u.movie_id, u.created_at,
                       (SELECT COUNT(*) FROM upcoming_subs s WHERE s.upcoming_id = u.id) AS subs
                FROM upcoming u
                ORDER BY CASE u.status WHEN 'pending' THEN 0 WHEN 'soon' THEN 1 ELSE 2 END,
                         u.created_at DESC
                LIMIT 300
            """)
            rows = cur.fetchall()
        items = [{
            "id": r[0], "title": r[1], "note": r[2] or "", "poster_url": r[3] or "",
            "status": r[4], "movie_id": r[5], "subs": int(r[7] or 0),
            "date": r[6].strftime("%Y-%m-%d") if r[6] else "",
        } for r in rows]
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/upcoming/save", methods=["POST"])
def admin_upcoming_save():
    """Yangi 'Tez orada' qo'shish yoki mavjudini tahrirlash (id berilsa)."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    title = (d.get("title") or "").strip()[:200]
    if not title:
        return jsonify({"error": "nom kerak"}), 400
    note = (d.get("note") or "").strip()[:300]
    poster = (d.get("poster_url") or "").strip()
    status = d.get("status") or "soon"
    if status not in ("pending", "soon"):
        status = "soon"
    up_id = d.get("id")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if up_id:
                cur.execute("UPDATE upcoming SET title=%s, note=%s, poster_url=%s, status=%s "
                            "WHERE id=%s AND status <> 'released'",
                            (title, note, poster, status, int(up_id)))
            else:
                cur.execute("INSERT INTO upcoming (title, note, poster_url, status) "
                            "VALUES (%s, %s, %s, %s)", (title, note, poster, status))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/upcoming/delete", methods=["POST"])
def admin_upcoming_delete():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM upcoming_subs WHERE upcoming_id=%s", (int(d.get("id")),))
            cur.execute("DELETE FROM upcoming WHERE id=%s", (int(d.get("id")),))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/upcoming/release", methods=["POST"])
def admin_upcoming_release():
    """Kutilgan kino qo'shildi → mavjud kinoga bog'laymiz va obunachilarga xabar yuboramiz."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    up_id = d.get("id")
    movie_id = d.get("movie_id")
    if not up_id or not movie_id:
        return jsonify({"error": "id va movie_id kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title, status FROM upcoming WHERE id=%s", (int(up_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "topilmadi"}), 404
            if row[1] == "released":
                return jsonify({"error": "allaqachon chiqarilgan"}), 400
            title = row[0] or ""
            # kino mavjudligini tekshiramiz + asl nomни olamiz
            cur.execute("SELECT title FROM movies WHERE id=%s", (int(movie_id),))
            mv = cur.fetchone()
            if not mv:
                return jsonify({"error": "bunday ID'li kino yo'q"}), 400
            cur.execute("SELECT user_id FROM upcoming_subs WHERE upcoming_id=%s", (int(up_id),))
            subs = [int(r[0]) for r in cur.fetchall()]
            cur.execute("UPDATE upcoming SET status='released', movie_id=%s, released_at=NOW() WHERE id=%s",
                        (int(movie_id), int(up_id)))
            # Saytdagi bildirishnoma (qo'ng'iroq) — har bir obunachiga
            note_text = f"🎉 «{title}» qo'shildi! Hoziroq ko'ring"
            for u in subs:
                cur.execute("INSERT INTO notifications (user_id, type, text, movie_id) VALUES (%s,'release',%s,%s)",
                            (u, note_text, int(movie_id)))
            conn.commit()
        _notify_release(subs, title, int(movie_id))
        return jsonify({"ok": True, "notified": len(subs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Saytdagi bildirishnomalar (qo'ng'iroq) ────────────────────────────────────
@app.route("/api/notifications")
def api_notifications():
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"items": [], "unread": 0, "logged_in": False})
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, type, text, movie_id, is_read, created_at FROM notifications "
                        "WHERE user_id=%s ORDER BY created_at DESC LIMIT 30", (uid,))
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id=%s AND is_read=FALSE", (uid,))
            unread = cur.fetchone()[0]
        items = [{
            "id": r[0], "type": r[1], "text": r[2], "movie_id": r[3],
            "read": bool(r[4]),
            "date": r[5].strftime("%d.%m %H:%M") if r[5] else "",
        } for r in rows]
        return jsonify({"items": items, "unread": int(unread or 0), "logged_in": True})
    except Exception as e:
        log.warning("notifications: %s", e)
        return jsonify({"items": [], "unread": 0, "logged_in": True})

@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    uid = session.get("tg_id")
    if not uid:
        return jsonify({"error": "login kerak"}), 401
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s AND is_read=FALSE", (uid,))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("notif read: %s", e)
        return jsonify({"error": "xato"}), 500

# ══════════════════ REKLAMA (sayt + bot umumiy) ══════════════════
@app.route("/api/ad")
def api_ad():
    """Sayt uchun bitta faol reklama (placement: site yoki all)."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, title, image_url, link FROM ads "
                        "WHERE is_active=TRUE AND placement IN ('site','all') "
                        "ORDER BY RANDOM() LIMIT 1")
            r = cur.fetchone()
        if not r:
            return jsonify({"ad": None})
        return jsonify({"ad": {"id": r[0], "title": r[1], "image_url": r[2] or "", "link": r[3] or ""}})
    except Exception as e:
        log.warning("api_ad: %s", e)
        return jsonify({"ad": None})

@app.route("/api/admin/ads/list", methods=["POST"])
def admin_ads_list():
    if not _check(request.get_json() or {}):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, title, image_url, link, placement, is_active, created_at "
                        "FROM ads ORDER BY id DESC")
            rows = cur.fetchall()
        items = [{"id": r[0], "title": r[1], "image_url": r[2] or "", "link": r[3] or "",
                  "placement": r[4] or "all", "is_active": bool(r[5])} for r in rows]
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/ads/save", methods=["POST"])
def admin_ads_save():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify({"error": "matn kiriting"}), 400
    image_url = (d.get("image_url") or "").strip()
    link = (d.get("link") or "").strip()
    placement = (d.get("placement") or "all").strip()
    if placement not in ("site", "bot", "all"):
        placement = "all"
    is_active = bool(d.get("is_active", True))
    aid = d.get("id")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if aid:
                cur.execute("UPDATE ads SET title=%s, image_url=%s, link=%s, placement=%s, is_active=%s WHERE id=%s",
                            (title[:500], image_url, link, placement, is_active, int(aid)))
            else:
                cur.execute("INSERT INTO ads (title, image_url, link, placement, is_active) "
                            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                            (title[:500], image_url, link, placement, is_active))
                aid = cur.fetchone()[0]
            conn.commit()
        return jsonify({"ok": True, "id": aid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/ads/delete", methods=["POST"])
def admin_ads_delete():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM ads WHERE id=%s", (int(d.get("id")),))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ══════════════════ TELEKANALLAR (TV) ══════════════════
import re as _re

_handle_to_channel_cache = {}

def _resolve_handle_to_channel_id(handle):
    """@handle (masalan @Yoshlartelekanali) ni UC... channel ID'ga aylantiradi.
    Natija xotirada keshlanadi (server qayta ishga tushmaguncha)."""
    handle = handle.lstrip("@")
    if handle in _handle_to_channel_cache:
        return _handle_to_channel_cache[handle]
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://www.youtube.com/@{handle}",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m = _re.search(r'"channelId":"(UC[\w-]{22})"', html)
        cid = m.group(1) if m else ""
        _handle_to_channel_cache[handle] = cid
        return cid
    except Exception as e:
        log.warning("handle resolve failed for @%s: %s", handle, e)
        return ""

def _extract_youtube_embed(url_or_id):
    """YouTube havola/handle/ID dan iframe uchun embed manzilini yasaydi.
    Qo'llab-quvvatlaydi: youtube.com/watch?v=ID, youtu.be/ID, /live/ID,
    @handle/live, /channel/UC.../live, yoki to'g'ridan-to'g'ri video ID."""
    s = (url_or_id or "").strip()
    if not s:
        return ""
    # To'g'ridan-to'g'ri 11-belgili video ID (havola emas)
    if _re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return f"https://www.youtube.com/embed/{s}?autoplay=1"
    m = _re.search(r"(?:v=|youtu\.be/|/live/|/embed/)([A-Za-z0-9_-]{11})", s)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?autoplay=1"
    # /channel/UC... — to'g'ridan-to'g'ri channel ID, YouTube live_stream buni qo'llab-quvvatlaydi
    m = _re.search(r"youtube\.com/channel/([\w-]+)", s)
    if m:
        return f"https://www.youtube.com/embed/live_stream?channel={m.group(1)}&autoplay=1"
    # To'g'ridan-to'g'ri UC... channel ID kiritilgan bo'lsa
    if _re.fullmatch(r"UC[\w-]{22}", s):
        return f"https://www.youtube.com/embed/live_stream?channel={s}&autoplay=1"
    # @handle (masalan youtube.com/@Yoshlartelekanali/live yoki shunchaki @Yoshlartelekanali)
    m = _re.search(r"(?:youtube\.com/)?(@[\w.-]+)", s)
    if m:
        cid = _resolve_handle_to_channel_id(m.group(1))
        if cid:
            return f"https://www.youtube.com/embed/live_stream?channel={cid}&autoplay=1"
    return ""

@app.route("/api/channels")
def api_channels():
    """Sayt /tv sahifasi uchun faol telekanallar ro'yxati."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, logo_url, stream_url, category, description, "
                        "COALESCE(source_type,'hls') "
                        "FROM tv_channels WHERE is_active=TRUE "
                        "ORDER BY sort_order ASC, id ASC")
            rows = cur.fetchall()
        items = []
        for r in rows:
            source_type = r[6] or "hls"
            item = {"id": r[0], "name": r[1], "logo_url": r[2] or "", "stream_url": r[3] or "",
                    "category": r[4] or "Umumiy", "description": r[5] or "", "source_type": source_type}
            if source_type == "youtube":
                item["embed_url"] = _extract_youtube_embed(r[3] or "")
            items.append(item)
        return jsonify({"channels": items})
    except Exception as e:
        log.warning("api_channels: %s", e)
        return jsonify({"channels": []})

@app.route("/api/tv/heartbeat", methods=["POST"])
def tv_heartbeat():
    """Foydalanuvchi qaysi kanalni tomosha qilayotganini bildiradi (tirik hisoblagich uchun)."""
    try:
        d = request.get_json(force=True, silent=True) or {}
        channel_id = int(d.get("channel_id") or 0)
        session_id = str(d.get("session_id") or "")[:80]
        if not channel_id or not session_id:
            return jsonify({"ok": False}), 400
        with get_conn() as conn:
            cur = conn.cursor()
            # bitta sessiya bir vaqtda faqat bitta kanalda "tomoshada" hisoblansin —
            # boshqa kanaldagi eski yozuvini darhol o'chiramiz (40s kutmasdan)
            cur.execute("DELETE FROM tv_viewers WHERE session_id=%s AND channel_id != %s",
                        (session_id, channel_id))
            cur.execute(
                "INSERT INTO tv_viewers (channel_id, session_id, last_seen) VALUES (%s,%s,NOW()) "
                "ON CONFLICT (channel_id, session_id) DO UPDATE SET last_seen=NOW()",
                (channel_id, session_id))
            # eskirgan (40s dan ortiq faolsiz) yozuvlarni safarbar tozalab boramiz
            cur.execute("DELETE FROM tv_viewers WHERE last_seen < NOW() - INTERVAL '40 seconds'")
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("tv_heartbeat: %s", e)
        return jsonify({"ok": False}), 200

@app.route("/api/tv/leave", methods=["POST"])
def tv_leave():
    """Foydalanuvchi kanalni tark etganda (boshqasiga o'tganda yoki sahifani yopganda) darhol hisobdan chiqaramiz."""
    try:
        d = request.get_json(force=True, silent=True) or {}
        session_id = str(d.get("session_id") or "")[:80]
        if not session_id:
            return jsonify({"ok": False}), 400
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM tv_viewers WHERE session_id=%s", (session_id,))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.warning("tv_leave: %s", e)
        return jsonify({"ok": False}), 200

@app.route("/api/tv/viewers")
def tv_viewers():
    """Har bir kanalni hozir necha kishi tomosha qilayotgani (so'nggi 40 soniyada faol)."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT channel_id, COUNT(*) FROM tv_viewers "
                "WHERE last_seen > NOW() - INTERVAL '40 seconds' GROUP BY channel_id")
            counts = {str(r[0]): r[1] for r in cur.fetchall()}
        return jsonify({"viewers": counts})
    except Exception as e:
        log.warning("tv_viewers: %s", e)
        return jsonify({"viewers": {}})

@app.route("/api/admin/channels/list", methods=["POST"])
def admin_channels_list():
    if not _check(request.get_json() or {}):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, logo_url, stream_url, category, description, "
                        "sort_order, is_active, COALESCE(source_type,'hls') "
                        "FROM tv_channels ORDER BY sort_order ASC, id ASC")
            rows = cur.fetchall()
        items = [{"id": r[0], "name": r[1], "logo_url": r[2] or "", "stream_url": r[3] or "",
                  "category": r[4] or "Umumiy", "description": r[5] or "",
                  "sort_order": r[6] or 0, "is_active": bool(r[7]), "source_type": r[8] or "hls"} for r in rows]
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/channels/save", methods=["POST"])
def admin_channels_save():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "kanal nomini kiriting"}), 400
    logo_url = (d.get("logo_url") or "").strip()
    stream_url = (d.get("stream_url") or "").strip()
    source_type = (d.get("source_type") or "hls").strip()
    if source_type not in ("hls", "youtube"):
        source_type = "hls"
    category = (d.get("category") or "Umumiy").strip() or "Umumiy"
    description = (d.get("description") or "").strip()
    try:
        sort_order = int(d.get("sort_order") or 0)
    except Exception:
        sort_order = 0
    is_active = bool(d.get("is_active", True))
    cid = d.get("id")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if cid:
                cur.execute("UPDATE tv_channels SET name=%s, logo_url=%s, stream_url=%s, category=%s, "
                            "description=%s, sort_order=%s, is_active=%s, source_type=%s WHERE id=%s",
                            (name[:200], logo_url, stream_url, category[:60], description[:500],
                             sort_order, is_active, source_type, int(cid)))
            else:
                cur.execute("INSERT INTO tv_channels (name, logo_url, stream_url, category, description, "
                            "sort_order, is_active, source_type) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (name[:200], logo_url, stream_url, category[:60], description[:500],
                             sort_order, is_active, source_type))
                cid = cur.fetchone()[0]
            conn.commit()
        return jsonify({"ok": True, "id": cid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/channels/delete", methods=["POST"])
def admin_channels_delete():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM tv_channels WHERE id=%s", (int(d.get("id")),))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/channels/bulk_delete", methods=["POST"])
def admin_channels_bulk_delete():
    """Bir nechta kanalni birdaniga o'chiradi.
    Body: {password, ids?: [1,2,3], all?: true, category?: "Music"}
    - ids berilsa: faqat shu ID'lar o'chadi
    - all=true berilsa: (category bilan yoki bo'lmasa) barcha kanallar o'chadi"""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    ids = d.get("ids") or []
    delete_all = bool(d.get("all"))
    category = (d.get("category") or "").strip()
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if delete_all:
                if category:
                    cur.execute("DELETE FROM tv_channels WHERE category=%s", (category,))
                else:
                    cur.execute("DELETE FROM tv_channels")
            elif ids:
                clean_ids = [int(i) for i in ids]
                cur.execute("DELETE FROM tv_channels WHERE id = ANY(%s)", (clean_ids,))
            else:
                return jsonify({"error": "ids yoki all kiritilmagan"}), 400
            deleted = cur.rowcount
            conn.commit()
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _clean_channel_name(name):
    """'Milliy (1080p)' -> 'Milliy'. Oxiridagi (NNNp)/(NNNi)/[Not 24/7] kabi belgilarni olib tashlaydi."""
    name = _re.sub(r'\s*\[[^\]]*\]\s*', ' ', name)  # [Not 24/7], [Geo-blocked] va h.k.
    name = _re.sub(r'\s*\((?:\d{3,4}[ip]|HD|FHD|UHD|4K|SD)\)\s*', ' ', name, flags=_re.IGNORECASE)
    name = _re.sub(r'\s+', ' ', name).strip()
    return name

def _looks_like_garbage_name(name):
    """User-Agent qatoridan chiqib qolgan chalkash nomlarni aniqlaydi."""
    low = name.lower()
    if 'mozilla' in low or 'chrome/' in low or 'safari/' in low or 'applewebkit' in low:
        return True
    if len(name) > 120:
        return True
    return False

def _extract_extinf_name(line):
    """#EXTINF qatoridan kanal nomini ajratib oladi.
    Format: #EXTINF:-1 attr="val" attr2="val2",Kanal Nomi
    Nom har doim OXIRGI qo'shtirnoqdan keyingi qismda keladi (attributlar ichidagi
    vergullar — masalan tvg-name="A, B" — ichida bo'lishi mumkin bo'lgani uchun,
    oddiy 'birinchi/oxirgi vergul' qidiruvi noto'g'ri natija berishi mumkin)."""
    last_quote = line.rfind('"')
    if last_quote != -1:
        tail = line[last_quote + 1:]
        comma = tail.find(',')
        if comma != -1:
            name = tail[comma + 1:].strip()
            if name:
                return name
    else:
        # Qo'shtirnoq umuman yo'q — oddiy #EXTINF:-1,Kanal Nomi format
        comma = line.find(',')
        if comma != -1:
            name = line[comma + 1:].strip()
            if name:
                return name
    # Nom topilmadi (bo'sh yoki format boshqacha) — tvg-name atributiga tayanamiz
    m_tvgname = _re.search(r'tvg-name="([^"]*)"', line)
    if m_tvgname and m_tvgname.group(1).strip():
        return m_tvgname.group(1).strip()
    return ""

def _parse_m3u(text):
    """M3U/M3U8 matnini [{name, logo, category, url}, ...] ro'yxatiga aylantiradi.
    Faqat #EXTINF qatoridan bevosita keyin keladigan http(s) URL qatorlarini kanal deb oladi —
    #EXTVLCOPT, #EXTGRP va boshqa meta-teglar chalkashtirmasligi uchun."""
    lines = text.splitlines()
    out = []
    pending = None  # oxirgi ko'rilgan #EXTINF ma'lumoti, hali URL kutilmoqda
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m_logo = _re.search(r'tvg-logo="([^"]*)"', line)
            m_cat = _re.search(r'group-title="([^"]*)"', line)
            raw_name = _extract_extinf_name(line)
            clean_name = _clean_channel_name(raw_name) if raw_name else ""
            pending = {
                "name": clean_name if clean_name and not _looks_like_garbage_name(clean_name) else "Nomsiz kanal",
                "logo": m_logo.group(1) if m_logo else "",
                "category": m_cat.group(1) if m_cat else "Umumiy",
            }
        elif line.startswith("#"):
            continue  # boshqa meta-teglar (#EXTVLCOPT, #EXTGRP va h.k.) — e'tiborsiz qoldiriladi
        else:
            if line.startswith("http://") or line.startswith("https://"):
                if pending:
                    out.append({"name": pending["name"], "logo": pending["logo"],
                                "category": pending["category"] or "Umumiy", "url": line})
                    pending = None
                # EXTINF'siz to'g'ridan-to'g'ri URL kelsa — e'tiborsiz qoldiramiz (nomsiz kanal keraksiz)
            # http(s) bilan boshlanmagan boshqa qatorlar (masalan yana bir meta ma'lumot) — o'tkazib yuboriladi
    return out

@app.route("/api/admin/channels/cleanup", methods=["POST"])
def admin_channels_cleanup():
    """Mavjud kanallarning nomidagi (1080p)/(576p) kabi qo'shimchalarni olib tashlaydi
    va logo bo'sh bo'lsa, iptv-org logos ro'yxatidan nom bo'yicha moslashtirib to'ldiradi."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403

    fixed_names, fixed_logos = 0, 0
    try:
        logos_map = {}
        try:
            import urllib.request, json as _json
            req = urllib.request.Request("https://iptv-org.github.io/api/channels.json",
                                          headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                channels_data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
            req2 = urllib.request.Request("https://iptv-org.github.io/api/logos.json",
                                           headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=15) as resp:
                logos_data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
            logo_by_id = {}
            for l in logos_data:
                if l.get("channel") and l.get("url") and l["channel"] not in logo_by_id:
                    logo_by_id[l["channel"]] = l["url"]
            for c in channels_data:
                nm = (c.get("name") or "").strip().lower()
                cid = c.get("id")
                if nm and cid in logo_by_id:
                    logos_map[nm] = logo_by_id[cid]
        except Exception as e:
            log.warning("cleanup: logos fetch failed: %s", e)

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, logo_url FROM tv_channels")
            rows = cur.fetchall()
            for cid, name, logo_url in rows:
                new_name = _clean_channel_name(name or "")
                new_logo = logo_url
                if not new_logo:
                    new_logo = logos_map.get(new_name.strip().lower(), "")
                    if new_logo:
                        fixed_logos += 1
                if new_name != name or new_logo != logo_url:
                    if new_name != name:
                        fixed_names += 1
                    cur.execute("UPDATE tv_channels SET name=%s, logo_url=%s WHERE id=%s",
                                (new_name[:200], new_logo[:500], cid))
            conn.commit()
        return jsonify({"ok": True, "fixed_names": fixed_names, "fixed_logos": fixed_logos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/channels/import_m3u", methods=["POST"])
def admin_channels_import_m3u():
    """M3U havoladan yoki matndan ko'plab kanallarni birdaniga import qiladi.
    Body: {password, m3u_url?, m3u_text?, category_filter?, source_type?, skip_existing?}"""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403

    m3u_url = (d.get("m3u_url") or "").strip()
    m3u_text = d.get("m3u_text") or ""
    category_filter = (d.get("category_filter") or "").strip().lower()
    source_type = (d.get("source_type") or "hls").strip()
    if source_type not in ("hls", "youtube"):
        source_type = "hls"
    skip_existing = bool(d.get("skip_existing", True))

    if not m3u_text and m3u_url:
        try:
            import urllib.request
            req = urllib.request.Request(m3u_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                m3u_text = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            return jsonify({"error": f"M3U yuklab bo'lmadi: {type(e).__name__}: {e}"}), 400

    if not m3u_text.strip():
        return jsonify({"error": "m3u_url yoki m3u_text kiriting"}), 400

    try:
        parsed = _parse_m3u(m3u_text)
    except Exception as e:
        return jsonify({"error": f"M3U parse xatoligi: {e}"}), 400

    if category_filter:
        parsed = [p for p in parsed if category_filter in (p["category"] or "").lower()]

    if not parsed:
        return jsonify({"error": "Kanal topilmadi (filtr juda tor bo'lishi mumkin)"}), 400

    added, skipped, failed = 0, 0, 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, stream_url FROM tv_channels")
            existing = {(r[0].strip().lower(), r[1].strip()) for r in cur.fetchall()}
            # keyingi sort_order eng kattasidan boshlanadi
            cur.execute("SELECT COALESCE(MAX(sort_order),0) FROM tv_channels")
            next_order = (cur.fetchone()[0] or 0) + 1

            for ch in parsed:
                key = (ch["name"].strip().lower(), ch["url"].strip())
                if skip_existing and key in existing:
                    skipped += 1
                    continue
                try:
                    cur.execute(
                        "INSERT INTO tv_channels (name, logo_url, stream_url, category, description, "
                        "sort_order, is_active, source_type) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ch["name"][:200], ch["logo"][:500], ch["url"][:1000],
                         (ch["category"] or "Umumiy")[:60], "", next_order, True, source_type))
                    existing.add(key)
                    next_order += 1
                    added += 1
                except Exception as e:
                    log.warning("import_m3u insert failed for %s: %s", ch.get("name"), e)
                    failed += 1
            conn.commit()
        return jsonify({"ok": True, "added": added, "skipped": skipped,
                        "failed": failed, "total_parsed": len(parsed)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/tv")
def tv_page():
    """Telekanallar sahifasi — HLS pleyer bilan jonli efir."""
    html = """<!DOCTYPE html>
<html lang="uz"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telekanallar — jonli efir | ASTRA</title>
<meta name="description" content="ASTRA telekanallar — jonli efir, yangiliklar, sport va boshqa kanallar onlayn, bepul.">
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg">
<link rel="shortcut icon" href="/favicon.ico">
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"></script>
<style>
  *{box-sizing:border-box;}
  .tv-wrap{max-width:1440px;margin:0 auto;padding:16px 16px 60px;}
  .tv-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;}
  .tv-top .logo img{height:30px;display:block;}
  .tv-top a.home{color:#8c87b8;text-decoration:none;font-size:14px;}
  .tv-wrap h1{font-size:26px;margin:6px 0 4px;color:#fff;}
  .tv-sub{color:#b8b4d8;font-size:14px;margin:0 0 18px;}

  .sidebar-toggle-btn{display:flex;align-items:center;gap:8px;background:#1b1840;border:1px solid #252154;
    color:#fff;font-size:13px;font-weight:600;padding:9px 14px;border-radius:10px;cursor:pointer;
    white-space:nowrap;margin-bottom:14px;transition:border-color .15s,background .15s;}
  .sidebar-toggle-btn:hover{border-color:#7c5cff;background:#211d4d;}
  .sidebar-toggle-btn .bars{width:16px;height:12px;position:relative;flex:0 0 auto;}
  .sidebar-toggle-btn .bars span{display:block;height:2px;background:#b8b4d8;border-radius:2px;position:absolute;left:0;right:0;}
  .sidebar-toggle-btn .bars span:nth-child(1){top:0;}
  .sidebar-toggle-btn .bars span:nth-child(2){top:5px;}
  .sidebar-toggle-btn .bars span:nth-child(3){top:10px;}

  /* Layout: chap tomonda kanallar, o'ngda pleyer */
  .tv-layout{display:flex;gap:20px;align-items:flex-start;}
  .tv-sidebar{flex:0 0 300px;width:300px;overflow:hidden;transition:width .22s ease,flex-basis .22s ease,
    opacity .18s ease,margin .22s ease;opacity:1;}
  .tv-sidebar.collapsed{flex:0 0 0;width:0;opacity:0;pointer-events:none;}
  .tv-main{flex:1 1 0%;min-width:0;position:sticky;top:16px;align-self:flex-start;}

  .player-box{background:#000;border-radius:14px;overflow:hidden;position:relative;aspect-ratio:16/9;
    box-shadow:0 10px 40px rgba(0,0,0,.5);margin-bottom:10px;}
  .player-box video{width:100%;height:100%;display:block;background:#000;}
  .player-box iframe{width:100%;height:100%;display:block;border:0;}
  .player-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
    justify-content:center;color:#8c87b8;gap:10px;text-align:center;padding:20px;}
  .now-playing{color:#fff;font-size:16px;font-weight:600;margin:0 0 20px;min-height:22px;}
  .now-playing span{color:#7c5cff;}
  .viewer-count{position:absolute;top:12px;right:12px;display:flex;align-items:center;gap:5px;
    background:rgba(0,0,0,.55);backdrop-filter:blur(4px);color:#fff;font-size:12px;font-weight:600;
    padding:5px 10px 5px 8px;border-radius:999px;z-index:2;display:none;}
  .viewer-count .dot{width:6px;height:6px;border-radius:50%;background:#4ade80;flex:0 0 auto;
    animation:pulse-dot 1.6s ease-in-out infinite;}
  @keyframes pulse-dot{0%,100%{opacity:1;}50%{opacity:.35;}}
  .ch-row .ch-viewers{color:#7c86b8;font-size:11px;margin-top:2px;display:flex;align-items:center;gap:4px;}
  .ch-row .ch-viewers .dot{width:5px;height:5px;border-radius:50%;background:#4ade80;flex:0 0 auto;}

  .cat-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;}
  .tv-search{width:100%;box-sizing:border-box;padding:10px 14px;margin-bottom:12px;border-radius:10px;
    background:#1b1840;border:1px solid #252154;color:#fff;font-size:14px;outline:none;}
  .tv-search:focus{border-color:#7c5cff;}
  .tv-search::placeholder{color:#8c87b8;}
  .cat{padding:6px 13px;border-radius:999px;background:#1b1840;border:1px solid #252154;
    color:#b8b4d8;font-size:12px;cursor:pointer;user-select:none;white-space:nowrap;}
  .cat.active{background:#7c5cff;border-color:#7c5cff;color:#fff;font-weight:600;}

  .ch-sidebar-title{color:#8c87b8;font-size:12px;font-weight:700;text-transform:uppercase;
    letter-spacing:.5px;margin:2px 0 10px;}
  .ch-list{display:flex;flex-direction:column;gap:6px;max-height:calc(100vh - 260px);min-height:200px;
    overflow-y:auto;padding-right:4px;}
  .ch-list::-webkit-scrollbar{width:6px;}
  .ch-list::-webkit-scrollbar-thumb{background:#252154;border-radius:6px;}
  .ch-row{display:flex;align-items:center;gap:10px;background:#1b1840;border:1px solid #252154;
    border-radius:10px;padding:8px;cursor:pointer;transition:border-color .15s,background .15s;}
  .ch-row:hover{border-color:#7c5cff;}
  .ch-row.active{border-color:#7c5cff;background:rgba(124,92,255,.14);box-shadow:0 0 0 1px rgba(124,92,255,.4) inset;}
  .ch-row .ch-logo{width:44px;height:44px;flex:0 0 auto;object-fit:contain;border-radius:8px;background:#0d0b22;}
  .ch-row .ch-logo.ph{display:flex;align-items:center;justify-content:center;font-size:19px;color:#7c5cff;}
  .ch-row .ch-info{min-width:0;flex:1 1 auto;}
  .ch-row .ch-name{color:#fff;font-size:13px;font-weight:500;line-height:1.3;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis;}
  .ch-row .ch-cat{color:#8c87b8;font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .ch-status{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;flex:0 0 auto;vertical-align:middle;}
  .ch-status-on{background:#4ade80;box-shadow:0 0 4px rgba(74,222,128,.7);}
  .ch-status-off{background:#6b6688;}

  .tv-empty{color:#b8b4d8;text-align:center;padding:50px 20px;}
  .live-badge{position:absolute;top:12px;left:12px;background:#e5484d;color:#fff;font-size:11px;
    font-weight:700;padding:3px 9px;border-radius:6px;letter-spacing:.5px;z-index:2;display:none;}

  .player-box:fullscreen{aspect-ratio:auto;border-radius:0;}
  .player-box:-webkit-full-screen{aspect-ratio:auto;border-radius:0;}
  .player-box:fullscreen video, .player-box:fullscreen iframe{width:100%;height:100%;}
  .player-box:-webkit-full-screen video, .player-box:-webkit-full-screen iframe{width:100%;height:100%;}

  /* Mobil: ustunlar tepa-past bo'lib joylashadi, kanallar ro'yxati pleyer ostida */
  @media (max-width: 860px){
    .tv-layout{flex-direction:column;}
    .tv-main{position:static;}
    .tv-sidebar{width:100%;flex:1 1 auto;order:2;transition:max-height .22s ease,opacity .18s ease,margin .22s ease;
      max-height:1200px;}
    .tv-sidebar.collapsed{max-height:0;width:100%;flex:1 1 auto;margin:0;}
    .tv-main{order:1;}
    .ch-list{max-height:60vh;}
  }
</style>
</head><body>
<div class="tv-wrap">
  <div class="tv-top">
    <a class="logo" href="/"><img src="/static/logo.svg" alt="ASTRA"></a>
    <a class="home" href="/">Bosh sahifa ↗</a>
  </div>
  <h1>📺 Telekanallar</h1>
  <p class="tv-sub">Jonli efir — yangiliklar, sport va boshqa kanallar onlayn.</p>

  <button class="sidebar-toggle-btn" id="sidebarToggleBtn" onclick="toggleSidebar()">
    <span class="bars"><span></span><span></span><span></span></span>
    <span id="sidebarToggleLabel">Kanallarni yashirish</span>
  </button>

  <div class="tv-layout">
    <aside class="tv-sidebar" id="tvSidebar">
      <div class="ch-sidebar-title" style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;" onclick="toggleCatBox()">
        <span>Kanallar</span>
        <span id="catToggleBtn" style="font-size:11px;color:#8c87b8;font-weight:600;text-transform:none;letter-spacing:0;display:flex;align-items:center;gap:4px;">
          Kategoriyalar <span id="catToggleIcon">▾</span>
        </span>
      </div>
      <div class="cat-row" id="catRow" style="display:none;"></div>
      <input id="chSearchInput" class="tv-search" type="text" placeholder="🔍 Kanal qidirish..." oninput="onSearchInput()">
      <div class="ch-list" id="chList"></div>
      <div class="tv-empty" id="tvEmpty" style="display:none;">
        Hozircha telekanallar qo'shilmagan. Tez orada! 📡
      </div>
    </aside>

    <div class="tv-main">
      <div class="player-box">
        <span class="live-badge" id="liveBadge">● JONLI</span>
        <span class="viewer-count" id="viewerCount"><span class="dot"></span><span id="viewerCountNum">0</span></span>
        <video id="tvVideo" controls playsinline></video>
        <iframe id="tvFrame" style="display:none;" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>
        <div class="player-empty" id="playerEmpty">
          <div style="font-size:42px;">📺</div>
          <div>Ko'rish uchun chapdan kanal tanlang</div>
        </div>
      </div>
      <p class="now-playing" id="nowPlaying"></p>
      __TV_AD_SLOT__
    </div>
  </div>
</div>

<script>
let CHANNELS = [], curCat = "Hammasi", hls = null, activeId = null, VIEWERS = {};
const video = document.getElementById('tvVideo');
const empty = document.getElementById('playerEmpty');
const liveBadge = document.getElementById('liveBadge');
const nowPlaying = document.getElementById('nowPlaying');
const viewerCountBox = document.getElementById('viewerCount');
const viewerCountNum = document.getElementById('viewerCountNum');

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

/* Sayt https orqali ochiladi — logotip manzili http:// bo'lsa, ko'p mobil brauzer uni
   "mixed content" deb bloklab, rasmni ko'rsatmaydi. Iloji boricha https'ga majburlaymiz. */
function normalizeLogoUrl(url){
  if (!url) return '';
  url = url.trim();
  if (!url) return '';
  if (url.startsWith('//')) return 'https:' + url;
  if (url.startsWith('http://')) return 'https://' + url.slice(7);
  return url;
}

/* Baza kategoriyalari ba'zan "Culture;Music;Religious" kabi qo'shilgan holatda keladi —
   ko'rsatish uchun birinchi ma'noli bo'lakni olamiz va tanish nomlarga moslashtiramiz. */
const CAT_LABELS = {
  'undefined':'Umumiy', '':'Umumiy', 'general':'Umumiy', 'public':'Umumiy',
  'news':'Yangiliklar', 'sport':'Sport', 'sports':'Sport', 'kids':'Bolalar',
  'family':'Oilaviy', 'music':'Musiqa', 'movies':'Kino', 'entertainment':'Ko\\'ngilochar',
  'documentary':'Hujjatli', 'culture':'Madaniyat', 'classic':'Klassik',
  'religious':'Diniy', 'lifestyle':'Turmush tarzi', 'animation':'Multfilm'
};
function primaryCategory(raw){
  const first = (raw||'').split(';')[0].trim().toLowerCase();
  if (CAT_LABELS[first]) return CAT_LABELS[first];
  return first ? first.charAt(0).toUpperCase()+first.slice(1) : 'Umumiy';
}

const frame = document.getElementById('tvFrame');

function scrollPlayerIntoViewIfNeeded(){
  if (window.innerWidth > 860) return; // desktopda pleyer sticky, joyidan qo'zg'almaydi
  const box = document.querySelector('.player-box');
  const rect = box.getBoundingClientRect();
  if (rect.top < 0 || rect.bottom > window.innerHeight){
    box.scrollIntoView({behavior:'smooth', block:'start'});
  }
}

function fmtViewers(n){
  if (n >= 1000) return (n/1000).toFixed(1).replace('.0','') + 'ming';
  return String(n);
}

function updateViewerBadges(){
  const cur = VIEWERS[String(activeId)] || 0;
  if (activeId){
    viewerCountBox.style.display = 'flex';
    viewerCountNum.textContent = fmtViewers(cur);
  } else {
    viewerCountBox.style.display = 'none';
  }
  document.querySelectorAll('.ch-row').forEach(row=>{
    const n = VIEWERS[row.dataset.id] || 0;
    const el = row.querySelector('.ch-viewers-num');
    if (el) el.textContent = fmtViewers(n) + ' tomoshada';
  });
}

function play(ch){
  activeId = ch.id;
  document.querySelectorAll('.ch-row').forEach(e=>e.classList.toggle('active', +e.dataset.id===ch.id));
  nowPlaying.innerHTML = '▶ <span>' + esc(ch.name) + '</span>';
  empty.style.display = 'none';
  liveBadge.style.display = 'block';
  updateViewerBadges();
  if (hls){ hls.destroy(); hls = null; }
  video.pause(); video.removeAttribute('src'); video.load();
  frame.src = ''; frame.style.display = 'none'; video.style.display = 'block';

  if (ch.source_type === 'youtube'){
    const embed = ch.embed_url;
    if (!embed){ nowPlaying.innerHTML = '⚠️ Bu kanalning YouTube havolasi noto\\'g\\'ri yoki qo\\'shilmagan.'; liveBadge.style.display='none'; return; }
    video.style.display = 'none';
    frame.style.display = 'block';
    frame.src = embed;
    scrollPlayerIntoViewIfNeeded();
    sendHeartbeat();
    return;
  }

  const url = ch.stream_url;
  if (!url){ nowPlaying.innerHTML = '⚠️ Bu kanalning oqim havolasi hali qo\\'shilmagan.'; liveBadge.style.display='none'; return; }
  if (video.canPlayType('application/vnd.apple.mpegurl')){
    video.src = url; video.play().catch(()=>{});
  } else if (window.Hls && Hls.isSupported()){
    hls = new Hls({lowLatencyMode:true});
    hls.loadSource(url); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, ()=>video.play().catch(()=>{}));
    hls.on(Hls.Events.ERROR, (e,data)=>{ if(data.fatal){ nowPlaying.innerHTML='⚠️ Oqimni ochib bo\\'lmadi (havola ishlamayapti yoki bloklangan).'; liveBadge.style.display='none'; } });
  } else {
    video.src = url; video.play().catch(()=>{});
  }
  scrollPlayerIntoViewIfNeeded();
  sendHeartbeat();
}

function render(){
  const cats = ["Hammasi", ...Array.from(new Set(CHANNELS.map(c=>primaryCategory(c.category)))).sort((a,b)=>a.localeCompare(b,'uz'))];
  document.getElementById('catRow').innerHTML = cats.map(c=>
    `<div class="cat ${c===curCat?'active':''}" onclick="setCat('${esc(c)}')">${esc(c)}</div>`).join('');
  let list = curCat==="Hammasi" ? CHANNELS : CHANNELS.filter(c=>primaryCategory(c.category)===curCat);
  const q = (document.getElementById('chSearchInput').value || '').trim().toLowerCase();
  if (q) list = list.filter(c => (c.name||'').toLowerCase().includes(q));
  const listEl = document.getElementById('chList');
  if (!list.length){
    listEl.innerHTML = '<div style="color:#8c87b8;text-align:center;padding:30px 10px;">Mos kanal topilmadi.</div>';
    return;
  }
  listEl.innerHTML = list.map(c=>{
    const logoUrl = normalizeLogoUrl(c.logo_url);
    const logo = logoUrl
      ? `<img class="ch-logo" src="${esc(logoUrl)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.onerror=null;this.outerHTML='<div class=\\'ch-logo ph\\'>📺</div>'">`
      : `<div class="ch-logo ph">📺</div>`;
    const n = VIEWERS[String(c.id)] || 0;
    const hasStream = !!(c.stream_url && c.stream_url.trim());
    const statusDot = hasStream
      ? '<span class="ch-status ch-status-on" title="Onlayn"></span>'
      : '<span class="ch-status ch-status-off" title="Oflayn — havola qo\\'shilmagan"></span>';
    return `<div class="ch-row ${c.id===activeId?'active':''}" data-id="${c.id}" onclick='play(${JSON.stringify(c).replace(/'/g,"&#39;")})'>
      ${logo}<div class="ch-info"><div class="ch-name">${statusDot}${esc(c.name)}${c.source_type==='youtube'?' <span style="color:#ff4444;font-size:10px;">▶</span>':''}</div>
      <div class="ch-cat">${esc(primaryCategory(c.category))}</div>
      <div class="ch-viewers"><span class="dot"></span><span class="ch-viewers-num">${fmtViewers(n)} tomoshada</span></div></div></div>`;
  }).join('');
}
function setCat(c){ curCat=c; render(); }
function toggleCatBox(){
  const box = document.getElementById('catRow');
  const icon = document.getElementById('catToggleIcon');
  const open = box.style.display !== 'none';
  box.style.display = open ? 'none' : 'flex';
  icon.textContent = open ? '▾' : '▴';
}
function onSearchInput(){ render(); }

/* ── Tomoshabinlar soni: haqiqiy heartbeat asosida ── */
function getSessionId(){
  try{
    let id = localStorage.getItem('tvSessionId');
    if (!id){ id = (crypto.randomUUID ? crypto.randomUUID() : ('s'+Date.now()+Math.random())); localStorage.setItem('tvSessionId', id); }
    return id;
  }catch(e){ return 's'+Date.now()+Math.random(); }
}
const SESSION_ID = getSessionId();
function sendHeartbeat(){
  if (!activeId) return;
  fetch('/api/tv/heartbeat', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({channel_id: activeId, session_id: SESSION_ID})}).catch(()=>{});
}
function loadViewerCounts(){
  fetch('/api/tv/viewers').then(r=>r.json()).then(d=>{ VIEWERS = d.viewers || {}; updateViewerBadges(); }).catch(()=>{});
}
function sendLeave(){
  const payload = JSON.stringify({session_id: SESSION_ID});
  if (navigator.sendBeacon){
    navigator.sendBeacon('/api/tv/leave', new Blob([payload], {type:'application/json'}));
  } else {
    fetch('/api/tv/leave', {method:'POST', headers:{'Content-Type':'application/json'}, body: payload, keepalive:true}).catch(()=>{});
  }
}
window.addEventListener('pagehide', sendLeave);
document.addEventListener('visibilitychange', ()=>{ if (document.visibilityState === 'hidden') sendLeave(); });
setInterval(sendHeartbeat, 20000);
setInterval(loadViewerCounts, 15000);
loadViewerCounts();

/* ── Sidebar (kanallar ro'yxati) ko'rsatish / yashirish — pleyer joyida qoladi ── */
const sidebar = document.getElementById('tvSidebar');
const toggleLabel = document.getElementById('sidebarToggleLabel');
const SIDEBAR_KEY = 'tvSidebarOpen';

function applySidebarState(open){
  sidebar.classList.toggle('collapsed', !open);
  toggleLabel.textContent = open ? 'Kanallarni yashirish' : 'Kanallarni ko\\'rsatish';
}
function toggleSidebar(){
  const open = sidebar.classList.contains('collapsed');
  applySidebarState(open);
  try{ localStorage.setItem(SIDEBAR_KEY, open ? '1' : '0'); }catch(e){}
}
(function initSidebar(){
  let open = true;
  try{
    const saved = localStorage.getItem(SIDEBAR_KEY);
    if (saved !== null) open = saved === '1';
    else if (window.innerWidth <= 860) open = false; // mobilda avvaliga pleyer kattaroq bo'lsin
  }catch(e){ if (window.innerWidth <= 860) open = false; }
  applySidebarState(open);
})();

fetch('/api/channels').then(r=>r.json()).then(d=>{
  CHANNELS = d.channels || [];
  if (!CHANNELS.length){ document.getElementById('tvEmpty').style.display='block'; return; }
  render();
}).catch(()=>{ document.getElementById('tvEmpty').style.display='block'; });
</script>
<script src="/static/ad-interstitial.js"></script>
</body></html>"""
    html = html.replace("__TV_AD_SLOT__", _render_ad_banner(_get_site_ad()))
    return Response(html, mimetype="text/html")


def _get_site_ad():
    """Sayt sahifalari uchun bitta faol reklama (yoki None) — hech qachon xato bermaydi."""
    """Sayt sahifalari uchun bitta faol reklama (yoki None) — hech qachon xato bermaydi."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title, image_url, link FROM ads "
                        "WHERE is_active=TRUE AND placement IN ('site','all') "
                        "ORDER BY RANDOM() LIMIT 1")
            r = cur.fetchone()
        if r:
            return {"title": r[0], "image_url": r[1] or "", "link": r[2] or ""}
    except Exception as e:
        log.warning("get_site_ad: %s", e)
    return None

def _render_ad_banner(ad):
    """Reklama bannerini HTML qilib qaytaradi (adaptiv: rasm bor=karta, yo'q=toza matn)."""
    if not ad:
        return ""
    e = _html.escape
    title = e(ad["title"])
    link = ad.get("link") or ""
    box = ("display:block;text-decoration:none;background:rgba(124,92,255,0.10);"
           "border:1px solid rgba(124,92,255,0.4);border-radius:12px;padding:14px;margin:18px 0;position:relative;")
    label = ('<span style="position:absolute;top:8px;right:10px;font-size:10px;color:#8a82b8;'
             'text-transform:uppercase;letter-spacing:0.5px;">Reklama</span>')
    if ad.get("image_url"):
        # Rasm bor — yon-ma-yon karta (rasm chap, matn o'ng), kesilmaydi xunuk emas
        inner = ('<div style="display:flex;gap:13px;align-items:center;">'
                 f'<img src="{e(ad["image_url"])}" alt="" loading="lazy" '
                 'style="width:96px;height:68px;object-fit:cover;border-radius:8px;flex:0 0 auto;">'
                 '<div style="min-width:0;">'
                 f'<div style="font-size:15px;color:#fff;font-weight:500;line-height:1.45;">{title}</div>'
                 + ('<div style="font-size:13px;color:#9b93c4;margin-top:4px;">Batafsil →</div>' if link else '')
                 + '</div></div>')
    else:
        # Rasm yo'q — toza matn (kichik ikonka + matn)
        inner = ('<div style="display:flex;gap:12px;align-items:center;">'
                 '<div style="width:38px;height:38px;flex:0 0 auto;border-radius:9px;background:rgba(124,92,255,0.25);'
                 'display:flex;align-items:center;justify-content:center;font-size:19px;">📢</div>'
                 f'<div style="font-size:15px;color:#fff;font-weight:500;line-height:1.45;">{title}</div></div>')
    if link:
        return (f'<a href="{e(link)}" target="_blank" rel="noopener nofollow sponsored" '
                f'style="{box}">{label}{inner}</a>')
    return f'<div style="{box}">{label}{inner}</div>'

# ══════════════════ SEO (Google uchun) ══════════════════
import html as _html

@app.route("/robots.txt")
def robots():
    base = BASE_URL
    txt = f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"
    return Response(txt, mimetype="text/plain")

# ── Favicon (Yandex/brauzerlar sayt ildizidan /favicon.ico ni so'raydi) ──
@app.route("/favicon.ico")
def favicon_ico():
    resp = send_from_directory("static", "favicon.ico", mimetype="image/x-icon")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp

# ── PWA (telefonga o'rnatish uchun) ──
@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    resp = send_from_directory("static", "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.route("/sitemap.xml")
def sitemap():
    base = BASE_URL
    rows = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, created_at, title, description, trailer "
                        "FROM movies ORDER BY created_at DESC LIMIT 2000")
            rows = cur.fetchall()
    except Exception as e:
        log.warning("sitemap: %s", e)

    def esc(s):
        s = str(s or "")
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "&apos;"))

    parts = ["<url><loc>" + base + "/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>"]
    has_video = False
    for row in rows:
        mid, created = row[0], row[1]
        title, desc, trailer = row[2], row[3], (row[4] if len(row) > 4 else "")
        loc = f"{base}/kino/{mid}"
        lastmod = f"<lastmod>{created.strftime('%Y-%m-%d')}</lastmod>" if created else ""
        yid = _yt_id(trailer)
        video = ""
        if yid:  # FAQAT haqiqiy treyler bo'lsa — video belgisi (xatosiz)
            has_video = True
            ttl = esc(title)[:100] if title else f"Film #{mid}"
            dsc = esc(desc)[:1900] if desc else f"{ttl} treyler."
            video = ("<video:video>"
                     f"<video:thumbnail_loc>https://img.youtube.com/vi/{yid}/hqdefault.jpg</video:thumbnail_loc>"
                     f"<video:title>{ttl}</video:title>"
                     f"<video:description>{dsc}</video:description>"
                     f"<video:player_loc>https://www.youtube.com/embed/{yid}</video:player_loc>"
                     "</video:video>")
        parts.append(f"<url><loc>{loc}</loc>{lastmod}"
                     f"<changefreq>weekly</changefreq><priority>0.8</priority>{video}</url>")
    # Kategoriya / Top / Janr sahifalari (crawlable SEO sahifalari)
    for ct in ("movie", "series", "anime", "cartoon"):
        parts.append(f"<url><loc>{base}/kategoriya/{ct}</loc>"
                     "<changefreq>daily</changefreq><priority>0.7</priority></url>")
    parts.append(f"<url><loc>{base}/top</loc><changefreq>daily</changefreq><priority>0.7</priority></url>")
    parts.append(f"<url><loc>{base}/trend</loc><changefreq>daily</changefreq><priority>0.7</priority></url>")
    parts.append(f"<url><loc>{base}/tv</loc><changefreq>daily</changefreq><priority>0.7</priority></url>")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT country FROM movies WHERE country IS NOT NULL AND country <> ''")
            for (c,) in cur.fetchall()[:40]:
                for part in str(c).split(","):
                    name = part.strip()
                    if name:
                        parts.append(f"<url><loc>{base}/davlat/{quote(name)}</loc>"
                                     "<changefreq>weekly</changefreq><priority>0.5</priority></url>")
            cur.execute("SELECT DISTINCT year FROM movies WHERE year IS NOT NULL ORDER BY year DESC LIMIT 40")
            for (y,) in cur.fetchall():
                parts.append(f"<url><loc>{base}/yil/{y}</loc>"
                             "<changefreq>weekly</changefreq><priority>0.5</priority></url>")
    except Exception as ex:
        log.warning("sitemap country/year: %s", ex)
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT genre FROM movies WHERE genre IS NOT NULL AND genre <> ''")
            seen = set()
            for (g,) in cur.fetchall():
                for part in str(g).split(","):
                    name = part.strip()
                    if name and name.lower() not in seen:
                        seen.add(name.lower())
                        parts.append(f"<url><loc>{base}/janr/{quote(name)}</loc>"
                                     "<changefreq>weekly</changefreq><priority>0.6</priority></url>")
                        if len(seen) >= 40:
                            break
                if len(seen) >= 40:
                    break
    except Exception as ex:
        log.warning("sitemap genres: %s", ex)
    items = "".join(parts)
    ns_video = ' xmlns:video="http://www.google.com/schemas/sitemap-video/1.1"' if has_video else ''
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"{ns_video}>'
           f'{items}</urlset>')
    return Response(xml, mimetype="application/xml")

@app.route("/sitemap_video.xml")
def sitemap_video():
    """Video sitemap — Google Search Console 'Video' hisoboti uchun."""
    base = BASE_URL
    bot_username = BOT_USERNAME or ""
    parts = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, description, content_type, poster_url, poster_id, year, created_at
                FROM movies ORDER BY created_at DESC LIMIT 1000
            """)
            rows = cur.fetchall()
    except Exception as e:
        log.warning("sitemap_video: %s", e)
        rows = []

    import xml.etree.ElementTree as ET
    import html as _html_mod

    for row in rows:
        mid, title, desc, ctype, poster_url, poster_id, year, created = row
        title = title or "Kino"
        desc = (desc or f"{title} — o'zbek tilida onlayn ko'rish.")[:300]
        page_url = f"{base}/kino/{mid}"
        bot_link = f"https://t.me/{bot_username}?start=movie_{mid}" if bot_username else page_url
        abs_poster = poster_url if poster_url and poster_url.startswith("http") else (f"{base}/api/poster/{mid}" if poster_id else f"{base}/static/icon-512.png")
        upload_date = created.strftime("%Y-%m-%d") if created else (f"{year}-01-01" if year else "2024-01-01")

        e = _html_mod.escape
        video_xml = (
            f"<video:video>"
            f"<video:thumbnail_loc>{e(abs_poster)}</video:thumbnail_loc>"
            f"<video:title>{e(title)}</video:title>"
            f"<video:description>{e(desc)}</video:description>"
            f"<video:player_loc>{e(bot_link)}</video:player_loc>"
            f"<video:publication_date>{e(upload_date)}</video:publication_date>"
            f"<video:family_friendly>yes</video:family_friendly>"
            f"</video:video>"
        )
        parts.append(f"<url><loc>{e(page_url)}</loc>{video_xml}</url>")

    items = "".join(parts)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">'
        f'{items}</urlset>'
    )
    return Response(xml, mimetype="application/xml")

@app.route("/kino/<int:mid>")
def movie_page(mid):
    """Har kino uchun alohida HTML sahifa — Google o'qiy oladi (SEO).
    Foydalanuvchi ko'rsa, JS uni chiroyli ko'rsatadi; Google matnni o'qiydi."""
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, genre, year, language, quality, description,
                       COALESCE(content_type,'movie'), poster_id, poster_url, trailer,
                       COALESCE(is_premium, FALSE), original_title,
                       director, actors, country, duration, age_rating, tmdb_rating
                FROM movies WHERE id=%s
            """, (mid,))
            r = cur.fetchone()
    except Exception:
        r = None
    if not r:
        return send_from_directory("static", "index.html")
    # Foydalanuvchi izohlari reytingi — Google AggregateRating (⭐) uchun
    rev_avg = None
    rev_count = 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT AVG(rating), COUNT(*) FROM reviews WHERE movie_id=%s AND rating > 0", (mid,))
            rr = cur.fetchone()
            if rr and rr[1]:
                rev_count = int(rr[1])
                rev_avg = round(float(rr[0]), 1)
    except Exception:
        pass
    title = r[1] or "Kino"
    genre = r[2] or ""
    year = r[3] or ""
    ctype = r[7]
    language = r[4] or ""
    quality = r[5] or ""
    type_uz = {"movie":"Kino","series":"Serial","anime":"Anime","cartoon":"Multfilm"}.get(ctype,"Kino")
    is_prem = bool(r[11]) if len(r) > 11 else False
    orig_title = (r[12] or "").strip() if len(r) > 12 else ""
    director = (r[13] or "").strip() if len(r) > 13 else ""
    actors = (r[14] or "").strip() if len(r) > 14 else ""
    country = (r[15] or "").strip() if len(r) > 15 else ""
    duration = r[16] if len(r) > 16 else None
    age_rating = (r[17] or "").strip() if len(r) > 17 else ""
    tmdb_rating = float(r[18]) if len(r) > 18 and r[18] else None
    prem_badge = ('<span style="display:inline-block;background:linear-gradient(90deg,#f7d046,#e0950b);'
                  'color:#231803;font-size:13px;font-weight:700;padding:5px 14px;border-radius:20px;'
                  'margin-top:12px;">💎 Premium</span>') if is_prem else ''
    # Tavsif — bo'sh bo'lsa, har kino uchun O'ZIGA XOS matn hosil qilamiz.
    # (Bir xil shablon → Google "dublikat/thin kontent" deb indekslamaydi.)
    _rd = (r[6] or "").strip()
    if _rd:
        desc = _rd[:300]
    else:
        _g1 = genre.split(",")[0].strip() if genre else ""
        _head = f"{title} ({year})" if year else title
        _kind = f"{_g1} {type_uz.lower()}" if _g1 else type_uz.lower()
        _tail = ([f"{quality} sifat"] if quality else []) + ["bepul onlayn tomosha", "Telegram orqali yuklab olish"]
        desc = (f"{_head} — {_kind} o'zbek tilida tarjima. " + ", ".join(_tail) + ".")[:300]
    poster = r[9] or (f"/api/poster/{mid}" if r[8] else "")
    bot_link = f"https://t.me/{BOT_USERNAME}?start=movie_{mid}" if BOT_USERNAME else "#"
    e = _html.escape
    page_title = f"{title} ({year}) — o'zbek tilida | ASTRA" if year else f"{title} — o'zbek tilida | ASTRA"
    canonical = f"{BASE_URL}/kino/{mid}"
    abs_poster = poster if poster.startswith("http") else (f"{BASE_URL}{poster}" if poster else "")
    trailer_id = _yt_id(r[10] if len(r) > 10 else "")
    yt_embed = f"https://www.youtube.com/embed/{trailer_id}" if trailer_id else ""
    yt_thumb = f"https://img.youtube.com/vi/{trailer_id}/hqdefault.jpg" if trailer_id else ""

    # Boshqa kinolar (foydalanuvchi saytni kashf qilishi + ichki havolalar SEO uchun)
    more = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            g1 = genre.split(",")[0].strip() if genre else ""
            if g1:
                cur.execute("SELECT id, title, poster_id, poster_url FROM movies "
                            "WHERE id<>%s AND genre ILIKE %s "
                            "ORDER BY views DESC NULLS LAST, created_at DESC LIMIT 12",
                            (mid, f"%{g1}%"))
                more = cur.fetchall()
            if len(more) < 6:
                cur.execute("SELECT id, title, poster_id, poster_url FROM movies "
                            "WHERE id<>%s ORDER BY created_at DESC LIMIT 12", (mid,))
                more = cur.fetchall()
    except Exception:
        more = []
    more_cards = []
    for mm in more[:12]:
        m_id, m_title, m_pid, m_purl = mm[0], mm[1], mm[2], mm[3]
        mp = m_purl if m_purl else (f"/api/poster/{m_id}" if m_pid else "/static/no-poster.svg")
        more_cards.append(
            f'<a href="/kino/{m_id}" style="text-decoration:none;color:inherit;width:130px;flex:0 0 auto;">'
            f'<img src="{e(mp)}" alt="{e(m_title or "")}" loading="lazy" '
            f'style="width:130px;height:195px;object-fit:cover;border-radius:10px;background:#1a1a1a;display:block;">'
            f'<div style="font-size:12.5px;margin-top:7px;line-height:1.35;color:#cfcfcf;">{e((m_title or "")[:42])}</div>'
            f'</a>'
        )
    more_html = (
        '<section style="padding:18px 8px 48px;">'
        '<h2 style="font-family:Bebas Neue,sans-serif;font-size:26px;letter-spacing:1px;margin:0 0 16px;">Boshqa kinolar</h2>'
        '<div style="display:flex;gap:14px;overflow-x:auto;padding-bottom:10px;scrollbar-width:thin;">'
        + "".join(more_cards) + '</div></section>'
    ) if more_cards else ''

    # JSON-LD — Google "boyitilgan natija" uchun struktura ma'lumoti
    import json as _json
    schema_type = {"series":"TVSeries","anime":"TVSeries","cartoon":"TVSeries"}.get(ctype, "Movie")
    ld = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "name": title,
        "description": desc,
        "inLanguage": "uz",
        "url": canonical,
    }
    if abs_poster: ld["image"] = abs_poster
    if genre: ld["genre"] = [g.strip() for g in genre.split(",") if g.strip()]
    if orig_title: ld["alternateName"] = orig_title
    if director:
        ld["director"] = [{"@type": "Person", "name": nm.strip()} for nm in director.split(",") if nm.strip()]
    if actors:
        ld["actor"] = [{"@type": "Person", "name": nm.strip()} for nm in actors.split(",") if nm.strip()]
    if country:
        ld["countryOfOrigin"] = [{"@type": "Country", "name": c.strip()} for c in country.split(",") if c.strip()]
    if duration:
        try: ld["duration"] = f"PT{int(duration)}M"
        except Exception: pass
    if age_rating: ld["contentRating"] = age_rating
    if year:
        try: ld["dateCreated"] = str(int(year))
        except Exception: pass
    # AggregateRating — faqat haqiqiy foydalanuvchi baholari bo'lganda (Google qoidasiga mos)
    if rev_count and rev_avg:
        ld["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": rev_avg,
            "bestRating": 5,
            "worstRating": 1,
            "ratingCount": rev_count,
            "reviewCount": rev_count,
        }

    # VideoObject — Google Search Console "Video" hisoboti uchun ZARUR
    # VideoObject — FAQAT haqiqiy treyler (YouTube) bo'lganda. Aks holda Google xato beradi.
    video_jsonld = ""
    if trailer_id:
        video_ld = {
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": f"{title} — treyler",
            "description": desc or f"{title} treyler",
            "thumbnailUrl": yt_thumb,
            "uploadDate": (str(int(year)) + "-01-01") if year else "2024-01-01",
            "embedUrl": yt_embed,
            "contentUrl": f"https://www.youtube.com/watch?v={trailer_id}",
            "url": canonical,
            "inLanguage": "uz",
        }
        video_jsonld = _json.dumps(video_ld, ensure_ascii=False)

    # Sahifa struktura ma'lumoti
    jsonld = _json.dumps(ld, ensure_ascii=False)
    video_script = f'<script type="application/ld+json">{video_jsonld}</script>' if video_jsonld else ''
    # BreadcrumbList — qidiruvda "Bosh sahifa › Kcategory › Kino" yo'lini ko'rsatadi
    _cat_path = {"movie":"/kategoriya/movie","series":"/kategoriya/series",
                 "anime":"/kategoriya/anime","cartoon":"/kategoriya/cartoon"}.get(ctype, "/kategoriya/movie")
    _cat_name = {"movie":"Kinolar","series":"Seriallar","anime":"Anime",
                 "cartoon":"Multfilmlar"}.get(ctype, "Kinolar")
    breadcrumb_ld = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Bosh sahifa", "item": BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": _cat_name, "item": BASE_URL + _cat_path},
            {"@type": "ListItem", "position": 3, "name": title, "item": canonical},
        ]
    }
    breadcrumb_script = f'<script type="application/ld+json">{_json.dumps(breadcrumb_ld, ensure_ascii=False)}</script>'
    ad_html = _render_ad_banner(_get_site_ad())   # reklama banner (bo'lmasa bo'sh)
    # Ulashish uchun JS-xavfsiz qiymatlar (kerakli qo'shtirnoq/maxsus belgilar ekranlanadi)
    share_url_js = _json.dumps(canonical)
    share_title_js = _json.dumps(title)

    # Treyler — to'g'ridan-to'g'ri qo'yilgan iframe (bosish shart emas, ishonchli ochiladi)
    if trailer_id:
        trailer_html = (
            '<section style="padding:14px 8px 4px;">'
            '<h2 style="font-family:Bebas Neue,sans-serif;font-size:24px;letter-spacing:1px;margin:0 0 12px;">🎬 Treyler</h2>'
            '<div style="position:relative;width:100%;aspect-ratio:16/9;border-radius:12px;overflow:hidden;background:#000;">'
            f'<iframe src="https://www.youtube.com/embed/{trailer_id}?rel=0&modestbranding=1" '
            'title="Treyler" loading="lazy" frameborder="0" '
            'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
            'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen '
            'style="position:absolute;inset:0;width:100%;height:100%;border:0;"></iframe>'
            '</div>'
            '<div style="margin-top:10px;">'
            f'<a href="https://www.youtube.com/watch?v={trailer_id}" target="_blank" rel="noopener" '
            'style="display:inline-flex;align-items:center;gap:6px;color:#9b93c4;text-decoration:none;font-size:13px;">'
            '▶ Agar treyler ochilmasa — YouTube\'da ko\'rish</a>'
            '</div>'
            '</section>'
        )
    else:
        trailer_html = ''
    # Ko'rinadigan reyting (schema'dagi qiymat bilan mos — Google talabi)
    if rev_count and rev_avg:
        filled = max(0, min(5, int(round(rev_avg))))
        stars = "★" * filled + "☆" * (5 - filled)
        baho_uz = "baho" if rev_count == 1 else "ta baho"
        rating_html = (
            '<div style="display:flex;align-items:center;gap:9px;margin:12px 0;flex-wrap:wrap;">'
            f'<span style="color:#ffc107;font-size:21px;letter-spacing:2px;line-height:1;">{stars}</span>'
            f'<b style="font-size:17px;color:#fff;">{rev_avg}</b>'
            f'<span style="color:#a3a3a3;font-size:14px;">/ 5 · {rev_count} {baho_uz}</span>'
            '</div>'
        )
    else:
        rating_html = ''
    tmdb_badge = (
        f'<span style="display:inline-flex;align-items:center;gap:5px;background:rgba(90,209,255,0.12);'
        f'border:1px solid rgba(90,209,255,0.4);color:#5ad1ff;font-size:13px;font-weight:600;'
        f'padding:5px 12px;border-radius:20px;margin:6px 0 0;">TMDB ★ {tmdb_rating}</span>'
    ) if tmdb_rating else ''
    page = f"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">
<link rel="shortcut icon" href="/favicon.ico">
<link rel="icon" type="image/png" sizes="512x512" href="/static/icon-512.png">
<link rel="shortcut icon" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<meta name="google-site-verification" content="NWyfq_vRf53C8JMiGFZ8xL666JbpZg4NJAfKzabPoik" />
<title>{e(page_title)}</title>
<meta name="description" content="{e(desc)}">
<meta name="keywords" content="{e(title)}, {e(orig_title) + ', ' if orig_title else ''}{e(genre)}, {e(actors) + ', ' if actors else ''}{e(director) + ', ' if director else ''}o'zbek tilida, uzbek tilida, {year}, onlayn kino, tarjima">
<link rel="canonical" href="{e(BASE_URL)}/kino/{mid}">
<meta property="og:type" content="video.movie">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{e(canonical)}">
<meta property="og:site_name" content="ASTRA">
{f'<meta property="og:image" content="{e(abs_poster)}">' if abs_poster else ''}
<meta property="og:locale" content="uz_UZ">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}">
{f'<meta name="twitter:image" content="{e(abs_poster)}">' if abs_poster else ''}
<script type="application/ld+json">{jsonld}</script>
{video_script}
{breadcrumb_script}
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<nav id="navbar"><a href="/" class="nav-logo" aria-label="ASTRA"><img src="/static/logo.svg" alt="ASTRA" class="nav-logo-img"></a></nav>
<main>
  <header style="position:relative; overflow:hidden;">
    {f'<div style="position:absolute; inset:0; background:#1a1640 center/cover no-repeat url(&quot;{e(poster)}&quot;);"></div>' if poster else '<div style="position:absolute; inset:0; background:#1a1640;"></div>'}
    <div style="position:absolute; inset:0; background:linear-gradient(180deg, rgba(18,16,42,0.55) 0%, rgba(18,16,42,0.82) 70%, #12102a 100%);"></div>
    <div style="position:relative; max-width:1180px; margin:0 auto; padding:110px 32px 48px; display:flex; gap:36px; flex-wrap:wrap; align-items:flex-start;">
      {f'<img src="{e(poster)}" alt="{e(title)}" style="width:200px; aspect-ratio:2/3; object-fit:cover; border-radius:12px; display:block; box-shadow:0 16px 50px rgba(0,0,0,0.7); flex:0 0 auto;">' if poster else ''}
      <div style="flex:1; min-width:300px;">
        <h1 style="font-family:Bebas Neue,sans-serif; font-size:48px; letter-spacing:1px; line-height:1.02; margin:0;">{e(title)}</h1>
        {f'<p style="font-style:italic; color:#b8b4d8; margin:6px 0 0; font-size:16px;">{e(orig_title)}</p>' if orig_title else ''}
        <p style="color:#cbb8f0; margin:12px 0 0; font-size:16px;">{type_uz}{f' · {year}' if year else ''}{f' · {e(genre)}' if genre else ''}{f' · {e(country)}' if country else ''}{f' · {duration} daq' if duration else ''}{f' · {e(age_rating)}' if age_rating else ''}</p>
        {f'<p style="color:#a99ee0; margin:10px 0 0; font-size:14.5px;"><b>Rejissyor:</b> {e(director)}</p>' if director else ''}
        {f'<p style="color:#a99ee0; margin:4px 0 0; font-size:14.5px;"><b>Aktyorlar:</b> {e(actors)}</p>' if actors else ''}
        {tmdb_badge}
        {prem_badge}
        {rating_html}
        <p style="line-height:1.75; color:#dcdcea; margin:18px 0 20px; font-size:15.5px; max-width:680px;">{e(desc)}</p>
        <div style="background:rgba(42,171,238,0.12); border:1px solid rgba(42,171,238,0.45); border-radius:10px; padding:13px 16px; margin:0 0 20px; color:#d6ecff; font-size:14px; line-height:1.6; max-width:680px;">
          <b>ℹ️ Eslatma:</b> Ushbu {type_uz.lower()} <b>Telegram bot</b> orqali ko'riladi — tugmani bossangiz, botimizda bemalol tomosha qilasiz yoki yuklab olasiz. Tez, bepul va ro'yxatdan o'tmasdan.
        </div>
        <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:center;">
          <a href="{e(bot_link)}" style="display:inline-block; background:#229ed9; color:#fff; padding:15px 30px; border-radius:8px; text-decoration:none; font-weight:600;">▶ Telegram botda ko'rish</a>
          <button onclick="astraShare()" style="display:inline-flex; align-items:center; gap:8px; background:rgba(124,92,255,0.18); color:#fff; padding:15px 26px; border:1px solid rgba(124,92,255,0.5); border-radius:8px; font-weight:600; font-size:15px; font-family:inherit; cursor:pointer;">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>
            Do'stga yuborish
          </button>
          <span id="astraShareMsg" style="display:none; color:#67e08a; font-size:14px;">✅ Havola nusxa olindi!</span>
        </div>
      </div>
    </div>
  </header>

  <div style="max-width:1180px; margin:0 auto; padding:0 32px;">
    {ad_html}
    {trailer_html}
    {more_html}
    <div style="text-align:center; padding:28px 8px 16px;">
      <a href="/" style="display:inline-flex; align-items:center; gap:10px; background:#7c5cff; color:#fff; padding:16px 40px; border-radius:12px; text-decoration:none; font-weight:700; font-size:16px; box-shadow:0 10px 30px rgba(124,92,255,0.45);">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        Barcha kinolarni ko'rish
      </a>
    </div>
  </div>
</main>
<script>
function loadTrailer(el){{
  var id = el.getAttribute('data-yt');
  if(!id) return;
  el.innerHTML = '<iframe width="100%" height="100%" src="https://www.youtube-nocookie.com/embed/'+id+'?autoplay=1&rel=0" title="Treyler" frameborder="0" allow="autoplay; encrypted-media; fullscreen" allowfullscreen style="position:absolute;inset:0;width:100%;height:100%;border:0;"></iframe>';
}}
var ASTRA_SHARE_URL = {share_url_js};
var ASTRA_SHARE_TITLE = {share_title_js};
function astraShare(){{
  var txt = ASTRA_SHARE_TITLE + ' — ASTRA da ko\'ring 🎬';
  if (navigator.share){{
    navigator.share({{ title: ASTRA_SHARE_TITLE, text: txt, url: ASTRA_SHARE_URL }}).catch(function(){{}});
    return;
  }}
  // Native ulashish yo'q — havolani nusxalaymiz va Telegram'ni ochamiz
  function show(){{ var m=document.getElementById('astraShareMsg'); if(m){{ m.style.display='inline'; setTimeout(function(){{ m.style.display='none'; }}, 2200); }} }}
  if (navigator.clipboard && navigator.clipboard.writeText){{
    navigator.clipboard.writeText(ASTRA_SHARE_URL).then(show).catch(function(){{}});
  }}
  window.open('https://t.me/share/url?url=' + encodeURIComponent(ASTRA_SHARE_URL) + '&text=' + encodeURIComponent(ASTRA_SHARE_TITLE + ' — ASTRA da ko\'ring 🎬'), '_blank');
}}
</script>
<script src="/static/ad-interstitial.js"></script>
</body>
</html>"""
    return Response(page, mimetype="text/html")

# ══════════════════ SEO LISTING (kategoriya / janr / top) ══════════════════
_TYPE_UZ = {"movie": "Kinolar", "series": "Seriallar", "anime": "Anime", "cartoon": "Multfilmlar"}
_TYPE_UZ_ONE = {"movie": "Kino", "series": "Serial", "anime": "Anime", "cartoon": "Multfilm"}

def _page_window(cur, total):
    s = sorted({p for p in [1, total, cur, cur-1, cur-2, cur+1, cur+2] if 1 <= p <= total})
    out, prev = [], 0
    for p in s:
        if prev and p - prev > 1:
            out.append("...")
        out.append(p); prev = p
    return out

def _list_movies_seo(ctype=None, genre=None, country=None, year=None, rated=False, sort="new", page=1, per=24):
    where, params = [], []
    if ctype:
        where.append("COALESCE(content_type,'movie') = %s"); params.append(ctype)
    if genre:
        where.append("genre ILIKE %s"); params.append(f"%{genre}%")
    if country:
        where.append("country ILIKE %s"); params.append(f"%{country}%")
    if year:
        where.append("year = %s"); params.append(year)
    if rated:
        where.append("rating IS NOT NULL AND rating > 0")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    order = {"new": "created_at DESC", "rating": "rating DESC NULLS LAST",
             "popular": "views DESC NULLS LAST"}.get(sort, "created_at DESC")
    offset = (page - 1) * per
    rows, total = [], 0
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM movies {wsql}", params)
            total = cur.fetchone()[0] or 0
            cur.execute(f"SELECT id, title, year, poster_url, poster_id, rating, content_type "
                        f"FROM movies {wsql} ORDER BY {order} LIMIT %s OFFSET %s",
                        params + [per, offset])
            rows = cur.fetchall()
    except Exception as ex:
        log.warning("list seo: %s", ex)
    return rows, total

def _render_listing(h1, intro, rows, total, page, per, base_path, crumb_label):
    e = html.escape
    base = BASE_URL
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    canon_base = f"{base}{base_path}"
    canonical = canon_base + (f"?page={page}" if page > 1 else "")

    cards, item_list = [], []
    for i, r in enumerate(rows):
        mid, mtitle, year, purl, pid, rating, ctype = r
        poster = purl or (f"/api/poster/{mid}" if pid else "/static/no-poster.svg")
        rate = f'<span class="lc-rate">⭐ {float(rating):.1f}</span>' if rating else ''
        meta = " · ".join([x for x in [str(year) if year else "",
                                       _TYPE_UZ_ONE.get(ctype or "movie", "Kino")] if x])
        cards.append(
            f'<a class="lc" href="/kino/{mid}">'
            f'<span class="lc-img"><img src="{e(poster)}" alt="{e(mtitle or "")}" loading="lazy">{rate}</span>'
            f'<span class="lc-t">{e(mtitle or "")}</span><span class="lc-m">{e(meta)}</span></a>'
        )
        item_list.append({"@type": "ListItem", "position": (page-1)*per + i + 1,
                          "url": f"{base}/kino/{mid}", "name": mtitle or f"Film #{mid}"})
    grid = "".join(cards) or '<p style="color:#888;padding:20px 0;">Bu bo\'limda hozircha kino yo\'q.</p>'

    pag = ""
    if pages > 1:
        def lk(p): return base_path + (f"?page={p}" if p > 1 else "")
        parts = []
        if page > 1: parts.append(f'<a class="pg" href="{lk(page-1)}" rel="prev">‹</a>')
        for p in _page_window(page, pages):
            if p == "...": parts.append('<span class="pg gap">…</span>')
            elif p == page: parts.append(f'<span class="pg cur">{p}</span>')
            else: parts.append(f'<a class="pg" href="{lk(p)}">{p}</a>')
        if page < pages: parts.append(f'<a class="pg" href="{lk(page+1)}" rel="next">›</a>')
        pag = '<nav class="pglist">' + "".join(parts) + '</nav>'

    prev_link = (f'<link rel="prev" href="{canon_base}'
                 + (f'?page={page-1}' if page-1 > 1 else '') + '">') if page > 1 else ""
    next_link = f'<link rel="next" href="{canon_base}?page={page+1}">' if page < pages else ""

    import json as _json
    breadcrumb = {"@context": "https://schema.org", "@type": "BreadcrumbList",
                  "itemListElement": [
                      {"@type": "ListItem", "position": 1, "name": "Bosh sahifa", "item": base + "/"},
                      {"@type": "ListItem", "position": 2, "name": crumb_label, "item": canon_base}]}
    itemlist = {"@context": "https://schema.org", "@type": "ItemList",
                "name": h1, "numberOfItems": total, "itemListElement": item_list}
    desc = (f"{h1} — ASTRA. {total} ta. Eng so'nggi kinolar, seriallar, anime va "
            "multfilmlar o'zbek tilida, bepul.")

    page_html = f"""<!DOCTYPE html>
<html lang="uz"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(h1)}{(' — sahifa ' + str(page)) if page>1 else ''} | ASTRA</title>
<meta name="description" content="{e(desc)}">
<link rel="canonical" href="{canonical}">
{prev_link}{next_link}
<meta property="og:title" content="{e(h1)} | ASTRA">
<meta property="og:description" content="{e(desc)}">
<meta property="og:type" content="website"><meta property="og:url" content="{canonical}">
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icon-192.png">
<link rel="shortcut icon" href="/favicon.ico">
<script type="application/ld+json">{_json.dumps(breadcrumb, ensure_ascii=False)}</script>
<script type="application/ld+json">{_json.dumps(itemlist, ensure_ascii=False)}</script>
<style>
  .seo-wrap{{max-width:1200px;margin:0 auto;padding:18px 16px 60px;}}
  .seo-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}}
  .seo-top .logo img{{height:30px;display:block;}}
  .seo-crumb{{font-size:13px;color:#8c87b8;margin-bottom:6px;}}
  .seo-crumb a{{color:#5ad1ff;text-decoration:none;}}
  .seo-wrap h1{{font-size:26px;margin:0 0 6px;color:#fff;}}
  .seo-intro{{color:#b8b4d8;font-size:14px;margin:0 0 22px;}}
  .lc-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;}}
  .lc{{text-decoration:none;color:inherit;display:block;}}
  .lc-img{{position:relative;display:block;aspect-ratio:2/3;border-radius:10px;overflow:hidden;background:#252154;}}
  .lc-img img{{width:100%;height:100%;object-fit:cover;display:block;transition:transform .25s;}}
  .lc:hover .lc-img img{{transform:scale(1.05);}}
  .lc-rate{{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.7);color:#ffd166;font-size:11px;font-weight:700;padding:2px 7px;border-radius:999px;}}
  .lc-t{{display:block;font-size:14px;color:#fff;margin-top:8px;font-weight:500;line-height:1.3;}}
  .lc-m{{display:block;font-size:12px;color:#8c87b8;margin-top:2px;}}
  .pglist{{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin:34px 0 0;}}
  .pglist .pg{{min-width:38px;height:38px;display:inline-flex;align-items:center;justify-content:center;padding:0 11px;border-radius:9px;background:#1b1840;border:1px solid #252154;color:#b8b4d8;text-decoration:none;font-size:14px;}}
  .pglist .pg:hover{{border-color:#7c5cff;color:#fff;}}
  .pglist .pg.cur{{background:#7c5cff;border-color:#7c5cff;color:#fff;font-weight:700;}}
  .pglist .pg.gap{{background:none;border:none;}}
  .seo-back{{display:inline-flex;align-items:center;gap:8px;margin-top:36px;color:#fff;
    text-decoration:none;font-size:14.5px;font-weight:600;padding:12px 26px;border-radius:12px;
    background:linear-gradient(135deg,#7c5cff,#5a8cff);box-shadow:0 6px 20px rgba(124,92,255,.35);
    transition:transform .15s ease, box-shadow .15s ease;}}
  .seo-back:hover{{transform:translateY(-2px);box-shadow:0 10px 26px rgba(124,92,255,.5);}}
  .seo-backwrap{{text-align:center;}}
</style>
</head><body>
<div class="seo-wrap">
  <div class="seo-top">
    <a class="logo" href="/"><img src="/static/logo.svg" alt="ASTRA"></a>
    <a href="/" style="color:#8c87b8;text-decoration:none;font-size:14px;">Bosh sahifa ↗</a>
  </div>
  <div class="seo-crumb"><a href="/">Bosh sahifa</a> › {e(crumb_label)}</div>
  <h1>{e(h1)}{(' — sahifa ' + str(page)) if page>1 else ''}</h1>
  <p class="seo-intro">{e(intro)} <b>{total}</b> ta.</p>
  <div class="lc-grid">{grid}</div>
  {pag}
  <div class="seo-backwrap">
    <a class="seo-back" href="/">← Bosh sahifaga qaytish</a>
  </div>
</div>
</body></html>"""
    return Response(page_html, mimetype="text/html")

@app.route("/kategoriya/<ctype>")
def seo_category(ctype):
    if ctype not in _TYPE_UZ:
        return redirect("/")
    try: page = max(1, int(request.args.get("page", 1)))
    except Exception: page = 1
    rows, total = _list_movies_seo(ctype=ctype, sort="new", page=page)
    label = _TYPE_UZ[ctype]
    return _render_listing(label, f"Eng so'nggi {label.lower()} o'zbek tilida —",
                           rows, total, page, 24, f"/kategoriya/{ctype}", label)

@app.route("/janr/<path:genre>")
def seo_genre(genre):
    genre = (genre or "").strip()[:60]
    if not genre:
        return redirect("/")
    try: page = max(1, int(request.args.get("page", 1)))
    except Exception: page = 1
    rows, total = _list_movies_seo(genre=genre, sort="new", page=page)
    return _render_listing(f"{genre} — kinolar", f"«{genre}» janridagi eng yaxshi kinolar —",
                           rows, total, page, 24, f"/janr/{quote(genre)}", genre)

@app.route("/top")
def seo_top():
    try: page = max(1, int(request.args.get("page", 1)))
    except Exception: page = 1
    rows, total = _list_movies_seo(rated=True, sort="rating", page=page)
    return _render_listing("Eng yaxshilar", "Reyting bo'yicha eng zo'r kinolar —",
                           rows, total, page, 24, "/top", "Eng yaxshilar")

@app.route("/trend")
def seo_trend():
    try: page = max(1, int(request.args.get("page", 1)))
    except Exception: page = 1
    rows, total = _list_movies_seo(sort="popular", page=page)
    return _render_listing("Mashhur kinolar", "Eng ko'p tomosha qilingan kinolar —",
                           rows, total, page, 24, "/trend", "Mashhur kinolar")

@app.route("/davlat/<path:country>")
def seo_country(country):
    country = (country or "").strip()[:60]
    if not country:
        return redirect("/")
    try: page = max(1, int(request.args.get("page", 1)))
    except Exception: page = 1
    rows, total = _list_movies_seo(country=country, sort="new", page=page)
    return _render_listing(f"{country} kinolari", f"«{country}» davlatida ishlab chiqarilgan kinolar —",
                           rows, total, page, 24, f"/davlat/{quote(country)}", country)

@app.route("/yil/<int:year>")
def seo_year(year):
    if year < 1900 or year > 2100:
        return redirect("/")
    try: page = max(1, int(request.args.get("page", 1)))
    except Exception: page = 1
    rows, total = _list_movies_seo(year=year, sort="new", page=page)
    return _render_listing(f"{year}-yil kinolari", f"{year} yilda chiqqan kinolar va seriallar —",
                           rows, total, page, 24, f"/yil/{year}", str(year))

# ══════════════════ ADMIN ══════════════════
def _check(d):
    # Sessiyada admin bo'lsa — parol shart emas; aks holda parol orqali (zaxira)
    if session.get("is_admin"):
        return True
    pw = (d or {}).get("password") or ""
    return bool(pw) and hmac.compare_digest(pw, ADMIN_PASSWORD)

_ADMIN_LOG_DDL = """CREATE TABLE IF NOT EXISTS admin_log (
    id SERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT NOW()
)"""

def _log_admin(action, target="", details=""):
    """Admin harakatini jurnalga yozadi (fon oqimida — javobni sekinlashtirmaydi)."""
    def worker():
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                try:
                    cur.execute("INSERT INTO admin_log (action, target, details) VALUES (%s,%s,%s)",
                                (action, str(target)[:200], str(details)[:500]))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    cur.execute(_ADMIN_LOG_DDL)
                    cur.execute("INSERT INTO admin_log (action, target, details) VALUES (%s,%s,%s)",
                                (action, str(target)[:200], str(details)[:500]))
                    conn.commit()
        except Exception as e:
            log.warning("log_admin: %s", e)
    threading.Thread(target=worker, daemon=True).start()

@app.route("/api/admin/log", methods=["POST"])
def admin_log_list():
    """So'nggi admin harakatlari jurnali."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute("""SELECT action, target, details, created_at FROM admin_log
                               ORDER BY created_at DESC LIMIT 100""")
                rows = cur.fetchall()
            except Exception:
                # Jadval hali yo'q bo'lsa (eski deploy) — shu yerda yaratib, bo'sh ro'yxat qaytaramiz
                conn.rollback()
                cur.execute(_ADMIN_LOG_DDL)
                conn.commit()
                rows = []
        items = [{"action": r[0], "target": r[1] or "", "details": r[2] or "",
                   "date": r[3].strftime("%Y-%m-%d %H:%M") if r[3] else ""} for r in rows]
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/broadcast", methods=["POST"])
def admin_broadcast():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Xabar matni bo'sh"}), 400
    btn_label = (d.get("button_label") or "").strip() or None
    btn_url = (d.get("button_url") or "").strip() or None
    count = _broadcast_to_all(text, btn_label, btn_url)
    _log_admin("broadcast", "", text[:100])
    return jsonify({"ok": True, "recipients": count})

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    ip = _client_ip()
    if _login_blocked(ip):
        _log_admin("login_blocked", "", ip)
        return jsonify({"ok": False, "error": "Juda ko'p noto'g'ri urinish. 15 daqiqadan keyin qayta urinib ko'ring."}), 429

    pw = (request.get_json() or {}).get("password") or ""
    ok = bool(pw) and hmac.compare_digest(pw, ADMIN_PASSWORD)
    if ok:
        _login_clear(ip)
        session.permanent = True
        session["is_admin"] = True
        _log_admin("login", "", ip)
    else:
        _login_register_fail(ip)
        _log_admin("login_failed", "", ip)
    return jsonify({"ok": ok})

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return jsonify({"ok": True})

@app.route("/api/admin/check")
def admin_check():
    return jsonify({"admin": bool(session.get("is_admin"))})

# ── Admin statistika (batafsil) ──
def _safe_query(cur, sql, params=None, default=None):
    """Bitta so'rov xato bersa ham qolgan statistikalar ishlashda davom etsin."""
    try:
        cur.execute(sql, params or ())
        return cur.fetchall()
    except Exception as e:
        log.warning("stats query: %s | %s", e, sql[:80])
        return default if default is not None else []

@app.route("/api/admin/stats", methods=["POST"])
def admin_stats():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    out = {
        "total": 0, "by_type": {}, "total_views": 0, "top": [], "no_poster": 0,
        "avg_views": 0, "no_views": 0, "by_genre": [], "by_year": [], "by_quality": [],
        "by_language": [], "by_country": [], "added_7d": 0, "added_30d": 0,
        "added_today": 0, "by_day": [], "top_rated": [], "no_rating": 0, "avg_rating": 0,
        "reviews_total": 0, "reviews_7d": 0, "top_reviewed": [], "top_commenters": [],
        "favorites_total": 0, "most_favorited": [],
        "upcoming_pending": 0, "upcoming_soon": 0, "upcoming_released": 0, "top_requested": [],
        "notifications_total": 0, "notifications_unread": 0,
        "ads_total": 0, "ads_active": 0,
        "channels_total": 0, "channels_active": 0, "channels_by_category": [],
        "known_users": 0, "active_users_7d": 0,
        "premium_total": 0,
    }
    try:
        with get_conn() as conn:
            cur = conn.cursor()

            row = _safe_query(cur, "SELECT COUNT(*), COALESCE(SUM(views),0), COALESCE(AVG(views),0) FROM movies", default=[(0,0,0)])[0]
            out["total"] = row[0]; out["total_views"] = int(row[1] or 0)
            out["avg_views"] = round(float(row[2] or 0), 1)

            for r in _safe_query(cur, "SELECT COALESCE(content_type,'movie'), COUNT(*) FROM movies GROUP BY 1"):
                out["by_type"][r[0]] = r[1]

            out["top"] = [{"id": r[0], "title": r[1], "views": r[2]} for r in _safe_query(
                cur, "SELECT id, title, COALESCE(views,0) FROM movies ORDER BY COALESCE(views,0) DESC LIMIT 10")]

            out["no_poster"] = _safe_query(cur, """SELECT COUNT(*) FROM movies
                WHERE (poster_url IS NULL OR poster_url='') AND (poster_id IS NULL OR poster_id='')""",
                default=[(0,)])[0][0]
            out["no_views"] = _safe_query(cur, "SELECT COUNT(*) FROM movies WHERE COALESCE(views,0)=0", default=[(0,)])[0][0]

            try:
                out["premium_total"] = _safe_query(cur, "SELECT COUNT(*) FROM movies WHERE is_premium=TRUE", default=[(0,)])[0][0]
            except Exception:
                pass

            out["by_genre"] = [{"name": r[0], "count": r[1]} for r in _safe_query(cur, """
                SELECT TRIM(genre), COUNT(*) FROM movies
                WHERE genre IS NOT NULL AND genre <> ''
                GROUP BY 1 ORDER BY 2 DESC LIMIT 12""")]

            out["by_year"] = [{"year": r[0], "count": r[1]} for r in _safe_query(cur, """
                SELECT year, COUNT(*) FROM movies
                WHERE year IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 15""")]

            out["by_quality"] = [{"name": r[0], "count": r[1]} for r in _safe_query(cur, """
                SELECT quality, COUNT(*) FROM movies
                WHERE quality IS NOT NULL AND quality <> ''
                GROUP BY 1 ORDER BY 2 DESC""")]

            out["by_language"] = [{"name": r[0], "count": r[1]} for r in _safe_query(cur, """
                SELECT language, COUNT(*) FROM movies
                WHERE language IS NOT NULL AND language <> ''
                GROUP BY 1 ORDER BY 2 DESC""")]

            out["by_country"] = [{"name": r[0], "count": r[1]} for r in _safe_query(cur, """
                SELECT country, COUNT(*) FROM movies
                WHERE country IS NOT NULL AND country <> ''
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")]

            row = _safe_query(cur, """SELECT
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'),
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'),
                    COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE)
                FROM movies""", default=[(0,0,0)])[0]
            out["added_7d"], out["added_30d"], out["added_today"] = row[0], row[1], row[2]

            out["by_day"] = [{"date": str(r[0]), "count": r[1]} for r in _safe_query(cur, """
                SELECT created_at::date, COUNT(*) FROM movies
                WHERE created_at >= NOW() - INTERVAL '14 days'
                GROUP BY 1 ORDER BY 1""")]

            row = _safe_query(cur, "SELECT COUNT(*) FILTER (WHERE rating IS NULL), COALESCE(AVG(rating),0) FROM movies", default=[(0,0)])[0]
            out["no_rating"] = row[0]; out["avg_rating"] = round(float(row[1] or 0), 2)
            out["top_rated"] = [{"id": r[0], "title": r[1], "rating": float(r[2])} for r in _safe_query(cur, """
                SELECT id, title, rating FROM movies
                WHERE rating IS NOT NULL ORDER BY rating DESC LIMIT 5""")]

            row = _safe_query(cur, """SELECT COUNT(*),
                    COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')
                FROM reviews""", default=[(0,0)])[0]
            out["reviews_total"], out["reviews_7d"] = row[0], row[1]

            out["top_reviewed"] = [{"movie_id": r[0], "title": r[1], "count": r[2]} for r in _safe_query(cur, """
                SELECT rv.movie_id, m.title, COUNT(*) c FROM reviews rv
                LEFT JOIN movies m ON m.id = rv.movie_id
                GROUP BY 1,2 ORDER BY c DESC LIMIT 5""")]

            out["top_commenters"] = [{"user_name": r[0] or "Noma'lum", "count": r[1]} for r in _safe_query(cur, """
                SELECT user_name, COUNT(*) c FROM reviews
                GROUP BY 1 ORDER BY c DESC LIMIT 5""")]

            out["favorites_total"] = _safe_query(cur, "SELECT COUNT(*) FROM favorites", default=[(0,)])[0][0]
            out["most_favorited"] = [{"title": r[0], "count": r[1]} for r in _safe_query(cur, """
                SELECT title, COUNT(*) c FROM favorites
                WHERE title IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 5""")]

            for r in _safe_query(cur, "SELECT status, COUNT(*) FROM upcoming GROUP BY 1"):
                if r[0] == "pending": out["upcoming_pending"] = r[1]
                elif r[0] == "soon": out["upcoming_soon"] = r[1]
                elif r[0] == "released": out["upcoming_released"] = r[1]
            out["top_requested"] = [{"title": r[0], "subs": r[1]} for r in _safe_query(cur, """
                SELECT u.title, COUNT(us.user_id) c FROM upcoming u
                LEFT JOIN upcoming_subs us ON us.upcoming_id = u.id
                WHERE u.status != 'released'
                GROUP BY u.id, u.title ORDER BY c DESC LIMIT 5""")]

            row = _safe_query(cur, "SELECT COUNT(*), COUNT(*) FILTER (WHERE is_read=FALSE) FROM notifications", default=[(0,0)])[0]
            out["notifications_total"], out["notifications_unread"] = row[0], row[1]

            row = _safe_query(cur, "SELECT COUNT(*), COUNT(*) FILTER (WHERE is_active=TRUE) FROM ads", default=[(0,0)])[0]
            out["ads_total"], out["ads_active"] = row[0], row[1]

            row = _safe_query(cur, "SELECT COUNT(*), COUNT(*) FILTER (WHERE is_active=TRUE) FROM tv_channels", default=[(0,0)])[0]
            out["channels_total"], out["channels_active"] = row[0], row[1]
            out["channels_by_category"] = [{"name": r[0] or "Umumiy", "count": r[1]} for r in _safe_query(cur, """
                SELECT category, COUNT(*) FROM tv_channels GROUP BY 1 ORDER BY 2 DESC""")]

            uids = _safe_query(cur, """
                SELECT user_id FROM favorites
                UNION SELECT user_id FROM reviews
                UNION SELECT user_id FROM upcoming_subs
                UNION SELECT user_id FROM notifications""")
            out["known_users"] = len(uids)

            out["active_users_7d"] = _safe_query(cur, """
                SELECT COUNT(DISTINCT user_id) FROM reviews WHERE created_at >= NOW() - INTERVAL '7 days'
                """, default=[(0,)])[0][0]

        return jsonify(out)
    except Exception as e:
        log.warning("admin_stats: %s", e)
        return jsonify({"error": str(e)}), 500

# ── TMDB qidiruv (poster + ma'lumotni avtomatik olish) ──
_TMDB_GENRES = {}

def _tmdb_headers():
    if TMDB_TOKEN:
        return {"Authorization": f"Bearer {TMDB_TOKEN}", "accept": "application/json"}
    return {"accept": "application/json"}

def _tmdb_get(path, params=None):
    params = dict(params or {})
    if not TMDB_TOKEN and TMDB_KEY:
        params["api_key"] = TMDB_KEY
    url = f"https://api.themoviedb.org/3{path}"
    r = requests.get(url, headers=_tmdb_headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def _tmdb_load_genres():
    global _TMDB_GENRES
    if _TMDB_GENRES:
        return
    try:
        for kind in ("movie", "tv"):
            data = _tmdb_get(f"/genre/{kind}/list", {"language": "ru-RU"})
            for g in data.get("genres", []):
                _TMDB_GENRES[g["id"]] = g["name"]
    except Exception as e:
        log.warning("tmdb genres: %s", e)

# ── Statistika kartasiga bosilganda — tegishli kontent ro'yxati ──
_STAT_DETAIL_QUERIES = {
    "no_poster": ("Postersiz kontent", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE (poster_url IS NULL OR poster_url='') AND (poster_id IS NULL OR poster_id='')
        ORDER BY id DESC LIMIT 200"""),
    "no_views": ("Ko'rishsiz kontent", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE COALESCE(views,0)=0 ORDER BY id DESC LIMIT 200"""),
    "no_rating": ("Reytingsiz kontent", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE rating IS NULL ORDER BY id DESC LIMIT 200"""),
    "premium_total": ("Premium kontent", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE is_premium=TRUE ORDER BY id DESC LIMIT 200"""),
    "added_today": ("Bugun qo'shilgan", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE created_at >= CURRENT_DATE ORDER BY created_at DESC LIMIT 200"""),
    "added_7d": ("So'nggi 7 kunda qo'shilgan", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE created_at >= NOW() - INTERVAL '7 days' ORDER BY created_at DESC LIMIT 200"""),
    "added_30d": ("So'nggi 30 kunda qo'shilgan", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE created_at >= NOW() - INTERVAL '30 days' ORDER BY created_at DESC LIMIT 200"""),
    "total": ("Barcha kontent", """SELECT id, title, COALESCE(views,0) FROM movies
        ORDER BY id DESC LIMIT 200"""),
    "type_movie": ("Kinolar", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE COALESCE(content_type,'movie')='movie' ORDER BY id DESC LIMIT 200"""),
    "type_series": ("Seriallar", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE content_type='series' ORDER BY id DESC LIMIT 200"""),
    "type_anime": ("Animelar", """SELECT id, title, COALESCE(views,0) FROM movies
        WHERE content_type='anime' ORDER BY id DESC LIMIT 200"""),
}

@app.route("/api/admin/stats/detail", methods=["POST"])
def admin_stats_detail():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    key = (d.get("key") or "").strip()
    info = _STAT_DETAIL_QUERIES.get(key)
    if not info:
        return jsonify({"error": "noma'lum kalit"}), 400
    label, sql = info
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            rows = _safe_query(cur, sql)
        items = [{"id": r[0], "title": r[1], "views": r[2]} for r in rows]
        return jsonify({"label": label, "items": items, "count": len(items)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/tmdb", methods=["POST"])
def admin_tmdb():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    if not (TMDB_TOKEN or TMDB_KEY):
        return jsonify({"error": "TMDB kaliti sozlanmagan (Railway Variables: TMDB_TOKEN)"}), 400
    raw = (d.get("q") or "").strip()
    if not raw:
        return jsonify({"results": []})

    # Qidiruvni tozalash: "83-qism", "2-fasl", "(2017)", "o'zbek tilida", "tarjima", "HD" va h.k. olib tashlanadi
    q = raw.lower()
    q = re.sub(r"\(?\b(19|20)\d{2}\b\)?", " ", q)                       # yil
    q = re.sub(r"\d+\s*[-–]?\s*(qism|qism|fasl|seriya|sezon|episode|ep|part)\b", " ", q)
    q = re.sub(r"\b(qism|fasl|seriya|sezon|barcha qismlar|to'liq|to liq|premyera|treyler|trailer)\b", " ", q)
    q = re.sub(r"\b(o'?zbek(cha)?|uzbek(cha)?|tilida|tarjima|tarjimasi|hd|full\s*hd|720p?|1080p?|4k|kino|serial|anime|multfilm)\b", " ", q)
    q = re.sub(r"[._]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    query = q if len(q) >= 2 else raw

    def _do_search(term):
        data = _tmdb_get("/search/multi", {"query": term, "language": "ru-RU", "include_adult": "false"})
        out = []
        for it in data.get("results", [])[:12]:
            mt = it.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            title = it.get("title") or it.get("name") or ""
            orig_title = it.get("original_title") or it.get("original_name") or ""
            date = it.get("release_date") or it.get("first_air_date") or ""
            poster = it.get("poster_path")
            genres = [_TMDB_GENRES.get(g, "") for g in it.get("genre_ids", [])]
            out.append({
                "title": title,
                "original_title": orig_title if orig_title != title else "",
                "year": date[:4] if date else "",
                "type": "series" if mt == "tv" else "movie",
                "poster_url": f"/api/timg/w500{poster}" if poster else "",
                "genre": ", ".join([g for g in genres if g]),
                "description": (it.get("overview") or "")[:500],
                "rating": round(it.get("vote_average") or 0, 1),
                "tmdb_id": it.get("id"),
                "media_type": mt,
            })
        return out

    try:
        _tmdb_load_genres()
        results = _do_search(query)
        # tozalangan so'rov natija bermasa, asl so'rov bilan ham urinib ko'ramiz
        if not results and query != raw:
            results = _do_search(raw)
        # birinchi so'z bilan ham urinib ko'ramiz (masalan ko'p so'zli nomda)
        if not results and " " in query:
            results = _do_search(query.split()[0])
        return jsonify({"results": results, "searched": query})
    except requests.HTTPError as he:
        code = getattr(he.response, "status_code", 0)
        if code in (401, 403):
            return jsonify({"error": "TMDB token noto'g'ri yoki eskirgan (Railway Variables: TMDB_TOKEN)"}), 400
        return jsonify({"error": f"TMDB xatosi: {code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── TMDB treyler (YouTube) — TMDB id bo'yicha ──
@app.route("/api/admin/tmdb-trailer", methods=["POST"])
def admin_tmdb_trailer():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    if not (TMDB_TOKEN or TMDB_KEY):
        return jsonify({"trailer": ""})
    tmdb_id = d.get("tmdb_id")
    media = "tv" if d.get("media_type") == "tv" else "movie"
    if not tmdb_id:
        return jsonify({"trailer": ""})
    try:
        # avval asl tilda (treylerlar ko'pincha en), keyin umumiy
        key = ""
        for lang in ("en-US", None):
            params = {} if lang is None else {"language": lang}
            data = _tmdb_get(f"/{media}/{int(tmdb_id)}/videos", params)
            vids = data.get("results", [])
            # YouTube + Trailer ustuvor
            best = None
            for v in vids:
                if v.get("site") != "YouTube":
                    continue
                if v.get("type") == "Trailer":
                    best = v
                    break
                if best is None and v.get("type") in ("Teaser", "Clip"):
                    best = v
            if best:
                key = best.get("key", "")
                break
        return jsonify({"trailer": key})
    except Exception as e:
        log.warning("tmdb-trailer: %s", e)
        return jsonify({"trailer": ""})

# ── TMDB to'liq ma'lumot — rejissyor, aktyorlar, davlat, davomiylik, yosh chegarasi ──
@app.route("/api/admin/tmdb-details", methods=["POST"])
def admin_tmdb_details():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    empty = {"director": "", "actors": "", "country": "", "duration": "", "age_rating": ""}
    if not (TMDB_TOKEN or TMDB_KEY):
        return jsonify(empty)
    tmdb_id = d.get("tmdb_id")
    media = "tv" if d.get("media_type") == "tv" else "movie"
    if not tmdb_id:
        return jsonify(empty)
    try:
        append = "credits,release_dates" if media == "movie" else "credits,content_ratings"
        data = _tmdb_get(f"/{media}/{int(tmdb_id)}", {"language": "ru-RU", "append_to_response": append})

        # Rejissyor / director yoki serial ijodkori
        crew = (data.get("credits") or {}).get("crew", [])
        if media == "movie":
            director = ", ".join([c.get("name", "") for c in crew if c.get("job") == "Director"][:2])
        else:
            director = ", ".join([c.get("name", "") for c in data.get("created_by", [])][:2])

        # Bosh aktyorlar (birinchi 5 tasi)
        cast = (data.get("credits") or {}).get("cast", [])
        actors = ", ".join([c.get("name", "") for c in cast[:5] if c.get("name")])

        # Davlat
        countries = data.get("production_countries") or []
        if not countries and data.get("origin_country"):
            countries = [{"name": c} for c in data.get("origin_country", [])]
        country = ", ".join([c.get("name", "") for c in countries[:2] if c.get("name")])

        # Davomiyligi (daqiqa)
        if media == "movie":
            duration = data.get("runtime") or ""
        else:
            rt = data.get("episode_run_time") or []
            duration = rt[0] if rt else ""

        # Yosh chegarasi
        age_rating = ""
        if media == "movie":
            for c in (data.get("release_dates") or {}).get("results", []):
                if c.get("iso_3166_1") in ("RU", "US"):
                    rds = c.get("release_dates", [])
                    if rds and rds[0].get("certification"):
                        age_rating = rds[0]["certification"]
                        if c.get("iso_3166_1") == "US":
                            break
        else:
            for c in (data.get("content_ratings") or {}).get("results", []):
                if c.get("iso_3166_1") in ("RU", "US") and c.get("rating"):
                    age_rating = c["rating"]
                    if c.get("iso_3166_1") == "US":
                        break

        return jsonify({
            "director": director, "actors": actors, "country": country,
            "duration": duration, "age_rating": age_rating,
        })
    except Exception as e:
        log.warning("tmdb-details: %s", e)
        return jsonify(empty)

# ── TMDB rasm proksi (image.tmdb.org bloklangan bo'lsa, server orqali uzatadi) ──
@app.route("/api/timg/<size>/<path:fname>")
def tmdb_img(size, fname):
    if size not in ("w200", "w300", "w500", "w780", "original"):
        size = "w500"
    if not re.match(r"^[A-Za-z0-9._/-]+\.(jpg|jpeg|png|webp)$", fname):
        return Response("bad request", status=400)
    try:
        r = requests.get(f"https://image.tmdb.org/t/p/{size}/{fname}", timeout=15)
        if r.status_code != 200:
            return Response("not found", status=404)
        resp = Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
        resp.headers["Cache-Control"] = "public, max-age=2592000"
        return resp
    except Exception:
        return Response("error", status=502)

@app.route("/api/admin/list", methods=["POST"])
def admin_list():
    """Admin uchun kinolar ro'yxati (qidiruv bilan) — boshqarish uchun."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    q = (d.get("q") or "").strip()
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if q:
                cur.execute("""
                    SELECT id, title, genre, year, language, quality,
                           COALESCE(content_type,'movie'), COALESCE(views,0),
                           COALESCE(poster_url,''),
                           CASE WHEN poster_id IS NOT NULL AND poster_id != '' THEN 1 ELSE 0 END,
                           COALESCE(is_premium, FALSE)
                    FROM movies WHERE title ILIKE %s
                    ORDER BY created_at DESC LIMIT 200
                """, (f"%{q}%",))
            else:
                cur.execute("""
                    SELECT id, title, genre, year, language, quality,
                           COALESCE(content_type,'movie'), COALESCE(views,0),
                           COALESCE(poster_url,''),
                           CASE WHEN poster_id IS NOT NULL AND poster_id != '' THEN 1 ELSE 0 END,
                           COALESCE(is_premium, FALSE)
                    FROM movies ORDER BY created_at DESC LIMIT 200
                """)
            rows = cur.fetchall()
        movies = [{
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "type": r[6], "views": r[7],
            "poster_url": r[8] or "", "has_poster": bool(r[9]), "is_premium": bool(r[10]),
        } for r in rows]
        return jsonify({"movies": movies})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/premium/toggle", methods=["POST"])
def admin_premium_toggle():
    """Kino uchun premium belgisini yoqadi/o'chiradi (bitta bosish bilan, to'liq tahrirlashsiz).
    Body: {password, id, is_premium: true/false}"""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        mid = int(d.get("id"))
    except Exception:
        return jsonify({"error": "id kerak"}), 400
    val = bool(d.get("is_premium"))
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE movies SET is_premium=%s WHERE id=%s", (val, mid))
            conn.commit()
        _log_admin("premium_toggle", mid, "yoqildi" if val else "o'chirildi")
        return jsonify({"ok": True, "is_premium": val})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/edit", methods=["POST"])
def admin_edit():
    """Mavjud kinoni tahrirlash (file_id o'zgartirilmaydi — video botda)."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    mid = d.get("id")
    title = (d.get("title") or "").strip()
    if not mid or not title:
        return jsonify({"error": "ID va nom kerak"}), 400
    try:
        year = int(d.get("year")) if d.get("year") else None
    except Exception:
        year = None
    try:
        duration = int(d.get("duration")) if d.get("duration") else None
    except Exception:
        duration = None
    try:
        tmdb_rating = float(d.get("tmdb_rating")) if d.get("tmdb_rating") else None
    except Exception:
        tmdb_rating = None
    # Treyler: YouTube havola yoki ID → faqat ID saqlaymiz (toza)
    trailer_id = _yt_id(d.get("trailer") or "")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE movies SET title=%s, genre=%s, year=%s, language=%s,
                       quality=%s, content_type=%s, description=%s, poster_url=%s, trailer=%s,
                       original_title=%s, director=%s, actors=%s, country=%s, duration=%s, age_rating=%s,
                       tmdb_rating=%s
                WHERE id=%s
            """, (title, (d.get("genre") or "").strip(), year,
                  (d.get("language") or "").strip(), (d.get("quality") or "").strip(),
                  (d.get("content_type") or "movie").strip(),
                  (d.get("description") or "").strip(),
                  (d.get("poster_url") or "").strip(), trailer_id,
                  (d.get("original_title") or "").strip(),
                  (d.get("director") or "").strip(),
                  (d.get("actors") or "").strip(),
                  (d.get("country") or "").strip(),
                  duration, (d.get("age_rating") or "").strip(), tmdb_rating,
                  int(mid)))
            conn.commit()
        _log_admin("edit_movie", mid, title)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/get", methods=["POST"])
def admin_get():
    """Tahrirlash uchun bitta kinoning to'liq ma'lumoti."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, genre, year, language, quality,
                       COALESCE(content_type,'movie'), description, poster_url, trailer,
                       original_title, director, actors, country, duration, age_rating, tmdb_rating
                FROM movies WHERE id=%s
            """, (int(d.get("id")),))
            r = cur.fetchone()
        if not r:
            return jsonify({"error": "Topilmadi"}), 404
        return jsonify({"movie": {
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3] or "",
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "description": r[7] or "", "poster_url": r[8] or "", "trailer": r[9] or "",
            "original_title": r[10] or "", "director": r[11] or "", "actors": r[12] or "",
            "country": r[13] or "", "duration": r[14] or "", "age_rating": r[15] or "",
            "tmdb_rating": float(r[16]) if r[16] else "",
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/delete", methods=["POST"])
def admin_delete():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        mid = int(d.get("id"))
        title = ""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM movies WHERE id=%s", (mid,))
            r = cur.fetchone()
            title = r[0] if r else ""
            cur.execute("DELETE FROM movies WHERE id=%s", (mid,))
            conn.commit()
        _log_admin("delete_movie", mid, title)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Til versiyalarini bog'lash (lang_group) ────────────────────────────────
@app.route("/api/admin/lang/link", methods=["POST"])
def admin_lang_link():
    """Ikkita mavjud kino yozuvini bir xil lang_group'ga bog'laydi —
    shu orqali ular saytda bitta kinoning turli til variantlari sifatida ko'rinadi.
    Body: {password, id, other_id}"""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        mid = int(d.get("id"))
        other_id = int(d.get("other_id"))
    except Exception:
        return jsonify({"error": "id va other_id kerak"}), 400
    if mid == other_id:
        return jsonify({"error": "Bir xil kinoni o'ziga bog'lab bo'lmaydi"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT lang_group, title FROM movies WHERE id=%s", (mid,))
            r1 = cur.fetchone()
            cur.execute("SELECT lang_group, title FROM movies WHERE id=%s", (other_id,))
            r2 = cur.fetchone()
            if not r1 or not r2:
                return jsonify({"error": "Kino(lar) topilmadi"}), 404
            group = r1[0] or ("m" + str(mid))
            # other_id shu guruhga qo'shiladi — uning eski guruhidagi boshqa a'zolar
            # (bo'lsa) ortda qolib ketmasligi uchun, avval o'sha guruhdagi barchani ko'chiramiz.
            old_group = r2[0] or ("m" + str(other_id))
            cur.execute("UPDATE movies SET lang_group=%s WHERE lang_group=%s OR id=%s",
                        (group, old_group, other_id))
            cur.execute("UPDATE movies SET lang_group=%s WHERE id=%s", (group, mid))
            conn.commit()
        _log_admin("lang_link", mid, f"{r1[1]} <-> {r2[1]} ({other_id})")
        return jsonify({"ok": True, "lang_group": group})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/lang/unlink", methods=["POST"])
def admin_lang_unlink():
    """Kinoni o'z lang_group'idan chiqarib, alohida (o'z ID'siga teng) guruhga qaytaradi.
    Body: {password, id}"""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        mid = int(d.get("id"))
    except Exception:
        return jsonify({"error": "id kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE movies SET lang_group=%s WHERE id=%s", ("m" + str(mid), mid))
            conn.commit()
        _log_admin("lang_unlink", mid, "")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

try:
    init_db()
except Exception as _e:
    log.warning("init_db (import): %s", _e)

if __name__ == "__main__":
    init_db()
    log.info("Kino sayti ishga tushdi, port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
