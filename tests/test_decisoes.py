import os

from cortes import config, db, decisions, orchestrator

from .base import Base


class Decisoes(Base):
    def test_cinco_botoes_pesos_e_estados(self):
        expected = {100: "APROVADO", 50: "APROVADO", 0: "ADIADO", -50: "REJEITADO", -100: "REJEITADO"}
        for score, state in expected.items():
            oid = self.opportunity()
            r = decisions.decide("opportunity", oid, score)
            self.assertEqual(r["state"], state)
            d = db.get("decisions", r["decision_id"])
            self.assertEqual((d["score"], d["label"]), (score, decisions.BUTTONS[score]))
            self.assertEqual(d["reason"], "Sem motivo informado")
            has_task = bool(db.query("SELECT 1 FROM tasks WHERE stage='AQUISICAO' AND item_id=?", (oid,)))
            self.assertEqual(has_task, score > 0, f"aquisição só com aprovação positiva ({score})")

    def test_justificativa_opcional_salva(self):
        oid = self.opportunity()
        r = decisions.decide("opportunity", oid, -50, "Fonte pouco interessante")
        self.assertEqual(db.get("decisions", r["decision_id"])["reason"], "Fonte pouco interessante")

    def test_aprovacao_bloqueada_sem_disponibilidade(self):
        oid = self.opportunity(ok=False)
        with self.assertRaises(PermissionError):
            decisions.decide("opportunity", oid, 100)
        self.assertFalse(db.query("SELECT 1 FROM tasks WHERE item_id=?", (oid,)))

    def test_aquisicao_recusa_item_nao_aprovado(self):
        from cortes import acquire
        oid = self.opportunity(state="ADIADO")
        with self.assertRaises(PermissionError):
            acquire.acquire(oid)

    def test_janela_de_desfazer(self):
        oid = self.opportunity()
        r = decisions.decide("opportunity", oid, 100)
        self.assertIsNone(orchestrator.next_task(), "worker espera a janela de desfazer")
        decisions.undo(r["decision_id"])
        self.assertEqual(db.get("opportunities", oid)["state"], "AGUARDANDO_APROVACAO")
        self.assertFalse(db.query("SELECT 1 FROM tasks WHERE item_id=?", (oid,)))
        self.assertEqual(db.get("decisions", r["decision_id"])["undone"], 1)

    def test_desfazer_negado_apos_inicio(self):
        oid = self.opportunity()
        r = decisions.decide("opportunity", oid, 50)
        db.query("SELECT 1")  # noqa
        with db.tx() as c:
            c.execute("UPDATE tasks SET state='EM_EXECUCAO' WHERE item_id=?", (oid,))
        with self.assertRaises(PermissionError):
            decisions.undo(r["decision_id"])

    def test_corte_descartado_nao_volta_e_adiado_nao_produz(self):
        db.insert("cuts", {"id": "c1", "video_id": "v", "state": "AGUARDANDO_APROVACAO", "features": {"estilo": "humor"}})
        decisions.decide("cut", "c1", 0)
        self.assertFalse(db.query("SELECT 1 FROM tasks WHERE item_id='c1'"), "adiado não recebe legendas/finalização")
        decisions.decide("cut", "c1", -100)  # adiado pode ser reavaliado
        with self.assertRaises(PermissionError):
            decisions.decide("cut", "c1", 100)  # descartado nunca volta automaticamente

    def test_estatisticas_da_sessao(self):
        for s in (100, 50, 0, -50, -100):
            decisions.decide("opportunity", self.opportunity(), s)
        self.opportunity()
        st = decisions.stats("opportunity")
        self.assertEqual((st["avaliados"], st["aprovados"], st["adiados"], st["descartados"], st["pendentes"]),
                         (5, 2, 1, 2, 1))

    def test_worker_pega_tarefa_apos_janela(self):
        oid = self.opportunity()
        decisions.decide("opportunity", oid, 100)
        os.environ["CORTES_FAKE_NOW"] = config.iso(config.now() + config.timedelta(seconds=200))
        t = orchestrator.next_task()
        self.assertEqual((t["stage"], t["item_id"]), ("AQUISICAO", oid))
