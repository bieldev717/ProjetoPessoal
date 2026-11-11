"""Aplicativo de gestão financeira em Flask."""

import csv
import io
import math
import os
import secrets
from datetime import date, datetime
from functools import lru_cache
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from dateutil.relativedelta import relativedelta
from flask import Flask, Response, current_app, g, jsonify, render_template, request
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientConnectionError, PyJWKClientError
from sqlalchemy import and_, extract, func

from neon_store import NeonStore
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


def _neon_store():
    return current_app.extensions.get("neon_store")


@lru_cache(maxsize=4)
def _jwks_client(jwks_url):
    return PyJWKClient(jwks_url, cache_keys=True, timeout=5)


def _neon_auth_base_url():
    auth_url = str(current_app.config.get("NEON_AUTH_URL", "")).strip().rstrip("/")
    parts = urlsplit(auth_url)
    if parts.scheme not in {"https", "http"} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("URL do Neon Auth inválida.")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("O Neon Auth deve usar HTTPS.")
    return auth_url


def _neon_auth_origin():
    parts = urlsplit(_neon_auth_base_url())
    return f"{parts.scheme}://{parts.netloc}"


def _neon_jwks_url():
    return f"{_neon_auth_base_url()}/.well-known/jwks.json"


def _verificar_token_neon(token):
    origin = _neon_auth_origin()
    signing_key = _jwks_client(_neon_jwks_url()).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["EdDSA"],
        issuer=origin,
        audience=origin,
        options={"require": ["exp", "sub", "iss", "aud"]},
    )
    return UUID(claims["sub"]), claims.get("email", "")


def _claim_legacy_records(user_id):
    store = _neon_store()
    if not store:
        return

    async def claim(connection):
        if await connection.fetchval("SELECT owner_id FROM public.finance_legacy_claim WHERE singleton = 1"):
            return
        await connection.execute("SELECT pg_advisory_xact_lock(91600417)")
        if await connection.fetchval("SELECT owner_id FROM public.finance_legacy_claim WHERE singleton = 1"):
            return
        first_user_id = await connection.fetchval(
            'SELECT id FROM neon_auth."user" ORDER BY "createdAt" ASC, id ASC LIMIT 1'
        )
        if first_user_id != user_id:
            return
        claimed = await connection.fetchrow(
            "INSERT INTO public.finance_legacy_claim (singleton, owner_id) VALUES (1, $1) "
            "ON CONFLICT (singleton) DO NOTHING RETURNING owner_id",
            user_id,
        )
        if not claimed:
            return
        for table in ("transacoes", "metas", "orcamentos_categoria", "recorrencias"):
            await connection.execute(
                f"UPDATE public.{table} SET user_id = $1 WHERE user_id IS NULL",
                user_id,
            )

    store.transaction(claim)


def _categorias(tipo=None):
    store = _neon_store()
    if store:
        if tipo:
            rows = store.fetch("SELECT id, nome, tipo, icone, cor FROM categorias WHERE tipo = $1 ORDER BY tipo, nome", tipo)
        else:
            rows = store.fetch("SELECT id, nome, tipo, icone, cor FROM categorias ORDER BY tipo, nome")
        return [SimpleNamespace(**dict(row)) for row in rows]
    query = Categoria.query.order_by(Categoria.tipo, Categoria.nome)
    if tipo:
        query = query.filter_by(tipo=tipo)
    return query.all()


def _categoria_valida(categoria_id, tipo=None):
    if categoria_id in (None, ""):
        return None
    try:
        categoria_id = int(categoria_id)
    except (TypeError, ValueError):
        raise ValueError("Categoria não encontrada.")
    store = _neon_store()
    if store:
        categoria = store.fetchrow("SELECT id, tipo FROM categorias WHERE id = $1", categoria_id)
    else:
        categoria = db.session.get(Categoria, categoria_id)
    if not categoria:
        raise ValueError("Categoria não encontrada.")
    categoria_tipo = categoria["tipo"] if store else categoria.tipo
    if tipo and categoria_tipo != tipo:
        raise ValueError("A categoria não corresponde ao tipo da transação.")
    return categoria_id


def _neon_transacao_select():
    return """SELECT t.id, t.descricao, t.valor, t.data, t.tipo, t.categoria_id,
                     COALESCE(c.nome, '—') AS categoria_nome,
                     COALESCE(c.icone, '💰') AS categoria_icone,
                     COALESCE(c.cor, '#6ee7b7') AS categoria_cor
              FROM transacoes t LEFT JOIN categorias c ON c.id = t.categoria_id"""


def _neon_transacao_dict(row):
    return {
        "id": row["id"], "descricao": row["descricao"], "valor": float(row["valor"]),
        "data": row["data"].isoformat(), "tipo": row["tipo"], "categoria_id": row["categoria_id"],
        "categoria_nome": row["categoria_nome"], "categoria_icone": row["categoria_icone"],
        "categoria_cor": row["categoria_cor"],
    }


def _neon_get_transacao(transacao_id):
    row = _neon_store().fetchrow(
        f"{_neon_transacao_select()} WHERE t.id = $1 AND t.user_id = $2",
        transacao_id, g.user_id,
    )
    return _neon_transacao_dict(row) if row else None


def _neon_recorrencia_dict(row):
    if not row:
        return None
    item = dict(row)
    item["valor"] = float(item["valor"])
    item["proxima_data"] = item["proxima_data"].isoformat()
    return item


def _neon_get_recorrencia(recorrencia_id):
    row = _neon_store().fetchrow("""SELECT r.id, r.descricao, r.valor, r.tipo, r.categoria_id,
                                         COALESCE(c.nome, '—') AS categoria_nome,
                                         COALESCE(c.icone, '💰') AS categoria_icone,
                                         r.frequencia, r.proxima_data, r.ativa
                                  FROM recorrencias r LEFT JOIN categorias c ON c.id = r.categoria_id
                                  WHERE r.id = $1 AND r.user_id = $2""", recorrencia_id, g.user_id)
    return _neon_recorrencia_dict(row)


def _sqlite_transacao(transacao_id):
    return Transacao.query.filter_by(id=transacao_id, user_id=str(g.user_id)).first()


def _sqlite_recorrencia(recorrencia_id):
    return Recorrencia.query.filter_by(id=recorrencia_id, user_id=str(g.user_id)).first()


def _neon_transacao_filtros():
    conditions, values = ["t.user_id = $1"], [g.user_id]

    def add(expression, value):
        values.append(value)
        conditions.append(expression.format(index=len(values)))

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
    if categoria_id is not None:
        add("t.categoria_id = ${index}", _categoria_valida(categoria_id))
    if mes is not None:
        add("EXTRACT(MONTH FROM t.data) = ${index}", mes)
    if ano is not None:
        add("EXTRACT(YEAR FROM t.data) = ${index}", ano)
    if tipo:
        add("t.tipo = ${index}", tipo)
    if busca:
        add("t.descricao ILIKE ${index}", f"%{busca}%")
    if inicio:
        add("t.data >= ${index}", _data_iso(inicio, "data inicial"))
    if fim:
        add("t.data <= ${index}", _data_iso(fim, "data final"))
    if inicio and fim and _data_iso(inicio, "data inicial") > _data_iso(fim, "data final"):
        raise ValueError("A data inicial deve ser anterior à data final.")
    return (" WHERE " + " AND ".join(conditions) if conditions else ""), values


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
        USE_NEON=False,
        NEON_AUTH_URL=os.environ.get("NEON_AUTH_BASE_URL") or os.environ.get("VITE_NEON_AUTH_URL", ""),
        AUTH_TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
    database_path = app.config["SQLALCHEMY_DATABASE_URI"]
    is_neon = database_path.startswith(("postgresql://", "postgresql+asyncpg://"))
    if os.environ.get("VERCEL") and not is_neon:
        raise RuntimeError("DATABASE_URL do Neon é obrigatória na Vercel; SQLite não aceita gravações persistentes.")
    app.config["USE_NEON"] = is_neon
    if is_neon:
        app.extensions["neon_store"] = NeonStore(database_path)
        with app.app_context():
            _seed_neon_categorias()
    else:
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
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000")
        return response

    @app.before_request
    def require_api_authentication():
        if not request.path.startswith("/api/"):
            return None

        if current_app.testing and current_app.config.get("AUTH_TESTING"):
            raw_user_id = request.headers.get(
                "X-Test-User-ID", current_app.config.get("AUTH_TEST_USER_ID")
            )
            if not raw_user_id:
                return _erro("Autenticação necessária.", 401)
            try:
                g.user_id = UUID(raw_user_id)
            except (TypeError, ValueError):
                return _erro("Sessão inválida. Entre novamente.", 401)
            g.user_email = request.headers.get("X-Test-User-Email", "teste@exemplo.com")
        else:
            authorization = request.headers.get("Authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return _erro("Autenticação necessária.", 401)
            try:
                g.user_id, g.user_email = _verificar_token_neon(token)
            except (InvalidTokenError, PyJWKClientError, TypeError, ValueError) as error:
                if isinstance(error, PyJWKClientConnectionError):
                    current_app.logger.warning("Neon Auth indisponível (%s)", type(error).__name__)
                    return _erro("Serviço de autenticação temporariamente indisponível.", 503)
                return _erro("Sessão inválida ou expirada. Entre novamente.", 401)
            except Exception as error:
                current_app.logger.warning("Falha ao validar sessão (%s)", type(error).__name__)
                return _erro("Serviço de autenticação temporariamente indisponível.", 503)

        try:
            _claim_legacy_records(g.user_id)
        except Exception as error:
            current_app.logger.exception("Falha ao vincular dados financeiros antigos (%s)", type(error).__name__)
            return _erro("Não foi possível abrir seus dados financeiros.", 503)
        return None

    @app.context_processor
    def inject_neon_auth_url():
        return {"neon_auth_url": current_app.config.get("NEON_AUTH_URL", "")}

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


def _categorias_padrao():
    return [
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


def _seed_categorias():
    if not Categoria.query.count():
        db.session.add_all(Categoria(**item) for item in _categorias_padrao())
        db.session.commit()


def _seed_neon_categorias():
    store = _neon_store()

    async def inserir_padrao(connection):
        for item in _categorias_padrao():
            await connection.execute(
                "INSERT INTO categorias (nome, tipo, icone, cor) VALUES ($1, $2, $3, $4) ON CONFLICT (nome) DO NOTHING",
                item["nome"], item["tipo"], item["icone"], item["cor"],
            )

    store.transaction(inserir_padrao)


def _resumo_mes(mes, ano):
    totais = {"entrada": 0.0, "saida": 0.0}
    store = _neon_store()
    if store:
        resultados = store.fetch(
            "SELECT tipo, COALESCE(SUM(valor), 0)::float8 AS total FROM transacoes "
            "WHERE EXTRACT(MONTH FROM data) = $1 AND EXTRACT(YEAR FROM data) = $2 "
            "AND user_id = $3 GROUP BY tipo",
            mes, ano, g.user_id,
        )
        meta = store.fetchrow(
            "SELECT limite_gastos, meta_receita FROM metas WHERE mes = $1 AND ano = $2 AND user_id = $3",
            mes, ano, g.user_id,
        )
        for row in resultados:
            totais[row["tipo"]] = round(float(row["total"] or 0), 2)
        limite = float(meta["limite_gastos"]) if meta else 0.0
        meta_receita = float(meta["meta_receita"]) if meta else 0.0
    else:
        resultados = (
            db.session.query(Transacao.tipo, func.sum(Transacao.valor))
            .filter(
                Transacao.user_id == str(g.user_id),
                extract("month", Transacao.data) == mes,
                extract("year", Transacao.data) == ano,
            )
            .group_by(Transacao.tipo).all()
        )
        for tipo, total in resultados:
            totais[tipo] = round(total or 0, 2)
        meta = Meta.query.filter_by(mes=mes, ano=ano, user_id=str(g.user_id)).first()
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


@app.route("/login")
def login_page():
    return render_template("auth.html", mode="login")


@app.route("/cadastro")
def cadastro_page():
    return render_template("auth.html", mode="register")


@app.route("/")
def dashboard():
    hoje = date.today()
    return render_template("dashboard.html", categorias=_categorias(), mes_atual=hoje.month, ano_atual=hoje.year)


@app.route("/transacoes")
def transacoes_page():
    hoje = date.today()
    return render_template("transacoes.html", categorias=_categorias(), mes_atual=hoje.month, ano_atual=hoje.year)


@app.route("/extrato")
def extrato_page():
    hoje = date.today()
    return render_template("extrato.html", categorias=_categorias(), mes_atual=hoje.month, ano_atual=hoje.year)


@app.route("/metas")
def metas_page():
    hoje = date.today()
    return render_template("metas.html", mes_atual=hoje.month, ano_atual=hoje.year, categorias=_categorias("saida"))


@app.route("/recorrencias")
def recorrencias_page():
    return render_template("recorrencias.html", categorias=_categorias())


@app.route("/api/auth/me")
def api_auth_me():
    return jsonify({"id": str(g.user_id), "email": g.user_email})


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
    query = Transacao.query.filter_by(user_id=str(g.user_id))
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
        page = max(1, request.args.get("page", 1, type=int) or 1)
        per_page = min(100, max(1, request.args.get("per_page", 20, type=int) or 20))
        store = _neon_store()
        if store:
            where, values = _neon_transacao_filtros()
            total_row = store.fetchrow(f"SELECT COUNT(*) AS total FROM transacoes t{where}", *values)
            limit_index, offset_index = len(values) + 1, len(values) + 2
            rows = store.fetch(
                f"{_neon_transacao_select()}{where} ORDER BY t.data DESC, t.criado_em DESC LIMIT ${limit_index} OFFSET ${offset_index}",
                *values, per_page, (page - 1) * per_page,
            )
            total = total_row["total"]
            items = [_neon_transacao_dict(row) for row in rows]
        else:
            query = _query_transacoes()
            total = query.count()
            items = [item.to_dict() for item in query.offset((page - 1) * per_page).limit(per_page).all()]
    except ValueError as error:
        return _erro(str(error))
    return jsonify({"total": total, "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page, "items": items})

@app.route("/api/transacoes/resumo")
def api_resumo_transacoes():
    try:
        totais = {"entrada": 0.0, "saida": 0.0}
        store = _neon_store()
        if store:
            where, values = _neon_transacao_filtros()
            rows = store.fetch(f"SELECT t.tipo, COALESCE(SUM(t.valor), 0)::float8 AS total FROM transacoes t{where} GROUP BY t.tipo", *values)
            for row in rows:
                totais[row["tipo"]] = round(float(row["total"] or 0), 2)
        else:
            query = _query_transacoes().order_by(None).with_entities(Transacao.tipo, func.sum(Transacao.valor)).group_by(Transacao.tipo)
            for tipo, total in query.all():
                totais[tipo] = round(total or 0, 2)
        return jsonify({"receitas": totais["entrada"], "despesas": totais["saida"], "saldo": round(totais["entrada"] - totais["saida"], 2)})
    except ValueError as error:
        return _erro(str(error))

@app.route("/api/transacoes/exportar.csv")
def api_exportar_transacoes():
    try:
        store = _neon_store()
        if store:
            where, values = _neon_transacao_filtros()
            items = store.fetch(f"{_neon_transacao_select()}{where} ORDER BY t.data DESC, t.criado_em DESC", *values)
        else:
            items = _query_transacoes().all()
    except ValueError as error:
        return _erro(str(error))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Data", "Descrição", "Tipo", "Categoria", "Valor"])
    for item in items:
        if store:
            writer.writerow([item["data"].isoformat(), _csv_safe(item["descricao"]), item["tipo"], _csv_safe(item["categoria_nome"]), f"{float(item['valor']):.2f}"])
        else:
            writer.writerow([item.data.isoformat(), _csv_safe(item.descricao), item.tipo, _csv_safe(item.categoria.nome if item.categoria else ""), f"{item.valor:.2f}"])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=extrato-financeiro.csv"})

@app.route("/api/transacoes", methods=["POST"])
def api_criar_transacao():
    store = _neon_store()
    try:
        dados = _json_body()
        descricao = str(dados.get("descricao", "")).strip()
        tipo = dados.get("tipo")
        if not descricao or len(descricao) > 200:
            raise ValueError("A descrição deve conter entre 1 e 200 caracteres.")
        if tipo not in TIPOS:
            raise ValueError("Tipo de transação inválido.")
        valor = _valor_positivo(dados.get("valor"))
        data_transacao = _data_iso(dados.get("data"))
        categoria_id = _categoria_valida(dados.get("categoria_id"), tipo)
        if store:
            row = store.fetchrow(
                "INSERT INTO transacoes (descricao, valor, data, tipo, categoria_id, user_id) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                descricao, valor, data_transacao, tipo, categoria_id, g.user_id,
            )
            return jsonify(_neon_get_transacao(row["id"])), 201
        transacao = Transacao(
            descricao=descricao, valor=valor, data=data_transacao, tipo=tipo,
            categoria_id=categoria_id, user_id=str(g.user_id),
        )
        db.session.add(transacao)
        db.session.commit()
        return jsonify(transacao.to_dict()), 201
    except ValueError as error:
        if not store:
            db.session.rollback()
        return _erro(str(error))

@app.route("/api/transacoes/<int:tid>", methods=["GET"])
def api_obter_transacao(tid):
    store = _neon_store()
    item = _neon_get_transacao(tid) if store else _sqlite_transacao(tid)
    if not item:
        return _erro("Transação não encontrada.", 404)
    return jsonify(item if store else item.to_dict())

@app.route("/api/transacoes/<int:tid>", methods=["PUT"])
def api_atualizar_transacao(tid):
    store = _neon_store()
    item = _neon_get_transacao(tid) if store else _sqlite_transacao(tid)
    if not item:
        return _erro("Transação não encontrada.", 404)
    try:
        dados = _json_body()
        descricao = str(dados.get("descricao", item["descricao"] if store else item.descricao)).strip()
        tipo = dados.get("tipo", item["tipo"] if store else item.tipo)
        if not descricao or len(descricao) > 200:
            raise ValueError("A descrição deve conter entre 1 e 200 caracteres.")
        if tipo not in TIPOS:
            raise ValueError("Tipo de transação inválido.")
        valor = _valor_positivo(dados.get("valor", item["valor"] if store else item.valor))
        data_transacao = _data_iso(dados["data"]) if "data" in dados else (date.fromisoformat(item["data"]) if store else item.data)
        categoria_id = _categoria_valida(dados.get("categoria_id", item["categoria_id"] if store else item.categoria_id), tipo)
        if store:
            store.execute(
                "UPDATE transacoes SET descricao = $1, tipo = $2, valor = $3, data = $4, categoria_id = $5 "
                "WHERE id = $6 AND user_id = $7",
                descricao, tipo, valor, data_transacao, categoria_id, tid, g.user_id,
            )
            return jsonify(_neon_get_transacao(tid))
        item.descricao, item.tipo, item.valor, item.data, item.categoria_id = descricao, tipo, valor, data_transacao, categoria_id
        db.session.commit()
        return jsonify(item.to_dict())
    except ValueError as error:
        if not store:
            db.session.rollback()
        return _erro(str(error))

@app.route("/api/transacoes/<int:tid>", methods=["DELETE"])
def api_deletar_transacao(tid):
    store = _neon_store()
    item = _neon_get_transacao(tid) if store else _sqlite_transacao(tid)
    if not item:
        return _erro("Transação não encontrada.", 404)
    if store:
        store.execute("DELETE FROM transacoes WHERE id = $1 AND user_id = $2", tid, g.user_id)
    else:
        db.session.delete(item)
        db.session.commit()
    return jsonify({"ok": True, "id": tid})

@app.route("/api/categorias", methods=["GET"])
def api_listar_categorias():
    tipo = request.args.get("tipo")
    if tipo and tipo not in TIPOS:
        return _erro("Tipo de categoria inválido.")
    store = _neon_store()
    if store:
        rows = store.fetch("SELECT id, nome, tipo, icone, cor FROM categorias WHERE ($1::text IS NULL OR tipo = $1) ORDER BY tipo, nome", tipo or None)
        return jsonify([dict(row) for row in rows])
    query = Categoria.query.order_by(Categoria.tipo, Categoria.nome)
    if tipo:
        query = query.filter_by(tipo=tipo)
    return jsonify([item.to_dict() for item in query.all()])

@app.route("/api/categorias", methods=["POST"])
def api_criar_categoria():
    store = _neon_store()
    try:
        dados = _json_body()
        nome, tipo = str(dados.get("nome", "")).strip(), dados.get("tipo")
        if not nome or len(nome) > 80 or tipo not in TIPOS:
            raise ValueError("Informe um nome de até 80 caracteres e um tipo válido.")
        icone = str(dados.get("icone", "💰"))[:10]
        if store:
            row = store.fetchrow("INSERT INTO categorias (nome, tipo, icone, cor) VALUES ($1, $2, $3, $4) RETURNING id, nome, tipo, icone, cor", nome, tipo, icone, "#6ee7b7")
            return jsonify(dict(row)), 201
        categoria = Categoria(nome=nome, tipo=tipo, icone=icone, cor="#6ee7b7")
        db.session.add(categoria)
        db.session.commit()
        return jsonify(categoria.to_dict()), 201
    except ValueError as error:
        if not store:
            db.session.rollback()
        return _erro(str(error))

@app.route("/api/metas", methods=["GET"])
def api_listar_metas():
    try:
        mes, ano = _validar_mes_ano(_argumento_inteiro("mes", date.today().month), _argumento_inteiro("ano", date.today().year))
    except ValueError as error:
        return _erro(str(error))
    store = _neon_store()
    if store:
        meta = store.fetchrow(
            "SELECT id, mes, ano, limite_gastos, meta_receita FROM metas "
            "WHERE mes = $1 AND ano = $2 AND user_id = $3",
            mes, ano, g.user_id,
        )
        resultado = dict(meta) if meta else {"mes": mes, "ano": ano, "limite_gastos": 0, "meta_receita": 0}
    else:
        meta = Meta.query.filter_by(mes=mes, ano=ano, user_id=str(g.user_id)).first()
        resultado = meta.to_dict() if meta else {"mes": mes, "ano": ano, "limite_gastos": 0, "meta_receita": 0}
    resumo = _resumo_mes(mes, ano)
    resultado.update({"despesas_atuais": resumo["despesas"], "receitas_atuais": resumo["receitas"], "progresso_gastos": resumo["progresso_gastos"], "progresso_receita": resumo["progresso_receita"], "alerta_limite": resumo["alerta_limite"]})
    return jsonify(resultado)


@app.route("/api/metas", methods=["POST"])
def api_salvar_meta():
    store = _neon_store()
    try:
        dados = _json_body()
        mes, ano = _validar_mes_ano(int(dados.get("mes", date.today().month)), int(dados.get("ano", date.today().year)))
        limite = _valor_positivo(dados.get("limite_gastos", 0), "limite", True)
        meta_receita = _valor_positivo(dados.get("meta_receita", 0), "meta de receita", True)
        if store:
            meta = store.fetchrow(
                "INSERT INTO metas (mes, ano, limite_gastos, meta_receita, user_id) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (user_id, mes, ano) DO UPDATE "
                "SET limite_gastos = EXCLUDED.limite_gastos, meta_receita = EXCLUDED.meta_receita "
                "RETURNING id, mes, ano, limite_gastos, meta_receita",
                mes, ano, limite, meta_receita, g.user_id,
            )
            return jsonify(dict(meta))
        meta = Meta.query.filter_by(mes=mes, ano=ano, user_id=str(g.user_id)).first()
        if not meta:
            meta = Meta(mes=mes, ano=ano, user_id=str(g.user_id))
            db.session.add(meta)
        meta.limite_gastos, meta.meta_receita = limite, meta_receita
        db.session.commit()
        return jsonify(meta.to_dict())
    except (ValueError, TypeError) as error:
        if not store:
            db.session.rollback()
        return _erro(str(error))

@app.route("/api/orcamentos-categoria", methods=["GET"])
def api_orcamentos_categoria():
    try:
        mes, ano = _validar_mes_ano(_argumento_inteiro("mes", date.today().month), _argumento_inteiro("ano", date.today().year))
    except ValueError as error:
        return _erro(str(error))
    store = _neon_store()
    if store:
        linhas = store.fetch("""SELECT o.id, o.categoria_id, c.nome AS categoria_nome, c.icone AS categoria_icone,
                                      o.limite, COALESCE(SUM(t.valor), 0)::float8 AS gasto
                               FROM orcamentos_categoria o
                               JOIN categorias c ON c.id = o.categoria_id
                               LEFT JOIN transacoes t ON t.categoria_id = o.categoria_id AND t.tipo = 'saida'
                                 AND t.user_id = o.user_id
                                 AND EXTRACT(MONTH FROM t.data) = $1 AND EXTRACT(YEAR FROM t.data) = $2
                               WHERE o.mes = $1 AND o.ano = $2 AND o.user_id = $3
                               GROUP BY o.id, c.id, c.nome, c.icone, o.limite ORDER BY c.nome""", mes, ano, g.user_id)
        return jsonify([{"id": row["id"], "categoria_id": row["categoria_id"], "categoria_nome": row["categoria_nome"], "categoria_icone": row["categoria_icone"], "limite": float(row["limite"]), "gasto": round(float(row["gasto"]), 2), "progresso": round(float(row["gasto"]) / float(row["limite"]) * 100, 1), "alerta": float(row["gasto"]) >= float(row["limite"]) * 0.8, "ultrapassado": float(row["gasto"]) > float(row["limite"])} for row in linhas])
    linhas = db.session.query(OrcamentoCategoria, func.coalesce(func.sum(Transacao.valor), 0)).select_from(OrcamentoCategoria).join(Categoria, Categoria.id == OrcamentoCategoria.categoria_id).outerjoin(Transacao, and_(Transacao.categoria_id == Categoria.id, Transacao.user_id == str(g.user_id), Transacao.tipo == "saida", extract("month", Transacao.data) == mes, extract("year", Transacao.data) == ano)).filter(OrcamentoCategoria.mes == mes, OrcamentoCategoria.ano == ano, OrcamentoCategoria.user_id == str(g.user_id)).group_by(OrcamentoCategoria.id, Categoria.id, Categoria.nome, Categoria.icone).order_by(Categoria.nome).all()
    return jsonify([{"id": orc.id, "categoria_id": orc.categoria_id, "categoria_nome": orc.categoria.nome, "categoria_icone": orc.categoria.icone, "limite": orc.limite, "gasto": round(gasto, 2), "progresso": round(gasto / orc.limite * 100, 1) if orc.limite else 0, "alerta": gasto >= orc.limite * 0.8, "ultrapassado": gasto > orc.limite} for orc, gasto in linhas])

@app.route("/api/orcamentos-categoria", methods=["POST"])
def api_salvar_orcamento_categoria():
    store = _neon_store()
    try:
        dados = _json_body()
        mes, ano = _validar_mes_ano(int(dados.get("mes", date.today().month)), int(dados.get("ano", date.today().year)))
        categoria_id = _categoria_valida(dados.get("categoria_id"), "saida")
        if not categoria_id:
            raise ValueError("Selecione uma categoria de despesa.")
        limite = _valor_positivo(dados.get("limite"), "limite")
        if store:
            store.fetchrow(
                "INSERT INTO orcamentos_categoria (categoria_id, mes, ano, limite, user_id) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (user_id, categoria_id, mes, ano) "
                "DO UPDATE SET limite = EXCLUDED.limite RETURNING id",
                categoria_id, mes, ano, limite, g.user_id,
            )
            return jsonify({"ok": True})

        orcamento = OrcamentoCategoria.query.filter_by(
            categoria_id=categoria_id, mes=mes, ano=ano, user_id=str(g.user_id)
        ).first()
        if not orcamento:
            orcamento = OrcamentoCategoria(
                categoria_id=categoria_id, mes=mes, ano=ano, user_id=str(g.user_id)
            )
            db.session.add(orcamento)
        orcamento.limite = limite
        db.session.commit()
        return jsonify({"ok": True})
    except (ValueError, TypeError) as error:
        if not store:
            db.session.rollback()
        return _erro(str(error))

@app.route("/api/recorrencias", methods=["GET", "POST"])
def api_recorrencias():
    store = _neon_store()
    if request.method == "GET":
        if store:
            rows = store.fetch("""SELECT r.id, r.descricao, r.valor, r.tipo, r.categoria_id,
                                        COALESCE(c.nome, '—') AS categoria_nome,
                                        COALESCE(c.icone, '💰') AS categoria_icone,
                                        r.frequencia, r.proxima_data, r.ativa
                                 FROM recorrencias r LEFT JOIN categorias c ON c.id = r.categoria_id
                                 WHERE r.user_id = $1
                                 ORDER BY r.ativa DESC, r.proxima_data""", g.user_id)
            return jsonify([_neon_recorrencia_dict(row) for row in rows])
        return jsonify([item.to_dict() for item in Recorrencia.query.filter_by(user_id=str(g.user_id)).order_by(Recorrencia.ativa.desc(), Recorrencia.proxima_data).all()])
    try:
        dados = _json_body()
        descricao, tipo, frequencia = str(dados.get("descricao", "")).strip(), dados.get("tipo"), dados.get("frequencia")
        if not descricao or len(descricao) > 200 or tipo not in TIPOS or frequencia not in FREQUENCIAS:
            raise ValueError("Informe descrição, tipo e frequência válidos.")
        valor = _valor_positivo(dados.get("valor"))
        categoria_id = _categoria_valida(dados.get("categoria_id"), tipo)
        proxima_data = _data_iso(dados.get("proxima_data"), "próxima data")
        if store:
            row = store.fetchrow(
                "INSERT INTO recorrencias (descricao, valor, tipo, categoria_id, frequencia, proxima_data, ativa, user_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7) RETURNING id",
                descricao, valor, tipo, categoria_id, frequencia, proxima_data, g.user_id,
            )
            return jsonify(_neon_get_recorrencia(row["id"])), 201
        recorrencia = Recorrencia(
            descricao=descricao, valor=valor, tipo=tipo, categoria_id=categoria_id,
            frequencia=frequencia, proxima_data=proxima_data, user_id=str(g.user_id),
        )
        db.session.add(recorrencia)
        db.session.commit()
        return jsonify(recorrencia.to_dict()), 201
    except ValueError as error:
        if not store:
            db.session.rollback()
        return _erro(str(error))

@app.route("/api/recorrencias/<int:rid>/executar", methods=["POST"])
def api_executar_recorrencia(rid):
    store = _neon_store()
    if store:
        async def executar(connection):
            recorrencia = await connection.fetchrow(
                "SELECT id, descricao, valor, tipo, categoria_id, frequencia, proxima_data, ativa "
                "FROM recorrencias WHERE id = $1 AND user_id = $2 FOR UPDATE",
                rid, g.user_id,
            )
            if not recorrencia:
                raise LookupError("Recorrência não encontrada.")
            if not recorrencia["ativa"]:
                raise ValueError("Esta recorrência está pausada.")
            if recorrencia["proxima_data"] > date.today():
                raise ValueError("Esta recorrência ainda não está vencida.")
            transacao = await connection.fetchrow(
                "INSERT INTO transacoes (descricao, valor, data, tipo, categoria_id, user_id) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                recorrencia["descricao"], recorrencia["valor"], recorrencia["proxima_data"],
                recorrencia["tipo"], recorrencia["categoria_id"], g.user_id,
            )
            proxima_data = recorrencia["proxima_data"] + FREQUENCIAS[recorrencia["frequencia"]]
            await connection.execute(
                "UPDATE recorrencias SET proxima_data = $1 WHERE id = $2 AND user_id = $3",
                proxima_data, rid, g.user_id,
            )
            return transacao["id"]

        try:
            transacao_id = store.transaction(executar)
        except LookupError as error:
            return _erro(str(error), 404)
        except ValueError as error:
            return _erro(str(error))
        return jsonify({"transacao": _neon_get_transacao(transacao_id), "recorrencia": _neon_get_recorrencia(rid)}), 201
    recorrencia = _sqlite_recorrencia(rid)
    if not recorrencia:
        return _erro("Recorrência não encontrada.", 404)
    if not recorrencia.ativa:
        return _erro("Esta recorrência está pausada.")
    if recorrencia.proxima_data > date.today():
        return _erro("Esta recorrência ainda não está vencida.")
    transacao = Transacao(
        descricao=recorrencia.descricao, valor=recorrencia.valor, data=recorrencia.proxima_data,
        tipo=recorrencia.tipo, categoria_id=recorrencia.categoria_id, user_id=str(g.user_id),
    )
    recorrencia.proxima_data = recorrencia.proxima_data + FREQUENCIAS[recorrencia.frequencia]
    db.session.add(transacao)
    db.session.commit()
    return jsonify({"transacao": transacao.to_dict(), "recorrencia": recorrencia.to_dict()}), 201

@app.route("/api/recorrencias/<int:rid>", methods=["PATCH", "DELETE"])
def api_alterar_recorrencia(rid):
    store = _neon_store()
    recorrencia = _neon_get_recorrencia(rid) if store else _sqlite_recorrencia(rid)
    if not recorrencia:
        return _erro("Recorrência não encontrada.", 404)
    if request.method == "DELETE":
        if store:
            store.execute("DELETE FROM recorrencias WHERE id = $1 AND user_id = $2", rid, g.user_id)
        else:
            db.session.delete(recorrencia)
            db.session.commit()
        return jsonify({"ok": True})
    try:
        dados = _json_body()
        ativa = dados.get("ativa", recorrencia["ativa"] if store else recorrencia.ativa)
        if not isinstance(ativa, bool):
            raise ValueError("O estado da recorrência deve ser verdadeiro ou falso.")
        if store:
            store.execute(
                "UPDATE recorrencias SET ativa = $1 WHERE id = $2 AND user_id = $3",
                ativa, rid, g.user_id,
            )
            return jsonify(_neon_get_recorrencia(rid))
        recorrencia.ativa = ativa
        db.session.commit()
        return jsonify(recorrencia.to_dict())
    except ValueError as error:
        if not store:
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
    store = _neon_store()
    if store:
        rows = store.fetch("""SELECT c.nome, c.icone, c.cor, SUM(t.valor)::float8 AS total
                               FROM categorias c JOIN transacoes t ON t.categoria_id = c.id
                               WHERE EXTRACT(MONTH FROM t.data) = $1 AND EXTRACT(YEAR FROM t.data) = $2
                                 AND t.tipo = $3 AND t.user_id = $4
                               GROUP BY c.id, c.nome, c.icone, c.cor ORDER BY SUM(t.valor) DESC""", mes, ano, tipo, g.user_id)
        return jsonify([{"nome": row["nome"], "icone": row["icone"], "cor": row["cor"], "total": round(float(row["total"]), 2)} for row in rows])
    rows = db.session.query(Categoria.nome, Categoria.icone, Categoria.cor, func.sum(Transacao.valor).label("total")).join(Transacao, Transacao.categoria_id == Categoria.id).filter(Transacao.user_id == str(g.user_id), extract("month", Transacao.data) == mes, extract("year", Transacao.data) == ano, Transacao.tipo == tipo).group_by(Categoria.id).order_by(func.sum(Transacao.valor).desc()).all()
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
