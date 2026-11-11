"""Aplicativo de gestão financeira em Flask."""

import csv
import io
import math
import os
import secrets
from datetime import date, datetime

from dateutil.relativedelta import relativedelta
from flask import Flask, Response, jsonify, render_template, request
from sqlalchemy import and_, extract, func
from werkzeug.exceptions import HTTPException

from extensions import db
from models import Categoria, Meta, OrcamentoCategoria, Recorrencia, Transacao

TIPOS = {"entrada", "saida"}
FREQUENCIAS = {"semanal": relativedelta(weeks=1), "mensal": relativedelta(months=1), "anual": relativedelta(years=1)}


def _erro(mensagem, status=400):
    return jsonify({"erro": mensagem}), status


def _json_body():
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        raise ValueError("Envie um objeto JSON válido.")
    return dados


def _valor_positivo(valor, campo="valor", permitir_zero=False):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f"{campo.capitalize()} deve ser um número válido.")
    if not math.isfinite(numero) or numero < 0 or (numero == 0 and not permitir_zero):
        regra = "maior ou igual a zero" if permitir_zero else "maior que zero"
        raise ValueError(f"{campo.capitalize()} deve ser {regra}.")
    return round(numero, 2)


def _csv_safe(valor):
    texto = str(valor or "")
    return "'" + texto if texto.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else texto


def _data_iso(valor, campo="data"):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{campo.capitalize()} inválida. Use o formato AAAA-MM-DD.")


def _validar_mes_ano(mes, ano):
    if mes is None or ano is None or not 1 <= mes <= 12 or not 1900 <= ano <= 2200:
        raise ValueError("Informe um mês entre 1 e 12 e um ano válido.")
    return mes, ano


def _argumento_inteiro(chave, padrao=None):
    valor = request.args.get(chave)
    if valor in (None, ""):
        return padrao
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ValueError(f"O parâmetro {chave} deve ser um número inteiro.")


def _categoria_valida(categoria_id, tipo=None):
    if categoria_id in (None, ""):
        return None
    try:
        categoria = db.session.get(Categoria, int(categoria_id))
    except (TypeError, ValueError):
        categoria = None
    if not categoria:
        raise ValueError("Categoria não encontrada.")
    if tipo and categoria.tipo != tipo:
        raise ValueError("A categoria não corresponde ao tipo da transação.")
    return categoria.id


def create_app(test_config=None):
    app = Flask(__name__)
    database_path = os.environ.get("DATABASE_URL", "sqlite:///database.db")
    if database_path.startswith("postgres://"):
        database_path = database_path.replace("postgres://", "postgresql://", 1)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_path,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        JSON_SORT_KEYS=False,
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        _seed_categorias()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000")
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        if request.path.startswith("/api/"):
            return _erro("Recurso não encontrado." if error.code == 404 else error.description, error.code)
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        app.logger.exception("Erro inesperado ao processar %s", request.path)
        if request.path.startswith("/api/"):
            return _erro("Não foi possível concluir a operação. Tente novamente.", 500)
        return "Ocorreu um erro inesperado.", 500

    return app


def _seed_categorias():
    if Categoria.query.count():
        return
    defaults = [
        {"nome": "Salário", "tipo": "entrada", "icone": "💼", "cor": "#6ee7b7"},
        {"nome": "Freelance", "tipo": "entrada", "icone": "💻", "cor": "#34d399"},
        {"nome": "Investimentos", "tipo": "entrada", "icone": "📈", "cor": "#10b981"},
        {"nome": "Outros (entrada)", "tipo": "entrada", "icone": "➕", "cor": "#059669"},
        {"nome": "Alimentação", "tipo": "saida", "icone": "🍔", "cor": "#f87171"},
        {"nome": "Transporte", "tipo": "saida", "icone": "🚗", "cor": "#fb923c"},
        {"nome": "Moradia", "tipo": "saida", "icone": "🏠", "cor": "#fbbf24"},
        {"nome": "Saúde", "tipo": "saida", "icone": "💊", "cor": "#a78bfa"},
        {"nome": "Lazer", "tipo": "saida", "icone": "🎮", "cor": "#60a5fa"},
        {"nome": "Educação", "tipo": "saida", "icone": "📚", "cor": "#818cf8"},
        {"nome": "Roupas", "tipo": "saida", "icone": "👗", "cor": "#f472b6"},
        {"nome": "Outros (saída)", "tipo": "saida", "icone": "➖", "cor": "#94a3b8"},
    ]
    db.session.add_all(Categoria(**item) for item in defaults)
    db.session.commit()


def _resumo_mes(mes, ano):
    totais = {"entrada": 0.0, "saida": 0.0}
    resultados = (
        db.session.query(Transacao.tipo, func.sum(Transacao.valor))
        .filter(extract("month", Transacao.data) == mes, extract("year", Transacao.data) == ano)
        .group_by(Transacao.tipo).all()
    )
    for tipo, total in resultados:
        totais[tipo] = round(total or 0, 2)
    meta = Meta.query.filter_by(mes=mes, ano=ano).first()
    limite = meta.limite_gastos if meta else 0.0
    meta_receita = meta.meta_receita if meta else 0.0
    despesas, receitas = totais["saida"], totais["entrada"]
    progresso_gastos = round(despesas / limite * 100, 1) if limite else 0
    progresso_receita = round(receitas / meta_receita * 100, 1) if meta_receita else 0
    return {
        "mes": mes, "ano": ano, "receitas": receitas, "despesas": despesas,
        "saldo": round(receitas - despesas, 2), "limite_gastos": limite,
        "meta_receita": meta_receita, "progresso_gastos": progresso_gastos,
        "progresso_receita": progresso_receita, "alerta_limite": despesas > limite > 0,
    }


app = create_app()


@app.route("/")
def dashboard():
    hoje = date.today()
    return render_template("dashboard.html", resumo=_resumo_mes(hoje.month, hoje.year), categorias=Categoria.query.order_by(Categoria.tipo, Categoria.nome).all(), mes_atual=hoje.month, ano_atual=hoje.year)


@app.route("/transacoes")
def transacoes_page():
    hoje = date.today()
    return render_template("transacoes.html", categorias=Categoria.query.order_by(Categoria.tipo, Categoria.nome).all(), mes_atual=hoje.month, ano_atual=hoje.year)


@app.route("/extrato")
def extrato_page():
    hoje = date.today()
    return render_template("extrato.html", categorias=Categoria.query.order_by(Categoria.nome).all(), mes_atual=hoje.month, ano_atual=hoje.year)


@app.route("/metas")
def metas_page():
    hoje = date.today()
    return render_template("metas.html", mes_atual=hoje.month, ano_atual=hoje.year, categorias=Categoria.query.filter_by(tipo="saida").order_by(Categoria.nome).all())


@app.route("/recorrencias")
def recorrencias_page():
    return render_template("recorrencias.html", categorias=Categoria.query.order_by(Categoria.tipo, Categoria.nome).all())


@app.route("/api/dashboard/resumo")
def api_resumo():
    try:
        mes, ano = _validar_mes_ano(_argumento_inteiro("mes", date.today().month), _argumento_inteiro("ano", date.today().year))
    except ValueError as error:
        return _erro(str(error))
    return jsonify(_resumo_mes(mes, ano))


@app.route("/api/dashboard/evolucao")
def api_evolucao():
    hoje = date.today()
    return jsonify([{"label": ref.strftime("%b/%y"), **{k: _resumo_mes(ref.month, ref.year)[k] for k in ("receitas", "despesas", "saldo")}} for ref in (hoje - relativedelta(months=i) for i in range(5, -1, -1))])


def _query_transacoes():
    query = Transacao.query
    mes, ano = _argumento_inteiro("mes"), _argumento_inteiro("ano")
    tipo, categoria_id = request.args.get("tipo", "").strip(), _argumento_inteiro("categoria_id")
    busca = request.args.get("q", "").strip()[:100]
    inicio, fim = request.args.get("data_inicio"), request.args.get("data_fim")
    if mes is not None and not 1 <= mes <= 12:
        raise ValueError("Mês inválido.")
    if ano is not None and not 1900 <= ano <= 2200:
        raise ValueError("Ano inválido.")
    if tipo and tipo not in TIPOS:
        raise ValueError("Tipo de transação inválido.")
    if categoria_id:
        if not db.session.get(Categoria, categoria_id):
            raise ValueError("Categoria não encontrada.")
        query = query.filter_by(categoria_id=categoria_id)
    if mes:
        query = query.filter(extract("month", Transacao.data) == mes)
    if ano:
        query = query.filter(extract("year", Transacao.data) == ano)
    if tipo:
        query = query.filter(Transacao.tipo == tipo)
    if busca:
        query = query.filter(Transacao.descricao.ilike(f"%{busca}%"))
    if inicio:
        query = query.filter(Transacao.data >= _data_iso(inicio, "data inicial"))
    if fim:
        query = query.filter(Transacao.data <= _data_iso(fim, "data final"))
    if inicio and fim and _data_iso(inicio, "data inicial") > _data_iso(fim, "data final"):
        raise ValueError("A data inicial deve ser anterior à data final.")
    return query.order_by(Transacao.data.desc(), Transacao.criado_em.desc())


@app.route("/api/transacoes", methods=["GET"])
def api_listar_transacoes():
    try:
        query = _query_transacoes()
        page = max(1, request.args.get("page", 1, type=int) or 1)
        per_page = min(100, max(1, request.args.get("per_page", 20, type=int) or 20))
    except ValueError as error:
        return _erro(str(error))
    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({"total": total, "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page, "items": [item.to_dict() for item in items]})


@app.route("/api/transacoes/resumo")
def api_resumo_transacoes():
    try:
        query = _query_transacoes().order_by(None).with_entities(Transacao.tipo, func.sum(Transacao.valor)).group_by(Transacao.tipo)
        totais = {"entrada": 0.0, "saida": 0.0}
        for tipo, total in query.all():
            totais[tipo] = round(total or 0, 2)
        return jsonify({"receitas": totais["entrada"], "despesas": totais["saida"], "saldo": round(totais["entrada"] - totais["saida"], 2)})
    except ValueError as error:
        return _erro(str(error))


@app.route("/api/transacoes/exportar.csv")
def api_exportar_transacoes():
    try:
        items = _query_transacoes().all()
    except ValueError as error:
        return _erro(str(error))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Data", "Descrição", "Tipo", "Categoria", "Valor"])
    for item in items:
        writer.writerow([item.data.isoformat(), _csv_safe(item.descricao), item.tipo, _csv_safe(item.categoria.nome if item.categoria else ""), f"{item.valor:.2f}"])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=extrato-financeiro.csv"})


@app.route("/api/transacoes", methods=["POST"])
def api_criar_transacao():
    try:
        dados = _json_body()
        descricao = str(dados.get("descricao", "")).strip()
        tipo = dados.get("tipo")
        if not descricao or len(descricao) > 200:
            raise ValueError("A descrição deve conter entre 1 e 200 caracteres.")
        if tipo not in TIPOS:
            raise ValueError("Tipo de transação inválido.")
        transacao = Transacao(descricao=descricao, valor=_valor_positivo(dados.get("valor")), data=_data_iso(dados.get("data")), tipo=tipo, categoria_id=_categoria_valida(dados.get("categoria_id"), tipo))
        db.session.add(transacao)
        db.session.commit()
        return jsonify(transacao.to_dict()), 201
    except ValueError as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/transacoes/<int:tid>", methods=["GET"])
def api_obter_transacao(tid):
    item = db.session.get(Transacao, tid)
    return jsonify(item.to_dict()) if item else _erro("Transação não encontrada.", 404)


@app.route("/api/transacoes/<int:tid>", methods=["PUT"])
def api_atualizar_transacao(tid):
    item = db.session.get(Transacao, tid)
    if not item:
        return _erro("Transação não encontrada.", 404)
    try:
        dados = _json_body()
        descricao = str(dados.get("descricao", item.descricao)).strip()
        tipo = dados.get("tipo", item.tipo)
        if not descricao or len(descricao) > 200:
            raise ValueError("A descrição deve conter entre 1 e 200 caracteres.")
        if tipo not in TIPOS:
            raise ValueError("Tipo de transação inválido.")
        item.descricao = descricao
        item.tipo = tipo
        item.valor = _valor_positivo(dados.get("valor", item.valor))
        item.data = _data_iso(dados["data"]) if "data" in dados else item.data
        item.categoria_id = _categoria_valida(dados.get("categoria_id", item.categoria_id), tipo)
        db.session.commit()
        return jsonify(item.to_dict())
    except ValueError as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/transacoes/<int:tid>", methods=["DELETE"])
def api_deletar_transacao(tid):
    item = db.session.get(Transacao, tid)
    if not item:
        return _erro("Transação não encontrada.", 404)
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True, "id": tid})


@app.route("/api/categorias", methods=["GET"])
def api_listar_categorias():
    tipo = request.args.get("tipo")
    if tipo and tipo not in TIPOS:
        return _erro("Tipo de categoria inválido.")
    query = Categoria.query.order_by(Categoria.tipo, Categoria.nome)
    if tipo:
        query = query.filter_by(tipo=tipo)
    return jsonify([item.to_dict() for item in query.all()])


@app.route("/api/categorias", methods=["POST"])
def api_criar_categoria():
    try:
        dados = _json_body()
        nome, tipo = str(dados.get("nome", "")).strip(), dados.get("tipo")
        if not nome or len(nome) > 80 or tipo not in TIPOS:
            raise ValueError("Informe um nome de até 80 caracteres e um tipo válido.")
        categoria = Categoria(nome=nome, tipo=tipo, icone=str(dados.get("icone", "💰"))[:10], cor="#6ee7b7")
        db.session.add(categoria)
        db.session.commit()
        return jsonify(categoria.to_dict()), 201
    except ValueError as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/metas", methods=["GET"])
def api_listar_metas():
    try:
        mes, ano = _validar_mes_ano(_argumento_inteiro("mes", date.today().month), _argumento_inteiro("ano", date.today().year))
    except ValueError as error:
        return _erro(str(error))
    meta = Meta.query.filter_by(mes=mes, ano=ano).first()
    resultado = meta.to_dict() if meta else {"mes": mes, "ano": ano, "limite_gastos": 0, "meta_receita": 0}
    resumo = _resumo_mes(mes, ano)
    resultado.update({"despesas_atuais": resumo["despesas"], "receitas_atuais": resumo["receitas"], "progresso_gastos": resumo["progresso_gastos"], "progresso_receita": resumo["progresso_receita"], "alerta_limite": resumo["alerta_limite"]})
    return jsonify(resultado)


@app.route("/api/metas", methods=["POST"])
def api_salvar_meta():
    try:
        dados = _json_body()
        mes, ano = _validar_mes_ano(int(dados.get("mes", date.today().month)), int(dados.get("ano", date.today().year)))
        meta = Meta.query.filter_by(mes=mes, ano=ano).first()
        if not meta:
            meta = Meta(mes=mes, ano=ano)
            db.session.add(meta)
        meta.limite_gastos = _valor_positivo(dados.get("limite_gastos", 0), "limite", True)
        meta.meta_receita = _valor_positivo(dados.get("meta_receita", 0), "meta de receita", True)
        db.session.commit()
        return jsonify(meta.to_dict())
    except (ValueError, TypeError) as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/orcamentos-categoria", methods=["GET"])
def api_orcamentos_categoria():
    try:
        mes, ano = _validar_mes_ano(_argumento_inteiro("mes", date.today().month), _argumento_inteiro("ano", date.today().year))
    except ValueError as error:
        return _erro(str(error))
    linhas = db.session.query(OrcamentoCategoria, func.coalesce(func.sum(Transacao.valor), 0)).select_from(OrcamentoCategoria).join(Categoria, Categoria.id == OrcamentoCategoria.categoria_id).outerjoin(Transacao, and_(Transacao.categoria_id == Categoria.id, Transacao.tipo == "saida", extract("month", Transacao.data) == mes, extract("year", Transacao.data) == ano)).filter(OrcamentoCategoria.mes == mes, OrcamentoCategoria.ano == ano).group_by(OrcamentoCategoria.id, Categoria.id, Categoria.nome, Categoria.icone).order_by(Categoria.nome).all()
    return jsonify([{"id": orc.id, "categoria_id": orc.categoria_id, "categoria_nome": orc.categoria.nome, "categoria_icone": orc.categoria.icone, "limite": orc.limite, "gasto": round(gasto, 2), "progresso": round(gasto / orc.limite * 100, 1) if orc.limite else 0, "alerta": gasto >= orc.limite * 0.8, "ultrapassado": gasto > orc.limite} for orc, gasto in linhas])


@app.route("/api/orcamentos-categoria", methods=["POST"])
def api_salvar_orcamento_categoria():
    try:
        dados = _json_body()
        mes, ano = _validar_mes_ano(int(dados.get("mes", date.today().month)), int(dados.get("ano", date.today().year)))
        categoria_id = _categoria_valida(dados.get("categoria_id"), "saida")
        if not categoria_id:
            raise ValueError("Selecione uma categoria de despesa.")
        limite = _valor_positivo(dados.get("limite"), "limite")
        orcamento = OrcamentoCategoria.query.filter_by(categoria_id=categoria_id, mes=mes, ano=ano).first()
        if not orcamento:
            orcamento = OrcamentoCategoria(categoria_id=categoria_id, mes=mes, ano=ano)
            db.session.add(orcamento)
        orcamento.limite = limite
        db.session.commit()
        return jsonify({"ok": True}), 200
    except (ValueError, TypeError) as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/recorrencias", methods=["GET", "POST"])
def api_recorrencias():
    if request.method == "GET":
        return jsonify([item.to_dict() for item in Recorrencia.query.order_by(Recorrencia.ativa.desc(), Recorrencia.proxima_data).all()])
    try:
        dados = _json_body()
        descricao, tipo, frequencia = str(dados.get("descricao", "")).strip(), dados.get("tipo"), dados.get("frequencia")
        if not descricao or len(descricao) > 200 or tipo not in TIPOS or frequencia not in FREQUENCIAS:
            raise ValueError("Informe descrição, tipo e frequência válidos.")
        recorrencia = Recorrencia(descricao=descricao, valor=_valor_positivo(dados.get("valor")), tipo=tipo, categoria_id=_categoria_valida(dados.get("categoria_id"), tipo), frequencia=frequencia, proxima_data=_data_iso(dados.get("proxima_data"), "próxima data"))
        db.session.add(recorrencia)
        db.session.commit()
        return jsonify(recorrencia.to_dict()), 201
    except ValueError as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/recorrencias/<int:rid>/executar", methods=["POST"])
def api_executar_recorrencia(rid):
    recorrencia = db.session.get(Recorrencia, rid)
    if not recorrencia:
        return _erro("Recorrência não encontrada.", 404)
    if not recorrencia.ativa:
        return _erro("Esta recorrência está pausada.")
    if recorrencia.proxima_data > date.today():
        return _erro("Esta recorrência ainda não está vencida.")
    transacao = Transacao(descricao=recorrencia.descricao, valor=recorrencia.valor, data=recorrencia.proxima_data, tipo=recorrencia.tipo, categoria_id=recorrencia.categoria_id)
    recorrencia.proxima_data = recorrencia.proxima_data + FREQUENCIAS[recorrencia.frequencia]
    db.session.add(transacao)
    db.session.commit()
    return jsonify({"transacao": transacao.to_dict(), "recorrencia": recorrencia.to_dict()}), 201


@app.route("/api/recorrencias/<int:rid>", methods=["PATCH", "DELETE"])
def api_alterar_recorrencia(rid):
    recorrencia = db.session.get(Recorrencia, rid)
    if not recorrencia:
        return _erro("Recorrência não encontrada.", 404)
    if request.method == "DELETE":
        db.session.delete(recorrencia)
        db.session.commit()
        return jsonify({"ok": True})
    try:
        dados = _json_body()
        ativa = dados.get("ativa", recorrencia.ativa)
        if not isinstance(ativa, bool):
            raise ValueError("O estado da recorrência deve ser verdadeiro ou falso.")
        recorrencia.ativa = ativa
        db.session.commit()
        return jsonify(recorrencia.to_dict())
    except ValueError as error:
        db.session.rollback()
        return _erro(str(error))


@app.route("/api/relatorio/categorias")
def api_relatorio_categorias():
    try:
        mes, ano = _validar_mes_ano(_argumento_inteiro("mes", date.today().month), _argumento_inteiro("ano", date.today().year))
    except ValueError as error:
        return _erro(str(error))
    tipo = request.args.get("tipo", "saida")
    if tipo not in TIPOS:
        return _erro("Tipo de transação inválido.")
    rows = db.session.query(Categoria.nome, Categoria.icone, Categoria.cor, func.sum(Transacao.valor).label("total")).join(Transacao, Transacao.categoria_id == Categoria.id).filter(extract("month", Transacao.data) == mes, extract("year", Transacao.data) == ano, Transacao.tipo == tipo).group_by(Categoria.id).order_by(func.sum(Transacao.valor).desc()).all()
    return jsonify([{"nome": row.nome, "icone": row.icone, "cor": row.cor, "total": round(row.total, 2)} for row in rows])


@app.route("/api/relatorio/comparativo")
def api_relatorio_comparativo():
    hoje = date.today()
    atual_mes = request.args.get("mes", hoje.month, type=int)
    atual_ano = request.args.get("ano", hoje.year, type=int)
    try:
        _validar_mes_ano(atual_mes, atual_ano)
    except ValueError as error:
        return _erro(str(error))
    atual = date(atual_ano, atual_mes, 1)
    anterior = atual - relativedelta(months=1)
    return jsonify({"atual": _resumo_mes(atual.month, atual.year), "anterior": _resumo_mes(anterior.month, anterior.year), "periodo_anterior": {"mes": anterior.month, "ano": anterior.year}})


if __name__ == "__main__":
    modo_desenvolvimento = os.environ.get("FLASK_ENV") == "development"
    debug_habilitado = modo_desenvolvimento and os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=debug_habilitado)
