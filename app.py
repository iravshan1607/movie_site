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
from urllib.parse import urlparse, unquote
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
ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID", "")    # admin Telegram ID(lar), vergul bilan — yangi so'rov xabari uchun
REVIEW_COOLDOWN  = int(os.getenv("REVIEW_COOLDOWN", "15"))    # izohlar orasidagi minimal vaqt (sekund)
REQUEST_COOLDOWN = int(os.getenv("REQUEST_COOLDOWN", "30"))   # kino so'rovlari orasidagi minimal vaqt (sekund)
ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID", "")   # admin(lar) telegram ID — yangi so'rov xabari uchun (vergul bilan bir nechta)
REVIEW_COOLDOWN  = int(os.getenv("REVIEW_COOLDOWN", "15"))    # soniya — izoh/javob orasidagi minimal vaqt
REQUEST_COOLDOWN = int(os.getenv("REQUEST_COOLDOWN", "60"))   # soniya — yangi kino so'rovi orasidagi minimal vaqt
TMDB_TOKEN     = os.getenv("TMDB_TOKEN", "")   # TMDB v4 "Read Access Token" (Bearer)
TMDB_KEY       = os.getenv("TMDB_KEY", "")     # TMDB v3 API key (zaxira)
PORT           = int(os.getenv("PORT", "8080"))
BASE_URL       = os.getenv("BASE_URL", "https://astramovie.com").rstrip("/")

app = Flask(__name__, static_folder="static")
# Sessiya imzosi uchun maxfiy kalit (SECRET_KEY bo'lmasa BOT_TOKEN'dan barqaror hosil qilinadi)
app.secret_key = os.getenv("SECRET_KEY") or hashlib.sha256(
    (BOT_TOKEN or "astra-fallback-secret").encode()).hexdigest()

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
    # poster_url ustuni — web uchun tashqi rasm havolasi (botdagi poster_id'dan mustaqil)
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS poster_url TEXT")
            cur.execute("ALTER TABLE movies ADD COLUMN IF NOT EXISTS trailer TEXT")
            # Fon effektlari sozlamalari uchun kalit-qiymat jadvali
            cur.execute("""
                CREATE TABLE IF NOT EXISTS site_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # Sevimlilar — bot bilan umumiy jadval (telegram user_id bo'yicha)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id BIGINT NOT NULL,
                    item_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    title TEXT,
                    extra TEXT,
                    added_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (user_id, item_type, item_id)
                )
            """)
            # Kino izohlari (fikr bildirish) — telegram foydalanuvchi bo'yicha
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id SERIAL PRIMARY KEY,
                    movie_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    user_name TEXT,
                    user_photo TEXT,
                    rating SMALLINT,
                    text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_movie ON reviews(movie_id)")
            # Izohga javob (thread) uchun — parent_id (NULL bo'lsa — asosiy izoh)
            cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS parent_id BIGINT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_parent ON reviews(parent_id)")
            # "Tez orada" — hali qo'shilmagan, lekin so'ralgan/kutilayotgan kinolar
            cur.execute("""
                CREATE TABLE IF NOT EXISTS upcoming (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    note TEXT,
                    poster_url TEXT,
                    status TEXT NOT NULL DEFAULT 'soon',  -- pending | soon | released
                    movie_id BIGINT,                       -- qo'shilgach bog'lanadigan kino
                    created_by BIGINT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    released_at TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_upcoming_status ON upcoming(status)")
            # "Tez orada" ga obuna — kino qo'shilganda kimga xabar berish kerakligi
            cur.execute("""
                CREATE TABLE IF NOT EXISTS upcoming_subs (
                    upcoming_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    user_name TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (upcoming_id, user_id)
                )
            """)
            # Saytdagi bildirishnomalar (qo'ng'iroq) — javoblar va chiqqan kinolar
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    type TEXT NOT NULL,          -- reply | release
                    text TEXT NOT NULL,
                    movie_id BIGINT,
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read)")
            conn.commit()
        log.info("Kino baza tayyor (poster_url + site_settings + favorites + reviews + upcoming)")
    except Exception as e:
        log.warning("init_db: %s", e)

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
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

# ── Kinolar ro'yxati (filtr/qidiruv bilan) ───────────────────────────────────
@app.route("/api/movies")
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
            where.append("(title ILIKE %s OR description ILIKE %s)")
            params += [f"%{q}%", f"%{q}%"]
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
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM movies{wsql}", params)
            total = cur.fetchone()[0]
            cur.execute(f"""
                SELECT id, title, genre, year, language, quality,
                       COALESCE(content_type,'movie'), poster_id,
                       COALESCE(views,0), rating, poster_url
                FROM movies{wsql}
                ORDER BY {order}
                LIMIT %s OFFSET %s
            """, params + [per, offset])
            rows = cur.fetchall()
        movies = [{
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "has_poster": bool(r[7]), "poster_url": r[10] or "",
            "views": r[8], "rating": float(r[9]) if r[9] else None,
        } for r in rows]
        return jsonify({"movies": movies, "total": total, "page": page,
                        "pages": (total + per - 1) // per})
    except Exception as e:
        log.warning("movies: %s", e)
        return jsonify({"movies": [], "total": 0, "error": str(e)})

# ── Bitta kino ──
@app.route("/api/movie/<int:mid>")
def api_movie(mid):
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, genre, year, language, quality, description,
                       COALESCE(content_type,'movie'), poster_id,
                       COALESCE(views,0), rating, poster_url, trailer
                FROM movies WHERE id=%s
            """, (mid,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE movies SET views = COALESCE(views,0)+1 WHERE id=%s", (mid,))
                conn.commit()
        if not r:
            return jsonify({"found": False}), 404
        return jsonify({"found": True, "movie": {
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "description": r[6] or "",
            "type": r[7], "has_poster": bool(r[8]), "poster_url": r[11] or "",
            "views": r[9], "rating": float(r[10]) if r[10] else None,
            "trailer": (r[12] or "") if len(r) > 12 else "",
        }})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 500

# ── Poster proxy (Telegram file_id → rasm) ────────────────────────────────────
@app.route("/api/poster/<int:mid>")
def api_poster(mid):
    """Telegram'dagi poster_id rasmni web uchun proxy qiladi (server keshi bilan)."""
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
        # Server keshidan (xotirada) — Telegram'ga qaytadan bormaymiz
        hit = _poster_cache.get(pid)
        if hit and (now - hit[2] < _POSTER_TTL):
            return Response(hit[0], mimetype=hit[1],
                            headers={"Cache-Control": "public, max-age=604800"})
        # Telegram'dan file path olamiz
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
        # Keshga saqlaymiz (hajmni cheklab)
        try:
            with _poster_lock:
                if len(_poster_cache) >= _POSTER_MAX:
                    oldest = min(_poster_cache, key=lambda k: _poster_cache[k][2])
                    _poster_cache.pop(oldest, None)
                _poster_cache[pid] = (img.content, ct, now)
        except Exception:
            pass
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

@app.route("/api/tg-login", methods=["POST"])
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
            # Asosiy izoh o'chsa, unga yozilgan javoblar ham o'chadi (thread tugaydi)
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

# ══════════════════ SEO (Google uchun) ══════════════════
import html as _html

@app.route("/robots.txt")
def robots():
    base = BASE_URL
    txt = f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"
    return Response(txt, mimetype="text/plain")

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
                       COALESCE(content_type,'movie'), poster_id, poster_url, trailer
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
    desc = (r[6] or f"{title} — o'zbek tilida onlayn ko'rish.")[:300]
    ctype = r[7]
    poster = r[9] or (f"/api/poster/{mid}" if r[8] else "")
    bot_link = f"https://t.me/{BOT_USERNAME}?start=movie_{mid}" if BOT_USERNAME else "#"
    e = _html.escape
    type_uz = {"movie":"Kino","series":"Serial","anime":"Anime","cartoon":"Multfilm"}.get(ctype,"Kino")
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
        '<section style="padding:10px 20px 48px;">'
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
    # Ulashish uchun JS-xavfsiz qiymatlar (kerakli qo'shtirnoq/maxsus belgilar ekranlanadi)
    share_url_js = _json.dumps(canonical)
    share_title_js = _json.dumps(title)

    # Treyler bloki — bosilganda yuklanadi (sahifa tez ochilishi uchun iframe darrov yuklanmaydi)
    if trailer_id:
        poster_bg = abs_poster or yt_thumb
        trailer_html = (
            '<section style="padding:8px 20px 4px;">'
            '<h2 style="font-family:Bebas Neue,sans-serif;font-size:24px;letter-spacing:1px;margin:0 0 12px;">🎬 Treyler</h2>'
            f'<div class="trailer-box" data-yt="{trailer_id}" onclick="loadTrailer(this)" '
            'style="position:relative;max-width:760px;aspect-ratio:16/9;border-radius:12px;overflow:hidden;cursor:pointer;'
            f'background:#000 center/cover no-repeat url(&quot;{e(poster_bg)}&quot;);">'
            '<div style="position:absolute;inset:0;background:rgba(0,0,0,0.35);"></div>'
            '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;">'
            '<div style="width:68px;height:68px;border-radius:50%;background:rgba(229,9,20,0.92);display:flex;'
            'align-items:center;justify-content:center;color:#fff;font-size:26px;padding-left:5px;">▶</div></div>'
            '</div>'
            f'<a href="{e(bot_link)}" style="display:inline-flex;align-items:center;gap:8px;margin-top:16px;'
            'background:#e50914;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;">'
            f'🎬 To\'liqini botda ko\'rish / yuklab olish</a>'
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
    page = f"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">
<link rel="icon" type="image/png" sizes="512x512" href="/static/icon-512.png">
<link rel="shortcut icon" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<meta name="google-site-verification" content="NWyfq_vRf53C8JMiGFZ8xL666JbpZg4NJAfKzabPoik" />
<title>{e(page_title)}</title>
<meta name="description" content="{e(desc)}">
<meta name="keywords" content="{e(title)}, {e(genre)}, o'zbek tilida, uzbek tilida, {year}, onlayn kino, tarjima">
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
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<nav id="navbar"><a href="/" class="nav-logo" aria-label="ASTRA"><img src="/static/logo.svg" alt="ASTRA" class="nav-logo-img"></a></nav>
<main style="padding-top:90px; max-width:900px; margin:0 auto;">
  <article style="display:flex; gap:24px; flex-wrap:wrap; padding:20px;">
    {f'<img src="{e(poster)}" alt="{e(title)}" style="width:220px; border-radius:10px;">' if poster else ''}
    <div style="flex:1; min-width:260px;">
      <h1 style="font-family:Bebas Neue,sans-serif; font-size:40px; letter-spacing:1px;">{e(title)}</h1>
      <p style="color:#a3a3a3; margin:8px 0;">{type_uz}{f' · {year}' if year else ''}{f' · {e(genre)}' if genre else ''}</p>
      {rating_html}
      <p style="line-height:1.7; color:#c8c8c8; margin:16px 0;">{e(desc)}</p>
      <div style="background:rgba(42,171,238,0.12); border:1px solid rgba(42,171,238,0.45); border-radius:10px; padding:14px 16px; margin:18px 0; color:#d6ecff; font-size:14.5px; line-height:1.65;">
        <b>ℹ️ Eslatma:</b> Ushbu {type_uz.lower()} <b>Telegram bot</b> orqali ko'riladi. Quyidagi tugmani bossangiz, Telegram botimizga o'tasiz va u yerda bemalol tomosha qilasiz yoki yuklab olasiz — tez, bepul va ro'yxatdan o'tmasdan.
      </div>
      <a href="{e(bot_link)}" style="display:inline-block; background:#229ed9; color:#fff; padding:14px 28px; border-radius:8px; text-decoration:none; font-weight:600;">▶ Telegram botda ko'rish</a>
      <button onclick="astraShare()" style="display:inline-flex; align-items:center; gap:8px; margin-left:10px; background:rgba(124,92,255,0.15); color:#fff; padding:14px 24px; border:1px solid rgba(124,92,255,0.5); border-radius:8px; font-weight:600; font-size:15px; font-family:inherit; cursor:pointer;">
        <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>
        Do'stga yuborish
      </button>
      <span id="astraShareMsg" style="display:none; margin-left:10px; color:#67e08a; font-size:14px;">✅ Havola nusxa olindi!</span>
      <p style="margin-top:24px;"><a href="/" style="color:#a3a3a3;">← Barcha kinolar</a></p>
    </div>
  </article>
  {trailer_html}
  {more_html}
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
</body>
</html>"""
    return Response(page, mimetype="text/html")

# ══════════════════ ADMIN ══════════════════
def _check(d):
    # Sessiyada admin bo'lsa — parol shart emas; aks holda parol orqali (zaxira)
    if session.get("is_admin"):
        return True
    return ((d or {}).get("password") or "") == ADMIN_PASSWORD

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    ok = ((request.get_json() or {}).get("password") or "") == ADMIN_PASSWORD
    if ok:
        session.permanent = True
        session["is_admin"] = True
    return jsonify({"ok": ok})

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return jsonify({"ok": True})

@app.route("/api/admin/check")
def admin_check():
    return jsonify({"admin": bool(session.get("is_admin"))})

# ── Admin statistika ──
@app.route("/api/admin/stats", methods=["POST"])
def admin_stats():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    out = {"total": 0, "by_type": {}, "total_views": 0, "top": [], "no_poster": 0}
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), COALESCE(SUM(views),0) FROM movies")
            row = cur.fetchone(); out["total"] = row[0]; out["total_views"] = int(row[1] or 0)
            cur.execute("SELECT COALESCE(content_type,'movie'), COUNT(*) FROM movies GROUP BY 1")
            out["by_type"] = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute("""SELECT id, title, COALESCE(views,0) FROM movies
                           ORDER BY COALESCE(views,0) DESC LIMIT 5""")
            out["top"] = [{"id": r[0], "title": r[1], "views": r[2]} for r in cur.fetchall()]
            cur.execute("""SELECT COUNT(*) FROM movies
                           WHERE (poster_url IS NULL OR poster_url='')
                             AND (poster_id IS NULL OR poster_id='')""")
            out["no_poster"] = cur.fetchone()[0]
        return jsonify(out)
    except Exception as e:
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
            date = it.get("release_date") or it.get("first_air_date") or ""
            poster = it.get("poster_path")
            genres = [_TMDB_GENRES.get(g, "") for g in it.get("genre_ids", [])]
            out.append({
                "title": title,
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
                           CASE WHEN poster_id IS NOT NULL AND poster_id != '' THEN 1 ELSE 0 END
                    FROM movies WHERE title ILIKE %s
                    ORDER BY created_at DESC LIMIT 200
                """, (f"%{q}%",))
            else:
                cur.execute("""
                    SELECT id, title, genre, year, language, quality,
                           COALESCE(content_type,'movie'), COALESCE(views,0),
                           COALESCE(poster_url,''),
                           CASE WHEN poster_id IS NOT NULL AND poster_id != '' THEN 1 ELSE 0 END
                    FROM movies ORDER BY created_at DESC LIMIT 200
                """)
            rows = cur.fetchall()
        movies = [{
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "type": r[6], "views": r[7],
            "poster_url": r[8] or "", "has_poster": bool(r[9]),
        } for r in rows]
        return jsonify({"movies": movies})
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
    # Treyler: YouTube havola yoki ID → faqat ID saqlaymiz (toza)
    trailer_id = _yt_id(d.get("trailer") or "")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE movies SET title=%s, genre=%s, year=%s, language=%s,
                       quality=%s, content_type=%s, description=%s, poster_url=%s, trailer=%s
                WHERE id=%s
            """, (title, (d.get("genre") or "").strip(), year,
                  (d.get("language") or "").strip(), (d.get("quality") or "").strip(),
                  (d.get("content_type") or "movie").strip(),
                  (d.get("description") or "").strip(),
                  (d.get("poster_url") or "").strip(), trailer_id, int(mid)))
            conn.commit()
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
                       COALESCE(content_type,'movie'), description, poster_url, trailer
                FROM movies WHERE id=%s
            """, (int(d.get("id")),))
            r = cur.fetchone()
        if not r:
            return jsonify({"error": "Topilmadi"}), 404
        return jsonify({"movie": {
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3] or "",
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "description": r[7] or "", "poster_url": r[8] or "", "trailer": r[9] or "",
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/delete", methods=["POST"])
def admin_delete():
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM movies WHERE id=%s", (int(d.get("id")),))
            conn.commit()
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