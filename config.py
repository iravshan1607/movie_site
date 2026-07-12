"""
Config moduli — barcha environment o'zgaruvchilari, global loggerи,
va ishga tushishda hosil qilinadigan maxfiy qiymatlar (SECRET_KEY,
zaxira ADMIN_PASSWORD) shu yerda joylashgan.

Bu modul app.py'dan avval import qilinishi kerak, chunki boshqa barcha
modullar (db, telegram_bot, auth) shu yerdagi sozlamalarga tayanadi.
"""
import os
import logging
import secrets as _secrets

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kino")

DATABASE_URL   = os.getenv("DATABASE_URL", "")
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_USERNAME   = os.getenv("BOT_USERNAME", "")          # botga yo'naltirish + Telegram login uchun

ADMIN_PASSWORD = os.getenv("KINO_ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    # KINO_ADMIN_PASSWORD o'rnatilmagan bo'lsa — standart "admin123" o'rniga
    # har safar ishga tushganda tasodifiy, kuchli parol generatsiya qilinadi va
    # faqat serverning o'z logiga chiqariladi. Bu "admin123" bilan production'da
    # qolib ketishning oldini oladi — parolni faqat log'ga kirish huquqi bor
    # (ya'ni admin) ko'ra oladi.
    ADMIN_PASSWORD = _secrets.token_urlsafe(12)
    log.warning("OGOHLANTIRISH: KINO_ADMIN_PASSWORD environment o'zgaruvchisi o'rnatilmagan! "
                "Vaqtinchalik tasodifiy parol generatsiya qilindi: %s — "
                "Railway'da KINO_ADMIN_PASSWORD ni o'rnating, aks holda har deploy'da parol o'zgaradi.",
                ADMIN_PASSWORD)

ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID", "")    # admin(lar) Telegram ID — yangi so'rov xabari uchun (vergul bilan bir nechta)
REVIEW_COOLDOWN  = int(os.getenv("REVIEW_COOLDOWN", "15"))    # soniya — izoh/javob orasidagi minimal vaqt
REQUEST_COOLDOWN = int(os.getenv("REQUEST_COOLDOWN", "30"))   # soniya — yangi kino so'rovi orasidagi minimal vaqt
TMDB_TOKEN     = os.getenv("TMDB_TOKEN", "")   # TMDB v4 "Read Access Token" (Bearer)
TMDB_KEY       = os.getenv("TMDB_KEY", "")     # TMDB v3 API key (zaxira)
PORT           = int(os.getenv("PORT", "8080"))
BASE_URL       = os.getenv("BASE_URL", "https://astramovie.com").rstrip("/")

# Sessiya imzosi uchun maxfiy kalit.
# Eslatma: avval BOT_TOKEN'dan hosil qilinardi — bu xavfli edi, chunki BOT_TOKEN
# sizib chiqsa session imzosi ham (ikkilamchi tarzda) buzilardi. Endi SECRET_KEY
# bo'lmasa, /app ichida saqlanadigan alohida tasodifiy fayl orqali barqaror kalit
# ishlatiladi (deploy qayta tushganda ham bir xil qoladi, lekin BOT_TOKEN'dan mustaqil).
_secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret_key")


def _load_or_create_secret():
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    try:
        if os.path.exists(_secret_path):
            with open(_secret_path, "r") as f:
                val = f.read().strip()
                if val:
                    return val
        val = _secrets.token_hex(32)
        with open(_secret_path, "w") as f:
            f.write(val)
        return val
    except Exception:
        # Fayl tizimi yozib bo'lmaydigan (masalan read-only) bo'lsa —
        # kamida process davomida barqaror bo'lsin (restart'da o'zgaradi,
        # bu esa faqat mavjud sessiyalarni bekor qiladi, xavfsizlik muammosi emas).
        return _secrets.token_hex(32)


SECRET_KEY = _load_or_create_secret()

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
