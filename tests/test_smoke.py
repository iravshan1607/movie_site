"""
Oddiy smoke-test — CI yoki deploy oldidan qo'lda ishga tushirish uchun.

Maqsad: DATABASE_URL bo'lmagan (yoki noto'g'ri) holatda ham Flask app
xatosiz import bo'lishini va asosiy public endpointlar 500 (server xatosi)
qaytarmasligini tekshirish. Bu chuqur funksional test emas — faqat
"deploy qilingandan keyin sayt darhol qulab tushmaydi" darajasidagi tekshiruv.

Ishga tushirish:
    cd movie_site-main
    pip install pytest --break-system-packages   # agar o'rnatilmagan bo'lsa
    pytest tests/test_smoke.py -v
"""
import os
import sys

# app.py'ni import qilishdan oldin — DB bo'lmasa ham ishga tushishi kerak
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("KINO_ADMIN_PASSWORD", "test-only-password-123")
os.environ.setdefault("SECRET_KEY", "test-only-secret-key")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def test_app_imports():
    """app.py xatosiz import bo'lishi kerak (sintaksis/global darajadagi xatolar yo'q)."""
    assert flask_app is not None


def test_index_page_loads(client):
    """Bosh sahifa (static/index.html) 200 qaytarishi kerak."""
    resp = client.get("/")
    assert resp.status_code == 200


def test_admin_page_loads(client):
    """Admin sahifasi (static/admin.html) 200 qaytarishi kerak."""
    resp = client.get("/admin")
    assert resp.status_code == 200


def test_movies_api_no_crash_without_db(client):
    """DB ulanmagan holatda ham /api/movies 500 bilan qulamasligi kerak
    (ideal holda bo'sh ro'yxat yoki tushunarli xato qaytarishi kerak)."""
    resp = client.get("/api/movies")
    assert resp.status_code != 500, (
        f"/api/movies DB'siz holatda 500 qaytardi: {resp.get_data(as_text=True)[:300]}"
    )


def test_botlink_endpoint(client):
    """DB'ga bog'liq bo'lmagan oddiy endpoint ishlashi kerak."""
    resp = client.get("/api/botlink")
    assert resp.status_code == 200
    assert "bot" in resp.get_json()


def test_me_endpoint_logged_out(client):
    """Login qilinmagan holatda /api/me logged_in=False qaytarishi kerak."""
    resp = client.get("/api/me")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["logged_in"] is False


def test_admin_check_default_false(client):
    """Sessiyasiz /api/admin/check admin=False qaytarishi kerak."""
    resp = client.get("/api/admin/check")
    assert resp.status_code == 200
    assert resp.get_json()["admin"] is False


def test_admin_login_wrong_password_rejected(client):
    """Noto'g'ri parol bilan admin login rad etilishi kerak."""
    resp = client.post("/api/admin/login", json={"password": "notu'g'ri-parol"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False


def test_admin_login_correct_password_accepted(client):
    """To'g'ri parol (test muhitida KINO_ADMIN_PASSWORD) bilan kirish muvaffaqiyatli bo'lishi kerak."""
    resp = client.post("/api/admin/login", json={"password": "test-only-password-123"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_admin_api_requires_auth(client):
    """Admin API'ga parolsiz/sessiyasiz kirish 403 bilan rad etilishi kerak."""
    resp = client.post("/api/admin/stats", json={})
    assert resp.status_code == 403


def test_favorites_requires_login(client):
    """Login qilinmagan foydalanuvchi sevimlilar ro'yxatiga kira olmasligi kerak (401)."""
    resp = client.get("/api/favorites")
    assert resp.status_code == 401


def test_unknown_route_returns_404(client):
    resp = client.get("/api/bunday-endpoint-yoq")
    assert resp.status_code == 404


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))