"""
app.py — Ponto de entrada do Sistema de Gestão Financeira
Tecnologia: Flask + SQLAlchemy + SQLite

Rotas HTML (renderização de páginas):
  GET  /                   → Dashboard principal
  GET  /transacoes         → Página de CRUD de transações
  GET  /extrato            → Extrato com filtros
  GET  /metas              → Módulo de metas mensais

API JSON (/api/...):
  Transações   → GET/POST /api/transacoes
                 GET/PUT/DELETE /api/transacoes/<id>
  Categorias   → GET/POST /api/categorias
  Metas        → GET/POST /api/metas
                 PUT /api/metas/<id>
  Dashboard    → GET /api/dashboard/resumo
                 GET /api/dashboard/evolucao
"""

from flask import Flask, render_template, request, jsonify, abort
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from sqlalchemy import extract, func

from extensions import db
from models import Transacao, Categoria, Meta

# ─────────────────────────────────────────────────────────────────
#  FACTORY / CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────────

def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = "financas-secret-2024"

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _seed_categorias()

    return app


def _seed_categorias():
    """Insere categorias padrão se o banco estiver vazio."""
    if Categoria.query.count() > 0:
        return

    defaults = [
        # Entradas
        {"nome": "Salário",       "tipo": "entrada", "icone": "💼", "cor": "#6ee7b7"},
        {"nome": "Freelance",     "tipo": "entrada", "icone": "💻", "cor": "#34d399"},
        {"nome": "Investimentos", "tipo": "entrada", "icone": "📈", "cor": "#10b981"},
        {"nome": "Outros (entrada)", "tipo": "entrada", "icone": "➕", "cor": "#059669"},
        # Saídas
        {"nome": "Alimentação",   "tipo": "saida",   "icone": "🍔", "cor": "#f87171"},
        {"nome": "Transporte",    "tipo": "saida",   "icone": "🚗", "cor": "#fb923c"},
        {"nome": "Moradia",       "tipo": "saida",   "icone": "🏠", "cor": "#fbbf24"},
        {"nome": "Saúde",         "tipo": "saida",   "icone": "💊", "cor": "#a78bfa"},
        {"nome": "Lazer",         "tipo": "saida",   "icone": "🎮", "cor": "#60a5fa"},
        {"nome": "Educação",      "tipo": "saida",   "icone": "📚", "cor": "#818cf8"},
        {"nome": "Roupas",        "tipo": "saida",   "icone": "👗", "cor": "#f472b6"},
        {"nome": "Outros (saída)", "tipo": "saida",  "icone": "➖", "cor": "#94a3b8"},
    ]

    for d in defaults:
        db.session.add(Categoria(**d))
    db.session.commit()


# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────

def _resumo_mes(mes: int, ano: int) -> dict:
    """Calcula receitas, despesas e saldo para um determinado mês/ano."""
    q = (
        db.session.query(Transacao.tipo, func.sum(Transacao.valor))
        .filter(
            extract("month", Transacao.data) == mes,
            extract("year", Transacao.data) == ano,
        )
        .group_by(Transacao.tipo)
        .all()
    )

    totais = {"entrada": 0.0, "saida": 0.0}
    for tipo, total in q:
        totais[tipo] = round(total or 0, 2)

    receitas = totais["entrada"]
    despesas = totais["saida"]
    saldo = round(receitas - despesas, 2)

    # Meta do mês (se existir)
    meta = Meta.query.filter_by(mes=mes, ano=ano).first()
    limite = meta.limite_gastos if meta else 0.0
    meta_receita = meta.meta_receita if meta else 0.0

    progresso_gastos = round((despesas / limite * 100), 1) if limite > 0 else 0
    progresso_receita = round((receitas / meta_receita * 100), 1) if meta_receita > 0 else 0

    return {
        "mes": mes,
        "ano": ano,
        "receitas": receitas,
        "despesas": despesas,
        "saldo": saldo,
        "limite_gastos": limite,
        "meta_receita": meta_receita,
        "progresso_gastos": min(progresso_gastos, 100),
        "progresso_receita": min(progresso_receita, 100),
        "alerta_limite": despesas > limite > 0,
    }


# ─────────────────────────────────────────────────────────────────
#  CRIAÇÃO DA APP
# ─────────────────────────────────────────────────────────────────

app = create_app()


# ═════════════════════════════════════════════════════════════════
#  ROTAS HTML
# ═════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    hoje = date.today()
    resumo = _resumo_mes(hoje.month, hoje.year)
    categorias = Categoria.query.order_by(Categoria.tipo, Categoria.nome).all()
    return render_template(
        "dashboard.html",
        resumo=resumo,
        categorias=categorias,
        mes_atual=hoje.month,
        ano_atual=hoje.year,
    )


@app.route("/transacoes")
def transacoes_page():
    categorias = Categoria.query.order_by(Categoria.tipo, Categoria.nome).all()
    return render_template("transacoes.html", categorias=categorias)


@app.route("/extrato")
def extrato_page():
    hoje = date.today()
    categorias = Categoria.query.order_by(Categoria.nome).all()
    return render_template(
        "extrato.html",
        categorias=categorias,
        mes_atual=hoje.month,
        ano_atual=hoje.year,
    )


@app.route("/metas")
def metas_page():
    hoje = date.today()
    return render_template(
        "metas.html",
        mes_atual=hoje.month,
        ano_atual=hoje.year,
    )


# ═════════════════════════════════════════════════════════════════
#  API — DASHBOARD
# ═════════════════════════════════════════════════════════════════

@app.route("/api/dashboard/resumo")
def api_resumo():
    mes = request.args.get("mes", date.today().month, type=int)
    ano = request.args.get("ano", date.today().year, type=int)
    return jsonify(_resumo_mes(mes, ano))


@app.route("/api/dashboard/evolucao")
def api_evolucao():
    """Retorna evolução mensal dos últimos 6 meses para o gráfico."""
    hoje = date.today()
    meses = []

    for i in range(5, -1, -1):
        ref = hoje - relativedelta(months=i)
        r = _resumo_mes(ref.month, ref.year)
        meses.append({
            "label": ref.strftime("%b/%y"),
            "receitas": r["receitas"],
            "despesas": r["despesas"],
            "saldo": r["saldo"],
        })

    return jsonify(meses)


# ═════════════════════════════════════════════════════════════════
#  API — TRANSAÇÕES
# ═════════════════════════════════════════════════════════════════

@app.route("/api/transacoes", methods=["GET"])
def api_listar_transacoes():
    mes = request.args.get("mes", type=int)
    ano = request.args.get("ano", type=int)
    tipo = request.args.get("tipo")          # "entrada" | "saida"
    categoria_id = request.args.get("categoria_id", type=int)
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)

    q = Transacao.query

    if mes:
        q = q.filter(extract("month", Transacao.data) == mes)
    if ano:
        q = q.filter(extract("year", Transacao.data) == ano)
    if tipo:
        q = q.filter(Transacao.tipo == tipo)
    if categoria_id:
        q = q.filter(Transacao.categoria_id == categoria_id)

    q = q.order_by(Transacao.data.desc(), Transacao.criado_em.desc())
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "items": [t.to_dict() for t in items],
    })


@app.route("/api/transacoes", methods=["POST"])
def api_criar_transacao():
    data = request.get_json(force=True)

    try:
        nova = Transacao(
            descricao=data["descricao"].strip(),
            valor=float(data["valor"]),
            data=datetime.strptime(data["data"], "%Y-%m-%d").date(),
            tipo=data["tipo"],
            categoria_id=data.get("categoria_id") or None,
        )
        db.session.add(nova)
        db.session.commit()
        return jsonify(nova.to_dict()), 201

    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400


@app.route("/api/transacoes/<int:tid>", methods=["GET"])
def api_obter_transacao(tid):
    t = Transacao.query.get_or_404(tid)
    return jsonify(t.to_dict())


@app.route("/api/transacoes/<int:tid>", methods=["PUT"])
def api_atualizar_transacao(tid):
    t = Transacao.query.get_or_404(tid)
    data = request.get_json(force=True)

    try:
        t.descricao = data.get("descricao", t.descricao).strip()
        t.valor = float(data.get("valor", t.valor))
        t.data = datetime.strptime(data["data"], "%Y-%m-%d").date() if "data" in data else t.data
        t.tipo = data.get("tipo", t.tipo)
        t.categoria_id = data.get("categoria_id") or None

        db.session.commit()
        return jsonify(t.to_dict())

    except ValueError as e:
        return jsonify({"erro": str(e)}), 400


@app.route("/api/transacoes/<int:tid>", methods=["DELETE"])
def api_deletar_transacao(tid):
    t = Transacao.query.get_or_404(tid)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True, "id": tid})


# ═════════════════════════════════════════════════════════════════
#  API — CATEGORIAS
# ═════════════════════════════════════════════════════════════════

@app.route("/api/categorias", methods=["GET"])
def api_listar_categorias():
    tipo = request.args.get("tipo")
    q = Categoria.query.order_by(Categoria.tipo, Categoria.nome)
    if tipo:
        q = q.filter(Categoria.tipo == tipo)
    return jsonify([c.to_dict() for c in q.all()])


@app.route("/api/categorias", methods=["POST"])
def api_criar_categoria():
    data = request.get_json(force=True)
    try:
        cat = Categoria(
            nome=data["nome"].strip(),
            tipo=data["tipo"],
            icone=data.get("icone", "💰"),
            cor=data.get("cor", "#6ee7b7"),
        )
        db.session.add(cat)
        db.session.commit()
        return jsonify(cat.to_dict()), 201
    except KeyError as e:
        return jsonify({"erro": f"Campo obrigatório ausente: {e}"}), 400


# ═════════════════════════════════════════════════════════════════
#  API — METAS
# ═════════════════════════════════════════════════════════════════

@app.route("/api/metas", methods=["GET"])
def api_listar_metas():
    mes = request.args.get("mes", date.today().month, type=int)
    ano = request.args.get("ano", date.today().year, type=int)
    meta = Meta.query.filter_by(mes=mes, ano=ano).first()

    if not meta:
        return jsonify({"mes": mes, "ano": ano, "limite_gastos": 0, "meta_receita": 0})

    # Enriquece com dados reais do mês
    resumo = _resumo_mes(mes, ano)
    resultado = meta.to_dict()
    resultado.update({
        "despesas_atuais": resumo["despesas"],
        "receitas_atuais": resumo["receitas"],
        "progresso_gastos": resumo["progresso_gastos"],
        "progresso_receita": resumo["progresso_receita"],
        "alerta_limite": resumo["alerta_limite"],
    })
    return jsonify(resultado)


@app.route("/api/metas", methods=["POST"])
def api_salvar_meta():
    data = request.get_json(force=True)
    mes = int(data.get("mes", date.today().month))
    ano = int(data.get("ano", date.today().year))

    meta = Meta.query.filter_by(mes=mes, ano=ano).first()

    if meta:
        meta.limite_gastos = float(data.get("limite_gastos", meta.limite_gastos))
        meta.meta_receita = float(data.get("meta_receita", meta.meta_receita))
    else:
        meta = Meta(
            mes=mes,
            ano=ano,
            limite_gastos=float(data.get("limite_gastos", 0)),
            meta_receita=float(data.get("meta_receita", 0)),
        )
        db.session.add(meta)

    db.session.commit()
    return jsonify(meta.to_dict()), 200


# ═════════════════════════════════════════════════════════════════
#  API — RELATÓRIO POR CATEGORIA
# ═════════════════════════════════════════════════════════════════

@app.route("/api/relatorio/categorias")
def api_relatorio_categorias():
    mes = request.args.get("mes", date.today().month, type=int)
    ano = request.args.get("ano", date.today().year, type=int)
    tipo = request.args.get("tipo", "saida")

    q = (
        db.session.query(
            Categoria.nome,
            Categoria.icone,
            Categoria.cor,
            func.sum(Transacao.valor).label("total"),
        )
        .join(Transacao, Transacao.categoria_id == Categoria.id)
        .filter(
            extract("month", Transacao.data) == mes,
            extract("year", Transacao.data) == ano,
            Transacao.tipo == tipo,
        )
        .group_by(Categoria.id)
        .order_by(func.sum(Transacao.valor).desc())
        .all()
    )

    return jsonify([
        {"nome": r.nome, "icone": r.icone, "cor": r.cor, "total": round(r.total, 2)}
        for r in q
    ])


# ─────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5000)
