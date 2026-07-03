"""
models.py — Modelos do banco de dados (SQLAlchemy + SQLite)
"""

from datetime import datetime
from extensions import db


# ──────────────────────────────────────────────
#  CATEGORIA
# ──────────────────────────────────────────────
class Categoria(db.Model):
    __tablename__ = "categorias"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False, unique=True)
    tipo = db.Column(db.String(10), nullable=False)   # "entrada" | "saida"
    icone = db.Column(db.String(10), default="💰")
    cor = db.Column(db.String(20), default="#6ee7b7")  # cor hex para destaque visual

    transacoes = db.relationship("Transacao", backref="categoria", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "nome": self.nome,
            "tipo": self.tipo,
            "icone": self.icone,
            "cor": self.cor,
        }


# ──────────────────────────────────────────────
#  TRANSAÇÃO
# ──────────────────────────────────────────────
class Transacao(db.Model):
    __tablename__ = "transacoes"

    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    data = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    tipo = db.Column(db.String(10), nullable=False)   # "entrada" | "saida"
    categoria_id = db.Column(db.Integer, db.ForeignKey("categorias.id"), nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "descricao": self.descricao,
            "valor": self.valor,
            "data": self.data.strftime("%Y-%m-%d"),
            "tipo": self.tipo,
            "categoria_id": self.categoria_id,
            "categoria_nome": self.categoria.nome if self.categoria else "—",
            "categoria_icone": self.categoria.icone if self.categoria else "💰",
            "categoria_cor": self.categoria.cor if self.categoria else "#6ee7b7",
        }


# ──────────────────────────────────────────────
#  META MENSAL
# ──────────────────────────────────────────────
class Meta(db.Model):
    __tablename__ = "metas"

    id = db.Column(db.Integer, primary_key=True)
    mes = db.Column(db.Integer, nullable=False)    # 1–12
    ano = db.Column(db.Integer, nullable=False)
    limite_gastos = db.Column(db.Float, nullable=False, default=0.0)
    meta_receita = db.Column(db.Float, nullable=False, default=0.0)

    __table_args__ = (
        db.UniqueConstraint("mes", "ano", name="uq_meta_mes_ano"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "mes": self.mes,
            "ano": self.ano,
            "limite_gastos": self.limite_gastos,
            "meta_receita": self.meta_receita,
        }
