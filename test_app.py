"""Testes de integração para as rotas financeiras."""

import os
import unittest
from datetime import date

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import _seed_categorias, app, db
from models import Categoria, Transacao


class FinanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(TESTING=True)
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

    def test_busca_filtro_paginacao_e_resumo(self):
        with app.app_context():
            categoria_id = self.despesa.id
            db.session.add_all([
                Transacao(descricao="Mercado Central", valor=40, data=date(2026, 5, 2), tipo="saida", categoria_id=categoria_id),
                Transacao(descricao="Mercado Bairro", valor=15, data=date(2026, 5, 3), tipo="saida", categoria_id=categoria_id),
                Transacao(descricao="Conta de luz", valor=80, data=date(2026, 5, 4), tipo="saida", categoria_id=categoria_id),
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
            db.session.add(Transacao(descricao="=1+1", valor=10, data=date(2026, 5, 2), tipo="saida", categoria_id=self.despesa.id))
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
            db.session.add(Transacao(descricao="Compra", valor=125, data=date(2026, 5, 2), tipo="saida", categoria_id=categoria_id))
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


if __name__ == "__main__":
    unittest.main()
