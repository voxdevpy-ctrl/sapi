"""Regressões dos achados do revisor técnico independente (idempotência, concorrência, segurança)."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from cortes import config, db, decisions, orchestrator, publish, qc, transcribe, web

from .base import Base


class Idempotencia(Base):
    def test_pesquisa_reprovada_nao_deixa_orfas(self):
        f = config.PROD / "col.json"
        f.write_text(json.dumps({"candidatos": [{"candidato_id": "c1", "canal": "a", "url": "u1"}], "erros": []}), "utf-8")
        bad = {"collect_file": str(f), "llm": {"oportunidades": [{"candidato_id": "c9", "evidencias": ["x"]}], "descartados": []},
               "proposals": [{"id": "opp_x", "source_url": "u1", "topic": "t", "state": "PENDENTE"}]}
        orchestrator.enqueue("PESQUISA", "batch", "p1")
        with mock.patch.dict(orchestrator.HANDLERS, {"PESQUISA": lambda t: bad}):
            orchestrator.run_task(orchestrator.next_task())
        self.assertFalse(db.query("SELECT * FROM opportunities"), "nada é gravado antes da auditoria aprovar")

    def test_reserva_atomica(self):
        orchestrator.enqueue("TRANSCRICAO", "video", "v1")
        tid = db.query("SELECT id FROM tasks")[0]["id"]
        self.assertTrue(orchestrator.claim(tid))
        self.assertFalse(orchestrator.claim(tid), "segundo executor não pega a mesma tarefa")

    def test_recover_reaplica_efeitos_e_reaudita_sem_reexecutar(self):
        oid = self.opportunity(state="PENDENTE")
        orchestrator.enqueue("VERIFICACAO", "opportunity", oid)
        t = db.query("SELECT * FROM tasks")[0]
        av = {"ok": True, "checked_at": "x", "method": "arquivo_local", "authorization": "conteudo_proprio",
              "duration": 100, "problems": []}
        db.update("tasks", t["id"], {"state": "AUDITADO", "output": {"availability": av}})
        orchestrator.recover()  # queda entre aprovação da auditoria e aplicação dos efeitos
        self.assertEqual(db.get("opportunities", oid)["state"], "AGUARDANDO_APROVACAO")
        self.assertEqual(db.get("tasks", t["id"])["state"], "CONCLUIDO")

        oid2 = self.opportunity(state="PENDENTE")
        orchestrator.enqueue("VERIFICACAO", "opportunity", oid2)
        t2 = db.query("SELECT * FROM tasks WHERE item_id=?", (oid2,))[0]
        db.update("tasks", t2["id"], {"state": "EM_VALIDACAO", "output": {"availability": av}})
        orchestrator.recover()
        handler = mock.Mock()
        with mock.patch.dict(orchestrator.HANDLERS, {"VERIFICACAO": handler}):
            orchestrator.run_task(orchestrator.next_task())
        handler.assert_not_called()  # a etapa não é refeita, só reauditada
        self.assertEqual(db.get("opportunities", oid2)["state"], "AGUARDANDO_APROVACAO")

    def test_fechar_bat_nao_conta_como_falha_mas_ciclo_de_quedas_bloqueia(self):
        orchestrator.enqueue("TRANSCRICAO", "video", "v1")
        for i in range(orchestrator.MAX_INTERRUPTIONS):
            with db.tx() as c:
                c.execute("UPDATE tasks SET state='EM_EXECUCAO'")
            orchestrator.recover()
            t = db.query("SELECT * FROM tasks")[0]
            if i < orchestrator.MAX_INTERRUPTIONS - 1:
                self.assertEqual((t["state"], t["attempts"]), ("PENDENTE", 0), "fechar o .bat não gasta tentativa")
        self.assertEqual(t["state"], "BLOQUEADO")


class Concorrencia(Base):
    def test_desfazer_nao_apaga_tarefa_em_execucao(self):
        oid = self.opportunity()
        r = decisions.decide("opportunity", oid, 50)
        with db.tx() as c:
            c.execute("UPDATE tasks SET state='EM_EXECUCAO' WHERE item_id=?", (oid,))
        with self.assertRaises(PermissionError):
            decisions.undo(r["decision_id"])
        self.assertEqual(db.get("opportunities", oid)["state"], "APROVADO")
        self.assertTrue(db.query("SELECT 1 FROM tasks WHERE item_id=?", (oid,)))

    def test_decisao_dupla_nao_gera_conflito(self):
        oid = self.opportunity()
        decisions.decide("opportunity", oid, 100)
        with self.assertRaises(PermissionError):
            decisions.decide("opportunity", oid, -100)

    def test_etapa_de_corte_cancelada_se_corte_nao_esta_aprovado(self):
        db.insert("cuts", {"id": "c1", "video_id": "v", "state": "REJEITADO"})
        orchestrator.enqueue("LEGENDAS", "cut", "c1")
        handler = mock.Mock()
        with mock.patch.dict(orchestrator.HANDLERS, {"LEGENDAS": handler}):
            orchestrator.run_task(orchestrator.next_task())
        handler.assert_not_called()
        self.assertEqual(db.query("SELECT state FROM tasks")[0]["state"], "CANCELADO")

    def test_qc_com_falha_de_legenda_volta_para_legendas(self):
        db.insert("cuts", {"id": "c1", "video_id": "v", "state": "APROVADO"})
        for st in ("LEGENDAS", "FINALIZACAO", "QC"):
            orchestrator.enqueue(st, "cut", "c1")
        with db.tx() as c:
            c.execute("UPDATE tasks SET state='CONCLUIDO' WHERE stage<>'QC'")
        bad = {"status": "REPROVADO", "falhas": ["legenda_na_tela"], "ressalvas": [],
               "testes": {k: {"ok": k != "legenda_na_tela", "detalhe": "", "grave": True} for k in qc.REQUIRED_TESTS}}
        with mock.patch.dict(orchestrator.HANDLERS, {"QC": lambda t: {"qc": bad}}):
            orchestrator.run_task(orchestrator.next_task())
        st = {r["stage"]: r["state"] for r in db.query("SELECT stage, state FROM tasks")}
        self.assertEqual(st, {"LEGENDAS": "PENDENTE", "FINALIZACAO": "AGUARDANDO_REEXECUCAO", "QC": "AGUARDANDO_REEXECUCAO"})


class Diversos(Base):
    def test_palavra_fora_de_turno_vai_para_turno_mais_proximo(self):
        turns = [(0.0, 2.0, 0), (3.0, 5.0, 1)]
        self.assertEqual(transcribe._speaker_for(2.2, 2.4, turns), 0)
        self.assertEqual(transcribe._speaker_for(2.8, 2.9, turns), 1)
        self.assertIsNone(transcribe._speaker_for(9.0, 9.5, turns))

    def test_publicado_sem_identificador_nao_conta(self):
        db.insert("exports", {"id": "e1", "cut_id": "c1", "folder": str(config.PROD), "platform": "p"})

        class Fake:
            name = "fake"
            def publish(self, e): return {"state": "PUBLICADO"}
        with mock.patch.dict(publish.ADAPTERS, {"manual": Fake}):
            r = publish.publish("e1")
        self.assertEqual(r["state"], "VERIFICAR_NA_PLATAFORMA")

    def test_estatisticas_respeitam_fuso(self):
        decisions.decide("opportunity", self.opportunity(), 50)
        since_utc = (config.now() - config.timedelta(minutes=1)).astimezone(__import__("datetime").timezone.utc).isoformat()
        self.assertEqual(decisions.stats("opportunity", since_utc)["avaliados"], 1)

    def test_config_valida_tipos(self):
        with self.assertRaises(ValueError):
            config.save({"max_tentativas": "3"})
        config.save({"max_tentativas": 4})
        self.assertEqual(config.load()["max_tentativas"], 4)


class Servidor(Base):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        p = config.DIRS["intermediarios"] / "previas" / "c1.mp4"
        p.parent.mkdir(parents=True)
        p.write_bytes(bytes(range(256)) * 4)
        db.insert("cuts", {"id": "c1", "video_id": "v", "state": "AGUARDANDO_APROVACAO", "preview_path": str(p)})

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, path, headers=None, data=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_host_estranho_bloqueado(self):
        self.assertEqual(self.req("/api/dashboard", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("/api/stats")[0], 200)

    def test_range(self):
        code, body, h = self.req("/media/preview/c1", {"Range": "bytes=10-19"})
        self.assertEqual((code, len(body), h["Content-Range"]), (206, 10, "bytes 10-19/1024"))
        self.assertEqual(self.req("/media/preview/c1", {"Range": "bytes=5000-"})[0], 416)
        self.assertEqual(len(self.req("/media/preview/c1", {"Range": "bytes=1000-99999"})[1]), 24)

    def test_post_sem_cabecalho_proprio_recusado(self):
        body = json.dumps({"kind": "cut", "id": "c1", "score": 0}).encode()
        self.assertEqual(self.req("/api/decide", {"Content-Type": "application/json"}, body)[0], 403)
        ok = self.req("/api/decide", {"Content-Type": "application/json", "X-Cortes": "1"}, body)
        self.assertEqual(ok[0], 200)


class Madrugada(Base):
    def test_limite_da_assinatura_espera_sem_gastar_tentativa(self):
        from cortes import llm
        orchestrator.enqueue("ANALISE", "video", "v1")
        boom = mock.Mock(side_effect=llm.LLMQuotaError("agente 03: Claude AI usage limit reached"))
        for _ in range(5):  # muito além do limite de tentativas
            with db.tx() as c:
                c.execute("UPDATE tasks SET not_before=NULL")
            with mock.patch.dict(orchestrator.HANDLERS, {"ANALISE": boom}):
                orchestrator.run_task(orchestrator.next_task())
        t = db.query("SELECT * FROM tasks")[0]
        self.assertEqual((t["state"], t["attempts"]), ("PENDENTE", 0))
        self.assertIn("limite de uso", t["error"])
        self.assertGreater(t["not_before"], config.iso())
