"""Vídeos de Página."""
import json
from unittest import mock
from pathlib import Path

from cortes import config, db, media, orchestrator, pagina, supervisors

from .base import Base

CANDS = [{"url": f"https://www.youtube.com/shorts/abc{i}xyz", "titulo": f"vídeo {i}", "canal": "canal",
          "visualizacoes": 900000, "duracao_s": 8} for i in range(3)]
HUNT = {"observacoes": "ok", "descartados": [{"url": CANDS[2]["url"], "motivo": "repetido"}],
        "escolhidos": [{"url": c["url"], "motivo": "engraçado", "nota_humor": 8, "riscos": [],
                        "piadas": ["eu quando a mãe chama kkkkk", "b", "c"]} for c in CANDS[:2]]
        + [{"url": "https://www.youtube.com/shorts/inventada", "motivo": "x", "nota_humor": 5, "riscos": [], "piadas": ["a", "b"]}]}
MOUNT = {"seguro": True, "motivo_inseguro": "", "o_que_acontece": "um gato pula", "enquadramento": "recorte", "alternativas": ["x", "y"],
         "legenda_tela": "eu tentando sair da cama na segunda feira de manhã depois de um feriado prolongado kkkkk 😂",
         "descricao_post": "segunda chegou", "hashtags": ["humor", "#memes", "#gato"], "justificativa": "melhor"}


def fake_source(d, w=1080, h=1920, dur=8):
    f = d / "fonte.mp4"
    media.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s={w}x{h}:r=30:d={dur}", "-f", "lavfi",
               "-i", f"sine=f=440:d={dur}", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(f)], 300)
    return f


class Pagina(Base):
    def run_all(self):
        while (t := orchestrator.next_task()):
            orchestrator.run_task(t)

    def fake_download(self, task):
        pv = db.get("page_videos", task["item_id"])
        src = fake_source(pagina.work(pv["id"]), *((1280, 720) if pv["source_url"].endswith("0xyz") else (1080, 1920)))
        pr = media.probe(src)
        return {"titulo": pv["title"], "canal": "canal", "canal_url": "", "descricao_fonte": "", "tags": [],
                "visualizacoes": 1, "curtidas": 1, "duracao": pr["duration"], "probe": pr, "arquivo": str(src),
                "bytes": src.stat().st_size, "sha256": media.sha256(src) + pv["id"]}

    def test_lote_de_teste_vai_do_inicio_ao_fim_sozinho_e_libera_o_foco(self):
        lote = pagina.request(5, auto_aprovar=True, foco=True)
        orchestrator.enqueue("AQUISICAO", "opportunity", "outra")  # produção normal fica parada no foco
        self.assertEqual(orchestrator.next_task()["stage"], "PG_BUSCA")

        def llm(agent, prompt, schema, **kw):
            return json.loads(json.dumps(HUNT if agent == "11-cacador" else MOUNT))
        with mock.patch.object(pagina, "collect", return_value=CANDS), mock.patch("cortes.llm.call_agent", side_effect=llm), \
                mock.patch.dict(orchestrator.HANDLERS, {"PG_DOWNLOAD": self.fake_download}):
            for _ in range(20):
                t = orchestrator.next_task()
                if not t or not t["stage"].startswith("PG_"):
                    break
                orchestrator.run_task(t)
        rows = db.query("SELECT * FROM page_videos ORDER BY source_url")
        self.assertEqual([r["state"] for r in rows], ["PRONTO", "PRONTO"], [r["state"] for r in rows])
        self.assertNotIn("inventada", " ".join(r["source_url"] for r in rows), "URL inventada é ignorada")
        for r in rows:
            p = media.probe(r["final_path"])
            self.assertEqual((p["video"]["width"], p["video"]["height"]), (1080, 1920))
            self.assertNotIn("😂", r["caption"])
            self.assertIn("Créditos: canal", r["description"])
            self.assertIn("automática", r["approval2"])
            self.assertTrue(db.get("exports", "exp_" + r["id"]))
        modos = {r["source_url"][-4:]: r["meta"]["montagem"]["modo_video"] for r in rows}
        # deitado (1280x720) perderia 66% no recorte: encaixa; em pé (9:16) preenche a área
        self.assertEqual(modos, {"0xyz": "encaixe_com_fundo_desfocado", "1xyz": "recorte"})
        self.assertEqual(config.load()["foco"], "", "foco liberado ao fim do lote")
        self.assertEqual(orchestrator.next_task()["stage"], "AQUISICAO")
        logf = config.DIRS["logs"] / "pagina" / f"{lote}.jsonl"
        evs = [json.loads(x)["evento"] for x in logf.read_text("utf-8").splitlines()]
        for must in ("agente 11 respondeu", "supervisor: APROVADO_COM_RESSALVAS", "montado", "qc concluído",
                     "pronto para postar", "lote encerrado; produção normal retomada"):
            self.assertIn(must, evs)
        from cortes import web
        self.assertFalse([r for r in web.ready_list() if r["id"].startswith("exp_pg")], "não mistura com os cortes")

    def test_sem_auto_espera_suas_duas_aprovacoes(self):
        pagina.request(2)
        with mock.patch.object(pagina, "collect", return_value=CANDS), \
                mock.patch("cortes.llm.call_agent", return_value=json.loads(json.dumps(HUNT))):
            orchestrator.run_task(orchestrator.next_task())
        rows = db.query("SELECT * FROM page_videos")
        self.assertEqual({r["state"] for r in rows}, {"AGUARDANDO_APROVACAO"})
        self.assertIsNone(orchestrator.next_task(), "nada baixa sem você aprovar")
        pagina.decide(rows[0]["id"], 100)
        pagina.decide(rows[1]["id"], -100)
        self.assertEqual(orchestrator.next_task()["stage"], "PG_DOWNLOAD")
        self.assertEqual(db.get("page_videos", rows[1]["id"])["state"], "DESCARTADO")

    def test_piada_longa_quebra_em_linhas(self):
        size, lines = pagina.wrap("eu " * 40)
        self.assertLessEqual(len(lines), 3)
        self.assertEqual(pagina.wrap("curta kkkk"), (52, ["curta kkkk"]))

    def test_falha_vai_para_o_log_do_lote(self):
        lote = pagina.request(1)
        with mock.patch.object(pagina, "collect", return_value=[]):
            orchestrator.run_task(orchestrator.next_task())
        txt = (config.DIRS["logs"] / "pagina" / f"{lote}.jsonl").read_text("utf-8")
        self.assertIn("supervisor: REPROVADO", txt)
        self.assertIn("nenhum vídeo curto", txt)


class Reserva(Base):
    def test_reserva_entra_no_lugar_do_descartado(self):
        lote = pagina.request(1, auto_aprovar=True)
        hunt = json.loads(json.dumps(HUNT))
        with mock.patch.object(pagina, "collect", return_value=CANDS), mock.patch("cortes.llm.call_agent", return_value=hunt):
            orchestrator.run_task(orchestrator.next_task())
        st = {r["source_url"][-4:]: r["state"] for r in db.query("SELECT * FROM page_videos")}
        self.assertEqual(st, {"0xyz": "APROVADO", "1xyz": "RESERVA"})
        first = db.query("SELECT id FROM page_videos WHERE state='APROVADO'")[0]["id"]
        db.update("page_videos", first, {"state": "DESCARTADO"})
        db.query("UPDATE tasks SET state='CONCLUIDO' WHERE stage='PG_DOWNLOAD'")
        pagina.maybe_release_focus()
        self.assertEqual(db.query("SELECT state FROM page_videos WHERE source_url LIKE '%1xyz'")[0]["state"], "APROVADO")
        self.assertIn("reserva promovida", (config.DIRS["logs"] / "pagina" / f"{lote}.jsonl").read_text("utf-8"))


_mini = mock.patch.object(pagina, "_miniaturas", return_value=0)  # testes não acessam a internet
_enr = mock.patch.object(pagina, "enrich", side_effect=lambda cands, c, lote, n=None: cands)


def setUpModule():
    _mini.start()
    _enr.start()


def tearDownModule():
    _mini.stop()
    _enr.stop()


class Engajamento(Base):
    def test_ordena_por_engajamento_e_filtra(self):
        c = dict(config.load()["pagina"], min_comentarios=50)
        cands = [{"url": f"u{i}", "visualizacoes": 1_000_000 - i} for i in range(4)]
        det = {"u0": {"curtidas": 10_000, "comentarios": 60, "duracao_s": 20},    # 10,3/mil
               "u1": {"curtidas": 40_000, "comentarios": 900, "duracao_s": 20},   # 44,5/mil
               "u2": {"curtidas": 90_000, "comentarios": 10, "duracao_s": 20},    # poucos comentários: fora
               "u3": {"curtidas": 90_000, "comentarios": 900, "duracao_s": 400}}  # longo demais: fora
        _enr.stop()
        try:
            with mock.patch.object(pagina, "_detalhes", side_effect=lambda u: dict(det[u])):
                out = pagina.enrich([dict(x) for x in cands], c, "lote_t")
        finally:
            _enr.start()
        self.assertEqual([x["url"] for x in out], ["u1", "u0"])
        self.assertEqual(out[0]["engajamento_por_mil"], 44.5)
        self.assertEqual(pagina.engajamento(1000, None, None), None)


class IaRepost(Base):
    """Pedido do usuário: IA ou muito repostado só se for MUITO viral; senão prioriza original sem IA."""

    def test_regra(self):
        c = config.load()["pagina"]
        self.assertIsNone(pagina.barrado_ia_repost({"parece_ia": False, "repost": "pouco"}, 400_000, c))
        self.assertIn("feito por IA", pagina.barrado_ia_repost({"parece_ia": True, "repost": "original"}, 900_000, c))
        self.assertIn("muito repostado", pagina.barrado_ia_repost({"parece_ia": False, "repost": "muito"}, 2_000_000, c))
        self.assertIsNone(pagina.barrado_ia_repost({"parece_ia": True, "repost": "muito"}, 8_000_000, c, 30, 20))
        self.assertIn("insuficiente", pagina.barrado_ia_repost({"parece_ia": True}, 8_000_000, c, 12, 10))
        self.assertIn("fora do topo", pagina.barrado_ia_repost({"parece_ia": True}, 8_000_000, c, 20, 20))
        self.assertIn("sem engajamento", pagina.barrado_ia_repost({"repost": "muito"}, 8_000_000, c, None, 20))
        self.assertIsNone(pagina.barrado_ia_repost({"parece_ia": False, "repost": "pouco"}, 1000, c, 1, 20))
        self.assertEqual(pagina.topo_engajamento([{"engajamento_por_mil": v} for v in range(1, 9)], 25), 7)

    def test_canal_com_regra_de_ia(self):
        c = dict(config.load()["pagina"], canais_regra_ia=["Canal Isca"])
        x = {"canal": "canal isca ", "parece_ia": False, "repost": "original"}
        self.assertIn("canal com regra de IA", pagina.barrado_ia_repost(x, 2_000_000, c, 32, 19))
        self.assertIsNone(pagina.barrado_ia_repost(x, 6_000_000, c, 32, 19), "muito viral e no topo: entra")
        self.assertIsNone(pagina.barrado_ia_repost({"canal": "Outro"}, 2_000_000, c, 12, 19))

    def test_origem(self):
        self.assertEqual(pagina.origem({"titulo": "quando minha mãe chama kkkkk"}), "br")
        self.assertEqual(pagina.origem({"titulo": "When my dog sees the vet lol"}), "gringo")
        self.assertEqual(pagina.origem({"titulo": "cuando mi hermano jajaja"}), "gringo")
        self.assertEqual(pagina.origem({"titulo": None}), "incerto")
        self.assertEqual(pagina.origem({"titulo": "Bro really froze up 😭🙏"}), "gringo")
        self.assertEqual(pagina.origem({"titulo": "Essa piscina tá diferente😂 #humor"}), "br")
        self.assertEqual(pagina.origem({"titulo": "x", "idioma": "pt-BR"}), "br")

    def test_cacador_prioriza_brasileiro_e_barra_ia_e_gringo_fracos(self):
        u = lambda s: f"https://www.youtube.com/shorts/{s}"  # noqa: E731
        A = {"url": u("A"), "titulo": "eu quando minha mãe chama kkkkk", "visualizacoes": 900_000, "engajamento_por_mil": 40}
        B = {"url": u("B"), "titulo": "quando o pai chega kkkk", "visualizacoes": 9_000_000, "engajamento_por_mil": 60}
        C = {"url": u("C"), "titulo": "minha vó no zap kkkk", "visualizacoes": 600_000, "engajamento_por_mil": 30}
        D = {"url": u("D"), "titulo": "When my dog sees the vet lol", "visualizacoes": 700_000, "engajamento_por_mil": 20}
        E = {"url": u("E"), "titulo": "não aguento mais kkkk", "visualizacoes": 7_000_000, "engajamento_por_mil": 5}
        esc = lambda c, ia, rp: {"url": c["url"], "motivo": "x", "nota_humor": 8, "riscos": [], "piadas": ["a", "b"],  # noqa: E731
                                 "parece_ia": ia, "repost": rp, "sinais_ia_repost": "s", "brasileiro": True}
        hunt = {"observacoes": "", "descartados": [],
                "escolhidos": [esc(A, True, "original"), esc(B, True, "muito"), esc(C, False, "original"),
                               esc(D, False, "original"), esc(E, False, "original")]}
        lote = pagina.request(2)
        seen = {}

        def llm(agent, prompt, schema, **kw):
            seen["lista"] = Path(prompt.split(" em ")[1].split(" (")[0]).read_text("utf-8")
            return json.loads(json.dumps(hunt))
        with mock.patch.object(pagina, "collect", return_value=[dict(x) for x in (A, B, C, D, E)]), \
                mock.patch("cortes.llm.call_agent", side_effect=llm):
            orchestrator.run_task(orchestrator.next_task())
        urls = [r["source_url"] for r in db.query("SELECT source_url FROM page_videos ORDER BY rowid")]
        self.assertEqual(urls, [C["url"], B["url"]], "IA pouco viral fora; original BR antes do IA viral no topo")
        self.assertNotIn(D["url"], seen["lista"], "gringo abaixo de 2× a mediana nem chega ao caçador")
        self.assertNotIn(E["url"], seen["lista"], "abaixo do piso geral nem chega ao caçador")
        self.assertEqual(len(seen["lista"].strip().splitlines()), 5, "uma linha por candidato")
        log = (config.DIRS["logs"] / "pagina" / f"{lote}.jsonl").read_text("utf-8")
        self.assertIn("notas do lote", log)
        self.assertIn("barrados pelas regras", log)

    def test_montador_descarta_ia_visto_nos_quadros(self):
        lote = pagina.request(1, auto_aprovar=True)
        mount = dict(MOUNT, parece_ia=True, repost="original", sinais_ia_repost="rosto derretendo")

        def llm(agent, prompt, schema, **kw):
            return json.loads(json.dumps(HUNT if agent == "11-cacador" else mount))
        with mock.patch.object(pagina, "collect", return_value=CANDS), mock.patch("cortes.llm.call_agent", side_effect=llm), \
                mock.patch.dict(orchestrator.HANDLERS, {"PG_DOWNLOAD": Pagina.fake_download.__get__(self)}):
            for _ in range(10):
                t = orchestrator.next_task()
                if not t or not t["stage"].startswith("PG_"):
                    break
                orchestrator.run_task(t)
        st = [r["state"] for r in db.query("SELECT state FROM page_videos")]
        self.assertNotIn("PRONTO", st)
        self.assertIn("regra IA/repost", (config.DIRS["logs"] / "pagina" / f"{lote}.jsonl").read_text("utf-8"))
