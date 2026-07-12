"""
DB (PostgreSQL, pg8000) ulanish qatlami.

Botning `movies` jadvali allaqachon mavjud — biz faqat o'qiymiz/yozamiz.
Qolgan jadvallar (favorites, reviews, upcoming va h.k.) shu modul orqali
ishga tushirilganda (init_db) yaratiladi.
"""
from urllib.parse import urlparse, unquote
import pg8000.dbapi

from config import DATABASE_URL, log


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
        """CREATE TABLE IF NOT EXISTS admin_login_fails (
                ip TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
        "CREATE INDEX IF NOT EXISTS idx_admin_login_fails_ip ON admin_login_fails(ip, created_at)",
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
