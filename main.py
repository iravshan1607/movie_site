"""
Railway gunicorn 'main:app' ni qidiradi.
Bu fayl app.py dagi Flask app'ni shu nom bilan ham mavjud qiladi.
"""
from app import app  # noqa

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
