"""
KINO KATALOG sayti (backend).
Botning kino bazasidan (PostgreSQL) o'qiydi. Video botda qoladi (file_id).
Sayt: chiroyli katalog ko'rsatadi, "Botda ko'rish" → botga yo'naltiradi.
Poster: bot orqali Telegram file_id'dan proxy qilinadi.
"""
import os
import logging
import io
from urllib.parse import urlparse, unquote
import pg8000.dbapi
import requests
from flask import Flask, request, jsonify, send_from_directory, Response, redirect

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kino")

DATABASE_URL   = os.getenv("DATABASE_URL", "")
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_USERNAME   = os.getenv("BOT_USERNAME", "")          # botga yo'naltirish uchun
ADMIN_PASSWORD = os.getenv("KINO_ADMIN_PASSWORD", "admin123")
TMDB_TOKEN     = os.getenv("TMDB_TOKEN", "")   # TMDB v4 "Read Access Token" (Bearer)
TMDB_KEY       = os.getenv("TMDB_KEY", "")     # TMDB v3 API key (zaxira)
PORT           = int(os.getenv("PORT", "8080"))

app = Flask(__name__, static_folder="static")

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
            # Fon effektlari sozlamalari uchun kalit-qiymat jadvali
            cur.execute("""
                CREATE TABLE IF NOT EXISTS site_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.commit()
        log.info("Kino baza tayyor (poster_url + site_settings)")
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
        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()][:60]
            if id_list:
                ph = ",".join(["%s"] * len(id_list))
                where.append(f"id IN ({ph})")
                params += id_list
        order = {
            "new": "created_at DESC",
            "old": "created_at ASC",
            "popular": "COALESCE(views,0) DESC",
            "rating": "rating DESC NULLS LAST",
            "title": "title ASC",
        }.get(sort, "created_at DESC")
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
                       COALESCE(views,0), rating, poster_url
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
        }})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 500

# ── Poster proxy (Telegram file_id → rasm) ────────────────────────────────────
@app.route("/api/poster/<int:mid>")
def api_poster(mid):
    """Telegram'dagi poster_id rasmni web uchun proxy qiladi."""
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
        # Telegram'dan file path olamiz
        fr = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                          params={"file_id": r[0]}, timeout=10).json()
        if not fr.get("ok"):
            log.warning("Poster getFile xato (id=%s): %s", mid, fr.get("description", fr))
            return redirect("/static/no-poster.svg")
        fpath = fr["result"]["file_path"]
        img = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}", timeout=15)
        if img.status_code != 200:
            log.warning("Poster yuklab bo'lmadi (id=%s): status %s", mid, img.status_code)
            return redirect("/static/no-poster.svg")
        ct = img.headers.get("Content-Type", "image/jpeg")
        return Response(img.content, mimetype=ct,
                        headers={"Cache-Control": "public, max-age=86400"})
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

# ══════════════════ SEO (Google uchun) ══════════════════
import html as _html

@app.route("/robots.txt")
def robots():
    base = request.url_root.rstrip("/")
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
    base = request.url_root.rstrip("/")
    urls = [f"{base}/"]
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM movies ORDER BY created_at DESC LIMIT 2000")
            for row in cur.fetchall():
                urls.append(f"{base}/kino/{row[0]}")
    except Exception as e:
        log.warning("sitemap: %s", e)
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
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
                       COALESCE(content_type,'movie'), poster_id, poster_url
                FROM movies WHERE id=%s
            """, (mid,))
            r = cur.fetchone()
    except Exception:
        r = None
    if not r:
        return send_from_directory("static", "index.html")
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
    canonical = f"{request.url_root.rstrip('/')}/kino/{mid}"
    abs_poster = poster if poster.startswith("http") else (f"{request.url_root.rstrip('/')}{poster}" if poster else "")
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
    jsonld = _json.dumps(ld, ensure_ascii=False)
    page = f"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<meta name="google-site-verification" content="NWyfq_vRf53C8JMiGFZ8xL666JbpZg4NJAfKzabPoik" />
<title>{e(page_title)}</title>
<meta name="description" content="{e(desc)}">
<meta name="keywords" content="{e(title)}, {e(genre)}, o'zbek tilida, uzbek tilida, {year}, onlayn kino, tarjima">
<link rel="canonical" href="{e(request.url_root.rstrip('/'))}/kino/{mid}">
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
      <p style="line-height:1.7; color:#c8c8c8; margin:16px 0;">{e(desc)}</p>
      <a href="{e(bot_link)}" style="display:inline-block; background:#e50914; color:#fff; padding:14px 28px; border-radius:6px; text-decoration:none; font-weight:600;">▶ Botda ko'rish / yuklab olish</a>
      <p style="margin-top:24px;"><a href="/" style="color:#a3a3a3;">← Barcha kinolar</a></p>
    </div>
  </article>
</main>
</body>
</html>"""
    return Response(page, mimetype="text/html")

# ══════════════════ ADMIN ══════════════════
def _check(d):
    return (d.get("password") or "") == ADMIN_PASSWORD

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    return jsonify({"ok": _check(request.get_json() or {})})

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
    query = (d.get("q") or "").strip()
    if not query:
        return jsonify({"results": []})
    try:
        _tmdb_load_genres()
        data = _tmdb_get("/search/multi", {"query": query, "language": "ru-RU", "include_adult": "false"})
        results = []
        for it in data.get("results", [])[:12]:
            mt = it.get("media_type")
            if mt not in ("movie", "tv"):
                continue
            title = it.get("title") or it.get("name") or ""
            date = it.get("release_date") or it.get("first_air_date") or ""
            year = date[:4] if date else ""
            poster = it.get("poster_path")
            poster_url = f"https://image.tmdb.org/t/p/w500{poster}" if poster else ""
            genres = [_TMDB_GENRES.get(g, "") for g in it.get("genre_ids", [])]
            genres = [g for g in genres if g]
            results.append({
                "title": title,
                "year": year,
                "type": "series" if mt == "tv" else "movie",
                "poster_url": poster_url,
                "genre": ", ".join(genres),
                "description": (it.get("overview") or "")[:500],
                "rating": round(it.get("vote_average") or 0, 1),
            })
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE movies SET title=%s, genre=%s, year=%s, language=%s,
                       quality=%s, content_type=%s, description=%s, poster_url=%s
                WHERE id=%s
            """, (title, (d.get("genre") or "").strip(), year,
                  (d.get("language") or "").strip(), (d.get("quality") or "").strip(),
                  (d.get("content_type") or "movie").strip(),
                  (d.get("description") or "").strip(),
                  (d.get("poster_url") or "").strip(), int(mid)))
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
                       COALESCE(content_type,'movie'), description, poster_url
                FROM movies WHERE id=%s
            """, (int(d.get("id")),))
            r = cur.fetchone()
        if not r:
            return jsonify({"error": "Topilmadi"}), 404
        return jsonify({"movie": {
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3] or "",
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "description": r[7] or "", "poster_url": r[8] or "",
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
