"""
extensions.py — Instância compartilhada do SQLAlchemy
(evita importações circulares)
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
