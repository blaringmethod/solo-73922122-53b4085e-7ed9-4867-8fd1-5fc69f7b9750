"""WSGI 入口：gunicorn 使用 ``wsgi:app``。"""

from app import create_app

app = create_app()
