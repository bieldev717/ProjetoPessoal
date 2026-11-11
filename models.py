"""Modelos financeiros do aplicativo."""

from datetime import date, datetime, timezone
from extensions import db


class Categoria(db.Model):
    __tablename__ = "categorias"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False, unique=True)
    tipo = db.Column(db.String(10), nullable=False)
    icone = db.Column(db.String(10), default="💰")
    cor = db.Column(db.String(20), default="#6ee7b7")
    transacoes = db.relationship("Transacao", backref="categoria", lazy=True)

    def to_dict(self):
        return {"id": self.id, "nome": self.nome, "tipo": self.tipo, "icone": self.icone, "cor": self.cor}


class Transacao(db.Model):
    __tablename__ = "transacoes"

    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    data = db.Column(db.Date, nullable=False, default=date.today)
    tipo = db.Column(db.String(10), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey("categorias.id"), nullable=True)
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id": self.id,
            "descricao": self.descricao,
            "valor": self.valor,
            "data": self.data.isoformat(),
            "tipo": self.tipo,
            "categoria_id": self.categoria_id,
            "categoria_nome": self.categoria.nome if self.categoria else "—",
            "categoria_icone": self.categoria.icone if self.categoria else "💰",
            "categoria_cor": self.categoria.cor if self.categoria else "#6ee7b7",
        }


class Meta(db.Model):
    __tablename__ = "metas"

    id = db.Column(db.Integer, primary_key=True)
    mes = db.Column(db.Integer, nullable=False)
    ano = db.Column(db.Integer, nullable=False)
    limite_gastos = db.Column(db.Float, nullable=False, default=0.0)
    meta_receita = db.Column(db.Float, nullable=False, default=0.0)
    __table_args__ = (db.UniqueConstraint("mes", "ano", name="uq_meta_mes_ano"),)

    def to_dict(self):
        return {"id": self.id, "mes": self.mes, "ano": self.ano, "limite_gastos": self.limite_gastos, "meta_receita": self.meta_receita}


class OrcamentoCategoria(db.Model):
    __tablename__ = "orcamentos_categoria"

    id = db.Column(db.Integer, primary_key=True)
    categoria_id = db.Column(db.Integer, db.ForeignKey("categorias.id"), nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    ano = db.Column(db.Integer, nullable=False)
    limite = db.Column(db.Float, nullable=False)
    categoria = db.relationship("Categoria")
    __table_args__ = (db.UniqueConstraint("categoria_id", "mes", "ano", name="uq_orcamento_categoria_mes"),)


class Recorrencia(db.Model):
    __tablename__ = "recorrencias"

    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    categoria_id = db.Column(db.Integer, db.ForeignKey("categorias.id"), nullable=True)
    frequencia = db.Column(db.String(10), nullable=False)
    proxima_data = db.Column(db.Date, nullable=False)
    ativa = db.Column(db.Boolean, nullable=False, default=True)
    categoria = db.relationship("Categoria")

    def to_dict(self):
        return {
            "id": self.id, "descricao": self.descricao, "valor": self.valor,
            "tipo": self.tipo, "categoria_id": self.categoria_id,
            "categoria_nome": self.categoria.nome if self.categoria else "—",
            "frequencia": self.frequencia, "proxima_data": self.proxima_data.isoformat(), "ativa": self.ativa,
        }
