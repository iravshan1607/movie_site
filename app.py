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
BOT_USERNAME   = os.getenv("BOT_USERNAME", "@AstraUz_AIBot")          # botga yo'naltirish uchun
ADMIN_PASSWORD = os.getenv("KINO_ADMIN_PASSWORD", "admin123")
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
    # movies jadvali botda allaqachon yaratilgan — qo'shimcha hech narsa kerak emas.
    log.info("Kino baza: movies jadvaliga ulanadi")

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
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM movies{wsql}", params)
            total = cur.fetchone()[0]
            cur.execute(f"""
                SELECT id, title, genre, year, language, quality,
                       COALESCE(content_type,'movie'), poster_id,
                       COALESCE(views,0), rating
                FROM movies{wsql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, params + [per, offset])
            rows = cur.fetchall()
        movies = [{
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "has_poster": bool(r[7]), "poster_url": "",
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
                       COALESCE(views,0), rating
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
            "type": r[7], "has_poster": bool(r[8]), "poster_url": "",
            "views": r[9], "rating": float(r[10]) if r[10] else None,
        }})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 500

# ── Poster proxy (Telegram file_id → rasm) ────────────────────────────────────
@app.route("/api/poster/<int:mid>")
def api_poster(mid):
    """Telegram'dagi poster_id rasmni web uchun proxy qiladi."""
    if not BOT_TOKEN:
        return redirect("/static/no-poster.svg")
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT poster_id FROM movies WHERE id=%s", (mid,))
            r = cur.fetchone()
        if not r:
            return redirect("/static/no-poster.svg")
        if not r[0]:
            return redirect("/static/no-poster.svg")
        # Telegram'dan file path olamiz
        fr = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                          params={"file_id": r[0]}, timeout=10).json()
        if not fr.get("ok"):
            return redirect("/static/no-poster.svg")
        fpath = fr["result"]["file_path"]
        img = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}", timeout=15)
        return Response(img.content, mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return redirect("/static/no-poster.svg")

# ── Janrlar ro'yxati ──
@app.route("/api/genres")
def api_genres():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT genre FROM movies WHERE genre IS NOT NULL AND genre <> '' ORDER BY genre LIMIT 40")
            genres = [row[0] for row in cur.fetchall()]
        return jsonify({"genres": genres})
    except Exception:
        return jsonify({"genres": []})

# ── Botga yo'naltirish havolasi ──
@app.route("/api/botlink")
def api_botlink():
    return jsonify({"bot": BOT_USERNAME})

# ══════════════════ ADMIN ══════════════════
def _check(d):
    return (d.get("password") or "") == ADMIN_PASSWORD

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    return jsonify({"ok": _check(request.get_json() or {})})

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
                           COALESCE(content_type,'movie'), COALESCE(views,0)
                    FROM movies WHERE title ILIKE %s
                    ORDER BY created_at DESC LIMIT 200
                """, (f"%{q}%",))
            else:
                cur.execute("""
                    SELECT id, title, genre, year, language, quality,
                           COALESCE(content_type,'movie'), COALESCE(views,0)
                    FROM movies ORDER BY created_at DESC LIMIT 200
                """)
            rows = cur.fetchall()
        movies = [{
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3],
            "language": r[4] or "", "quality": r[5] or "", "type": r[6], "views": r[7],
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
                       quality=%s, content_type=%s, description=%s
                WHERE id=%s
            """, (title, (d.get("genre") or "").strip(), year,
                  (d.get("language") or "").strip(), (d.get("quality") or "").strip(),
                  (d.get("content_type") or "movie").strip(),
                  (d.get("description") or "").strip(), int(mid)))
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
                       COALESCE(content_type,'movie'), description, poster_id
                FROM movies WHERE id=%s
            """, (int(d.get("id")),))
            r = cur.fetchone()
        if not r:
            return jsonify({"error": "Topilmadi"}), 404
        return jsonify({"movie": {
            "id": r[0], "title": r[1], "genre": r[2] or "", "year": r[3] or "",
            "language": r[4] or "", "quality": r[5] or "", "type": r[6],
            "description": r[7] or "", "poster_id": r[8] or "",
        }})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/admin/poster", methods=["POST"])
def admin_poster():
    """Poster file_id ni yangilash."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    mid = d.get("id")
    poster_id = (d.get("poster_id") or "").strip() or None
    if not mid:
        return jsonify({"error": "ID kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE movies SET poster_id=%s WHERE id=%s", (poster_id, int(mid)))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/episodes", methods=["POST"])
def admin_episodes():
    """Serial qismlarini royxatini qaytaradi."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    movie_id = d.get("movie_id")
    if not movie_id:
        return jsonify({"error": "movie_id kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='episodes' ORDER BY ordinal_position
            """)
            cols = [row[0] for row in cur.fetchall()]
            season_col = next((c for c in cols if 'season' in c.lower()), None)
            ep_col = next((c for c in cols if 'episode' in c.lower() and 'season' not in c.lower()), None)
            title_col = next((c for c in cols if c.lower() in ('title','name','episode_title')), None)
            quality_col = next((c for c in cols if 'quality' in c.lower()), None)
            file_col = next((c for c in cols if 'file_id' in c.lower()), None)

            order_parts = []
            if season_col: order_parts.append(f"{season_col} ASC NULLS FIRST")
            if ep_col: order_parts.append(f"{ep_col} ASC NULLS FIRST")
            if not order_parts: order_parts.append("id ASC")

            sel = """
                SELECT id, {} AS season_number, {} AS episode_number,
                       {} AS title, {} AS quality, {} AS file_id
                FROM episodes WHERE movie_id=%s ORDER BY {}
            """.format(
                season_col or "NULL", ep_col or "NULL",
                title_col or "NULL", quality_col or "NULL", file_col or "NULL",
                ", ".join(order_parts)
            )
            cur.execute(sel, (int(movie_id),))
            rows = cur.fetchall()
        episodes = [{"id": r[0], "season_number": r[1], "episode_number": r[2],
                     "title": r[3] or "", "quality": r[4] or "", "file_id": bool(r[5])} for r in rows]
        return jsonify({"episodes": episodes, "columns": {"season": season_col, "episode": ep_col}})
    except Exception as e:
        log.warning("episodes: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/episode/edit", methods=["POST"])
def admin_episode_edit():
    """Qism raqami va nomini tahrirlash."""
    d = request.get_json() or {}
    if not _check(d):
        return jsonify({"error": "ruxsat yo'q"}), 403
    ep_id = d.get("id")
    ep_num = d.get("episode_number")
    season_num = d.get("season_number")
    title = (d.get("title") or "").strip() or None
    if not ep_id or not ep_num:
        return jsonify({"error": "ID va qism raqami kerak"}), 400
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='episodes' ORDER BY ordinal_position
            """)
            cols = [row[0] for row in cur.fetchall()]
            season_col = next((c for c in cols if 'season' in c.lower()), None)
            ep_col = next((c for c in cols if 'episode' in c.lower() and 'season' not in c.lower()), None)
            title_col = next((c for c in cols if c.lower() in ('title','name','episode_title')), None)

            sets = []
            params = []
            if ep_col:
                sets.append(f"{ep_col}=%s"); params.append(int(ep_num))
            if season_col and season_num is not None and str(season_num).strip():
                sets.append(f"{season_col}=%s"); params.append(int(season_num))
            if title_col:
                sets.append(f"{title_col}=%s"); params.append(title)
            if not sets:
                return jsonify({"error": "Yangilanadigan ustun topilmadi"}), 400
            params.append(int(ep_id))
            cur.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id=%s", params)
            conn.commit()
        return jsonify({"ok": True})
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
