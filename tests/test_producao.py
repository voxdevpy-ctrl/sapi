"""Legendas, render, QC, supervisores, máquina de estados, retomada e idempotência (sem IA/rede)."""
import json
from pathlib import Path
from unittest import mock

from cortes import acquire, config, cuts, db, orchestrator, publish, qc, render, storage, subtitles, supervisors

from .base import Base, make_video

UTTS = [
    {"id": 0, "speaker": "P1", "start": 0.5, "end": 3.0, "low_conf": False, "text": "Como você começou?",
     "words": [{"w": " Como", "s": 0.5, "e": 0.9, "p": 0.9}, {"w": " você", "s": 1.0, "e": 1.4, "p": 0.9},
               {"w": " começou?", "s": 1.5, "e": 3.0, "p": 0.9}]},
    {"id": 1, "speaker": "P2", "start": 6.0, "end": 7.8, "low_conf": False, "text": "Foi por acaso.",
     "words": [{"w": " Foi", "s": 6.0, "e": 6.4, "p": 0.95}, {"w": " por", "s": 6.5, "e": 6.8, "p": 0.95},
               {"w": " acaso.", "s": 6.9, "e": 7.8, "p": 0.3}]},
]


class Legendas(Base):
    def test_intervalos_removem_silencio_longo_e_mapeiam_tempo(self):
        ivs = cuts.keep_intervals(UTTS, 0.35, 8.0)
        # pergunta → resposta de outro locutor: fica meio segundo de pausa
        self.assertEqual(ivs, [[0.35, 3.275], [5.775, 8.0]])
        self.assertAlmostEqual(subtitles.map_time(6.0, ivs), 2.925 + 0.225, places=2)
        self.assertIsNone(subtitles.map_time(4.5, ivs))
        self.assertAlmostEqual(subtitles.map_time(4.5, ivs, snap=True), 2.925, places=3)

    def test_blocos_srt_validos_sem_sobreposicao(self):
        ivs = cuts.keep_intervals(UTTS, 0.35, 8.0)
        blocks = subtitles.build_blocks(UTTS, ivs, 32)
        self.assertEqual([b["speaker"] for b in blocks], ["P1", "P2"], "troca de locutor separa blocos")
        self.assertTrue(blocks[1]["low_conf"])
        p = config.PROD / "t.srt"
        subtitles.write_srt(blocks, p, 32)
        parsed = subtitles.parse_srt(p)
        self.assertEqual(len(parsed), 2)
        self.assertLessEqual(parsed[0]["end"], parsed[1]["start"])

    def test_srt_invalida_detectada(self):
        p = config.PROD / "ruim.srt"
        p.write_text("1\n00:00:01 --> 00:00:02\ntexto\n", "utf-8")
        with self.assertRaises(ValueError):
            subtitles.parse_srt(p)


class RenderQC(Base):
    def setUp(self):
        super().setUp()
        self.vid = self.video_record(make_video("render.mp4", 10, "1280x720"))
        storage.compress(self.vid)
        ivs = [[1.0, 4.0], [6.0, 9.0]]
        db.insert("cuts", {"id": "cutR", "video_id": self.vid, "start": 1.0, "end": 9.0, "state": "APROVADO",
                           "scores": {"intervalos": ivs}, "created_at": config.iso()})
        self.dir = config.DIRS["intermediarios"] / "final" / "cutR"
        self.dir.mkdir(parents=True)
        blocks = [{"i": 0, "start": 0.2, "end": 2.5, "text": "Primeira legenda de teste", "speaker": "P1"},
                  {"i": 1, "start": 3.2, "end": 5.5, "text": "Segunda legenda com destaque", "speaker": "P2", "destaque": "destaque"}]
        st = subtitles.style()
        subtitles.write_srt(blocks, self.dir / "legenda.srt", st["max_chars"])
        subtitles.write_ass(blocks, self.dir / "legenda.ass", st, 1080, 1920)

    def test_previa_render_e_qc(self):
        prev = cuts.make_preview("cutR")
        from cortes import media
        self.assertAlmostEqual(media.probe(prev)["duration"], 6.0, delta=0.35)
        out = render.render("cutR", self.dir / "legenda.ass")
        res = qc.check(Path(out["video"]), self.dir / "legenda.srt", 6.0, 1080, 1920, subtitles.style(),
                       media.probe(prev)["duration"])
        failed = {k: v for k, v in res["testes"].items() if not v["ok"]}
        # o tom senoidal contínuo não tem silêncio; tela de teste não é preta
        self.assertEqual(set(qc.REQUIRED_TESTS), set(res["testes"]))
        self.assertNotEqual(res["status"], "REPROVADO", failed)
        task = {"id": "tq", "stage": "QC", "item_id": "cutR"}
        rep = supervisors.conformidade(task, {"qc": res}, None)
        self.assertTrue(rep["pode_avancar"], rep)

    def test_qc_reprova_duracao_errada_e_supervisor_confere(self):
        out = render.render("cutR", self.dir / "legenda.ass")
        res = qc.check(Path(out["video"]), self.dir / "legenda.srt", 20.0, 1080, 1920, subtitles.style(), None)
        self.assertEqual(res["status"], "REPROVADO")
        self.assertIn("duracao", res["falhas"])
        forged = dict(res, status="APROVADO")  # agente "declara" aprovado com teste grave falho
        rep = supervisors.conformidade({"id": "t", "stage": "QC", "item_id": "cutR"}, {"qc": forged}, None)
        self.assertFalse(rep["pode_avancar"], "supervisor não aceita veredito incoerente")


class Supervisores(Base):
    def test_nome_sem_evidencia_reprovado(self):
        vid = self.video_record(make_video("ok.mp4"))
        tp = config.PROD / "tr.json"
        tp.write_text(json.dumps({"utterances": UTTS, "speakers": {"P1": {}, "P2": {}}, "duration": 8}), "utf-8")
        db.insert("transcripts", {"video_id": vid, "path": str(tp)})
        base = {"assuntos": [{"titulo": "x", "inicio_id": 0, "fim_id": 1}], "resumo": "", "momentos": [],
                "observacoes_audio_video": ""}
        bad = dict(base, participantes=[
            {"rotulo": "P1", "nome": "Carlos", "papel": "host", "evidencia_ids": [0], "fonte_evidencia": "", "tentativas": ""},
            {"rotulo": "P2", "nome": None, "papel": "convidado", "evidencia_ids": [], "fonte_evidencia": "",
             "tentativas": "sem apresentação; metadados sem nomes"}])
        task = {"id": "ta", "stage": "ANALISE", "item_id": vid}
        rep = supervisors.analise(task, {"llm": bad}, None)
        self.assertFalse(rep["pode_avancar"])
        self.assertTrue(any("Carlos" in p for p in rep["problemas"]))
        UT = [dict(UTTS[0], text="Eu sou o Carlos. Como você começou?"), UTTS[1]]
        tp.write_text(json.dumps({"utterances": UT, "speakers": {"P1": {}, "P2": {}}, "duration": 8}), "utf-8")
        self.assertTrue(supervisors.analise(task, {"llm": bad}, None)["pode_avancar"])

    def test_voz_sem_analise_reprovada(self):
        vid = self.video_record(make_video("ok.mp4"))
        tp = config.PROD / "tr.json"
        tp.write_text(json.dumps({"utterances": UTTS, "speakers": {"P1": {}, "P2": {}}, "duration": 8}), "utf-8")
        db.insert("transcripts", {"video_id": vid, "path": str(tp)})
        res = {"participantes": [{"rotulo": "P1", "nome": None, "papel": "", "evidencia_ids": [], "fonte_evidencia": "",
                                  "tentativas": "x"}], "assuntos": [{"titulo": "a", "inicio_id": 0, "fim_id": 1}],
               "resumo": "", "momentos": [], "observacoes_audio_video": ""}
        self.assertFalse(supervisors.analise({"id": "t", "stage": "ANALISE", "item_id": vid}, {"llm": res}, None)["pode_avancar"])

    def test_pesquisa_com_fonte_inventada(self):
        f = config.PROD / "col.json"
        f.write_text(json.dumps({"candidatos": [{"candidato_id": "c1", "canal": "a"}], "erros": []}), "utf-8")
        llm = {"oportunidades": [{"candidato_id": "c99", "evidencias": ["x"]}], "descartados": []}
        rep = supervisors.pesquisa({"id": "t", "stage": "PESQUISA", "item_id": "p"}, {"collect_file": str(f), "llm": llm, "proposals": []}, None)
        self.assertFalse(rep["pode_avancar"])

    def test_erro_do_agente_nunca_avanca(self):
        rep = supervisors.audit({"id": "t", "stage": "FINALIZACAO", "item_id": "c"}, None, "MediaError: boom")
        self.assertEqual(rep["status"], "REPROVADO")
        self.assertIn("STATUS: REPROVADO", supervisors.render_text(rep))


class Disponibilidade(Base):
    def test_fonte_local_inexistente_bloqueada(self):
        r = acquire.check_availability("local:C:/nao/existe.mp4")
        self.assertFalse(r["ok"])

    def test_fonte_local_valida(self):
        p = make_video("longo.mp4", 70)
        r = acquire.check_availability(f"local:{p}")
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(r["authorization"], "conteudo_local_fornecido_pelo_usuario")

    def test_fonte_curta_ou_sem_autorizacao_bloqueada(self):
        self.assertFalse(acquire.check_availability(f"local:{make_video('ok.mp4')}")["ok"], "8s é curto demais")
        cfg = config.load()
        self.assertIsNone(acquire.authorization_basis({"channel_id": "x", "license": None}, cfg))
        self.assertIn("creative_commons", acquire.authorization_basis(
            {"channel_id": "x", "license": "Creative Commons Attribution license (reuse allowed)"}, cfg))

    def test_metadados_remotos_problematicos(self):
        info = {"title": "Outro vídeo", "duration": 3000, "channel_id": "x", "license": None, "availability": "needs_auth",
                "live_status": "is_live", "formats": [{"vcodec": "avc1", "acodec": "none", "has_drm": True}]}

        class FakeYDL:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def extract_info(self, *a, **k): return info
        with mock.patch("yt_dlp.YoutubeDL", FakeYDL):
            r = acquire.check_availability("https://exemplo.invalid/v", "Entrevista completa sobre IA")
        txt = " ".join(r["problems"])
        for frag in ("transmissão", "restrito", "DRM", "áudio", "título", "autorização"):
            self.assertIn(frag, txt)
        self.assertFalse(r["ok"])


class Orquestrador(Base):
    def test_falha_repete_com_feedback_e_bloqueia(self):
        oid = self.opportunity()
        orchestrator.enqueue("VERIFICACAO", "opportunity", oid)
        with mock.patch.dict(orchestrator.HANDLERS, {"VERIFICACAO": mock.Mock(side_effect=RuntimeError("rede caiu"))}):
            for _ in range(config.load()["max_tentativas"]):
                t = db.query("SELECT * FROM tasks WHERE item_id=?", (oid,))[0]
                db.update("tasks", t["id"], {"not_before": None})
                orchestrator.run_task(db.get("tasks", t["id"]))
        t = db.query("SELECT * FROM tasks WHERE item_id=?", (oid,))[0]
        self.assertEqual(t["state"], "BLOQUEADO")
        self.assertEqual(db.get("opportunities", oid)["state"], "BLOQUEADO")
        self.assertIn("rede caiu", t["error"])
        self.assertEqual(len(db.query("SELECT * FROM reports WHERE task_id=?", (t["id"],))), 3)

    def test_retomada_apos_queda(self):
        orchestrator.enqueue("TRANSCRICAO", "video", "v1")
        with db.tx() as c:
            c.execute("UPDATE tasks SET state='EM_EXECUCAO'")
        self.assertEqual(orchestrator.recover(), 1)
        t = orchestrator.next_task()
        self.assertEqual((t["stage"], t["attempts"], t["payload"]["interrupcoes"]), ("TRANSCRICAO", 0, 1))

    def test_qc_reprovado_volta_para_finalizacao_e_qc_reexecuta(self):
        db.insert("cuts", {"id": "c1", "video_id": "v", "state": "APROVADO"})
        orchestrator.enqueue("FINALIZACAO", "cut", "c1")
        orchestrator.enqueue("QC", "cut", "c1")
        with db.tx() as c:
            c.execute("UPDATE tasks SET state='CONCLUIDO' WHERE stage='FINALIZACAO'")
        qc_task = db.query("SELECT * FROM tasks WHERE stage='QC'")[0]
        bad = {"status": "REPROVADO", "testes": {k: {"ok": k != "duracao", "detalhe": "", "grave": True} for k in qc.REQUIRED_TESTS},
               "falhas": ["duracao"], "ressalvas": []}
        with mock.patch.dict(orchestrator.HANDLERS, {"QC": lambda t: {"qc": bad}}):
            orchestrator.run_task(qc_task)
        states = {r["stage"]: r["state"] for r in db.query("SELECT stage, state FROM tasks")}
        self.assertEqual(states, {"FINALIZACAO": "PENDENTE", "QC": "AGUARDANDO_REEXECUCAO"})
        orchestrator.enqueue("QC", "cut", "c1")  # o que on_success(FINALIZACAO) faz
        self.assertEqual(db.query("SELECT state FROM tasks WHERE stage='QC'")[0]["state"], "PENDENTE")

    def test_publicacao_idempotente_e_honesta(self):
        db.insert("exports", {"id": "e1", "cut_id": "c1", "folder": str(config.PROD), "platform": "youtube_shorts"})
        r1 = publish.publish("e1")
        self.assertEqual(r1["state"], "PRONTO_PARA_PUBLICAR", "sem integração não alega publicação")
        publish.publish("e1")
        self.assertEqual(len(db.query("SELECT * FROM publications")), 1)
        db.update("publications", r1["publication_id"], {"state": "ENVIANDO"})  # queda durante envio
        r3 = publish.publish("e1")
        self.assertIn("conferir", r3["skipped"])
        rep = supervisors.publicacao({"id": "t", "stage": "PUBLICACAO", "item_id": "c1"}, {"publication_id": r1["publication_id"]}, None)
        self.assertTrue(rep["pode_avancar"])

    def test_agenda_nao_repete_horario(self):
        s1 = publish.next_slot()
        db.insert("exports", {"id": "e1", "cut_id": "c1", "scheduled_for": config.iso(s1)})
        self.assertGreater(publish.next_slot(), s1)
