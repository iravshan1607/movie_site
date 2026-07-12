# 🎬 AstraMovie — Onlayn Kino Katalog Sayti

**[astramovie.com](https://astramovie.com)** — O'zbek tilidagi onlayn kino, serial, anime va multfilm katalogi.

## 🛠 Texnologiyalar

- **Backend:** Python, Flask, Gunicorn
- **Database:** PostgreSQL (pg8000)
- **Deploy:** Railway
- **Telegram:** Bot integratsiyasi (file_id orqali video)
- **SEO:** Sitemap, VideoObject schema, Open Graph, robots.txt

## ⚙️ Environment Variables

| O'zgaruvchi | Tavsif |
|---|---|
| `DATABASE_URL` | PostgreSQL ulanish manzili |
| `BOT_TOKEN` | Telegram bot tokeni |
| `BOT_USERNAME` | Bot username (@ siz) |
| `KINO_ADMIN_PASSWORD` | Admin panel paroli |
| `ADMIN_CHAT_ID` | Admin(lar) Telegram ID (vergul bilan) |
| `TMDB_TOKEN` | TMDB v4 Read Access Token |
| `TMDB_KEY` | TMDB v3 API Key |
| `BASE_URL` | Sayt asosiy manzili (https://astramovie.com) |
| `SECRET_KEY` | Flask sessiya kaliti (ixtiyoriy) |
| `REVIEW_COOLDOWN` | Izoh orasidagi minimal vaqt (soniya, default: 15) |
| `REQUEST_COOLDOWN` | Kino so'rovi orasidagi minimal vaqt (soniya, default: 30) |

## 🚀 Local ishga tushirish

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."
export BOT_TOKEN="..."
python app.py
```

## ✅ Testlar

Har bir deploy'dan oldin oddiy smoke-testlarni ishga tushirish tavsiya etiladi
(DB'siz ham ishlaydi, faqat app qulab tushmasligini va asosiy auth mantiqini tekshiradi):

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

## 📁 Loyiha tuzilmasi

```
movie_site/
├── app.py              # Asosiy backend (Flask)
├── main.py             # Entry point
├── Procfile            # Railway/Gunicorn ishga tushirish
├── requirements.txt    # Python kutubxonalar (production)
├── requirements-dev.txt # + pytest (faqat lokal/CI test uchun)
├── tests/
│   └── test_smoke.py   # Deploy oldidan tekshiruv (DB'siz ham ishlaydi)
├── nixpacks.toml       # Railway build konfiguratsiyasi
├── Dockerfile          # Docker konfiguratsiyasi
└── static/
    ├── index.html      # Bosh sahifa
    ├── app.js          # Frontend logika
    ├── style.css       # Dizayn
    ├── admin.html      # Admin panel
    ├── manifest.json   # PWA manifest
    ├── sw.js           # Service Worker
    └── favicon.svg     # Sayt ikonkasi
```

## 🔍 SEO

- ✅ Google Search Console
- ✅ Yandex Webmaster
- ✅ sitemap.xml
- ✅ sitemap_video.xml
- ✅ robots.txt
- ✅ Schema.org (Movie, TVSeries, VideoObject, BreadcrumbList)
- ✅ Open Graph / Twitter Card

## 📺 Asosiy xususiyatlar

- Kino, serial, anime, multfilm katalogi
- Telegram bot orqali video ko'rish
- Poster keshi (xotirada, 6 soat)
- TMDB integratsiyasi (poster, tavsif)
- Foydalanuvchi izohlari va reytingi
- Kino so'rovi (foydalanuvchilardan)
- Admin panel
- PWA (telefonga o'rnatish)
- Telegram Login Widget