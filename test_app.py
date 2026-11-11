"""Testes de integração para as rotas financeiras."""

import os
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

TEST_USER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"

from app import _neon_auth_base_url, _neon_auth_origin, _neon_jwks_url, _seed_categorias, app, db
from models import Categoria, Transacao


class FinanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(
            TESTING=True,
            AUTH_TESTING=True,
            AUTH_TEST_USER_ID=TEST_USER_ID,
        )
        cls.client = app.test_client()

    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()
            _seed_categorias()
            self.despesa = Categoria.query.filter_by(tipo="saida").first()
            self.receita = Categoria.query.filter_by(tipo="entrada").first()

    def test_transacao_rejeita_valor_invalido_e_categoria_incompativel(self):
        with app.app_context():
            despesa_id, receita_id = self.despesa.id, self.receita.id
        payload = {"descricao": "Teste", "valor": "NaN", "data": "2026-01-01", "tipo": "saida"}
        self.assertEqual(self.client.post("/api/transacoes", json=payload).status_code, 400)
        payload.update(valor=25, categoria_id=receita_id)
        response = self.client.post("/api/transacoes", json=payload)
        self.assertEqual(response.status_code, 400)
        payload["categoria_id"] = despesa_id
        self.assertEqual(self.client.post("/api/transacoes", json=payload).status_code, 201)

    def test_transacao_cria_consulta_e_remove(self):
        with app.app_context():
            categoria_id = self.despesa.id
        criada = self.client.post("/api/transacoes", json={"descricao": "Despesa de teste", "valor": 32.5, "data": "2026-05-02", "tipo": "saida", "categoria_id": categoria_id})
        self.assertEqual(criada.status_code, 201)
        transacao_id = criada.json["id"]
        consultada = self.client.get(f"/api/transacoes/{transacao_id}")
        self.assertEqual(consultada.json["descricao"], "Despesa de teste")
        removida = self.client.delete(f"/api/transacoes/{transacao_id}")
        self.assertEqual(removida.status_code, 200)
        self.assertEqual(self.client.get(f"/api/transacoes/{transacao_id}").status_code, 404)

    def test_meta_salva_e_atualiza_mes_ano(self):
        primeiro = self.client.post("/api/metas", json={"mes": 12, "ano": 2026, "limite_gastos": 2500, "meta_receita": 5000})
        self.assertEqual(primeiro.status_code, 200)
        segundo = self.client.post("/api/metas", json={"mes": 12, "ano": 2026, "limite_gastos": 3000, "meta_receita": 6000})
        self.assertEqual(segundo.status_code, 200)
        meta = self.client.get("/api/metas?mes=12&ano=2026").json
        self.assertEqual(meta["limite_gastos"], 3000)
        self.assertEqual(meta["meta_receita"], 6000)

    def test_busca_filtro_paginacao_e_resumo(self):
        with app.app_context():
            categoria_id = self.despesa.id
            db.session.add_all([
                Transacao(descricao="Mercado Central", valor=40, data=date(2026, 5, 2), tipo="saida", categoria_id=categoria_id, user_id=TEST_USER_ID),
                Transacao(descricao="Mercado Bairro", valor=15, data=date(2026, 5, 3), tipo="saida", categoria_id=categoria_id, user_id=TEST_USER_ID),
                Transacao(descricao="Conta de luz", valor=80, data=date(2026, 5, 4), tipo="saida", categoria_id=categoria_id, user_id=TEST_USER_ID),
            ])
            db.session.commit()
        response = self.client.get("/api/transacoes?mes=5&ano=2026&q=mercado&per_page=1&page=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["total"], 2)
        self.assertEqual(response.json["pages"], 2)
        resumo = self.client.get("/api/transacoes/resumo?mes=5&ano=2026&q=mercado").json
        self.assertEqual(resumo["despesas"], 55)
        self.assertEqual(resumo["saldo"], -55)

    def test_csv_filtra_e_protege_formula(self):
        with app.app_context():
            db.session.add(Transacao(descricao="=1+1", valor=10, data=date(2026, 5, 2), tipo="saida", categoria_id=self.despesa.id, user_id=TEST_USER_ID))
            db.session.commit()
        response = self.client.get("/api/transacoes/exportar.csv?mes=5&ano=2026")
        self.assertEqual(response.status_code, 200)
        self.assertIn("'=1+1", response.get_data(as_text=True))
        self.assertTrue(response.headers["Content-Type"].startswith("text/csv"))

    def test_orcamento_por_categoria_recalcula_gasto(self):
        with app.app_context():
            categoria_id = self.despesa.id
        resposta = self.client.post("/api/orcamentos-categoria", json={"mes": 5, "ano": 2026, "categoria_id": categoria_id, "limite": 100})
        self.assertEqual(resposta.status_code, 200)
        with app.app_context():
            db.session.add(Transacao(descricao="Compra", valor=125, data=date(2026, 5, 2), tipo="saida", categoria_id=categoria_id, user_id=TEST_USER_ID))
            db.session.commit()
        dados = self.client.get("/api/orcamentos-categoria?mes=5&ano=2026").json
        self.assertEqual(dados[0]["gasto"], 125)
        self.assertTrue(dados[0]["alerta"])

    def test_recorrencia_so_registra_quando_vencida_e_avanca_data(self):
        data_vencida = "2000-01-31"
        criada = self.client.post("/api/recorrencias", json={"descricao": "Aluguel", "valor": 1200, "tipo": "saida", "frequencia": "mensal", "proxima_data": data_vencida})
        self.assertEqual(criada.status_code, 201)
        rid = criada.json["id"]
        response = self.client.post(f"/api/recorrencias/{rid}/executar")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["transacao"]["data"], data_vencida)
        self.assertEqual(self.client.get("/api/recorrencias").json[0]["proxima_data"], "2000-02-29")

    def test_erros_de_api_sao_json_e_tem_mensagens_claras(self):
        response = self.client.get("/api/transacoes?mes=13")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["erro"], "Mês inválido.")
        self.assertEqual(self.client.get("/api/transacoes/999999").status_code, 404)
        self.assertEqual(self.client.get("/api/transacoes/999999").json["erro"], "Transação não encontrada.")

    def test_parametros_numericos_malformados_sao_rejeitados(self):
        response = self.client.get("/api/transacoes?mes=abc")
        self.assertEqual(response.status_code, 400)
        self.assertIn("inteiro", response.json["erro"])
        response = self.client.get("/api/dashboard/resumo?ano=abc")
        self.assertEqual(response.status_code, 400)

    def test_respostas_tem_headers_basicos_de_seguranca(self):
        response = self.client.get("/api/dashboard/resumo?mes=5&ano=2026")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_neon_auth_preserva_caminho_da_url_jwks(self):
        with app.app_context():
            auth_url_anterior = app.config["NEON_AUTH_URL"]
            app.config["NEON_AUTH_URL"] = "https://auth.example.com/neondb/auth/"
            try:
                self.assertEqual(_neon_auth_base_url(), "https://auth.example.com/neondb/auth")
                self.assertEqual(_neon_auth_origin(), "https://auth.example.com")
                self.assertEqual(_neon_jwks_url(), "https://auth.example.com/neondb/auth/.well-known/jwks.json")
            finally:
                app.config["NEON_AUTH_URL"] = auth_url_anterior

    def test_api_exige_autenticacao(self):
        response = self.client.get("/api/auth/me", headers={"X-Test-User-ID": ""})
        self.assertEqual(response.status_code, 401)

    def test_dados_ficam_isolados_por_usuario(self):
        with app.app_context():
            categoria_id = self.despesa.id
        outra_conta = {"X-Test-User-ID": OTHER_USER_ID}

        criada = self.client.post(
            "/api/transacoes",
            json={"descricao": "Conta A", "valor": 42, "data": "2026-06-05", "tipo": "saida", "categoria_id": categoria_id},
        )
        transacao_id = criada.json["id"]
        self.assertEqual(self.client.get("/api/transacoes?mes=6&ano=2026", headers=outra_conta).json["total"], 0)
        self.assertEqual(self.client.get(f"/api/transacoes/{transacao_id}", headers=outra_conta).status_code, 404)
        self.assertEqual(self.client.put(f"/api/transacoes/{transacao_id}", headers=outra_conta, json={}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/transacoes/{transacao_id}", headers=outra_conta).status_code, 404)

        self.client.post("/api/metas", json={"mes": 6, "ano": 2026, "limite_gastos": 800, "meta_receita": 3000})
        meta_outra_conta = self.client.get("/api/metas?mes=6&ano=2026", headers=outra_conta).json
        self.assertEqual(meta_outra_conta["limite_gastos"], 0)

        self.client.post("/api/orcamentos-categoria", json={"mes": 6, "ano": 2026, "categoria_id": categoria_id, "limite": 250})
        self.assertEqual(self.client.get("/api/orcamentos-categoria?mes=6&ano=2026", headers=outra_conta).json, [])

        recorrencia = self.client.post(
            "/api/recorrencias",
            json={"descricao": "Conta A", "valor": 50, "tipo": "saida", "frequencia": "mensal", "proxima_data": "2026-06-05", "categoria_id": categoria_id},
        )
        recorrencia_id = recorrencia.json["id"]
        self.assertEqual(self.client.get("/api/recorrencias", headers=outra_conta).json, [])
        self.assertEqual(self.client.post(f"/api/recorrencias/{recorrencia_id}/executar", headers=outra_conta).status_code, 404)

    def test_paginas_de_acesso_e_painel_estao_disponiveis(self):
        for path in ("/login", "/cadastro"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'data-auth-page="true"', response.data)
        for path in ("/", "/transacoes", "/extrato", "/metas", "/recorrencias"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(b'data-auth-required="true"', response.data)
            self.assertIn(b"runWhenAuthenticated", response.data)


if __name__ == "__main__":
    unittest.main()
