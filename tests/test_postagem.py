"""Marcações Postado TikTok/Instagram, limpeza no dia seguinte e pedido de assuntos (10/11/2026)."""
import os
from unittest import mock

from cortes import acquire, config, db, nuvem, orchestrator, pos_postagem, research

from .base import Base


class Postagem(Base):
    def _export(self, eid="e1"):
        f = config.DIRS["produzidos"] / "2026-11-10" / "12-00"
        f.mkdir(parents=True, exist_ok=True)
        for n in ("video.mp4", "titulo.txt", "legenda.srt", "descricao.txt", "metadata.json"):
            (f / n).write_text("x" * 10, "utf-8")
        db.insert("exports", {"id": eid, "cut_id": "c_" + eid, "video_path": str(f / "video.mp4"), "folder": str(f),
                              "platform": "manual", "state": "PRONTO_PARA_PUBLICAR"})
        return f

    def test_so_apaga_no_dia_seguinte_depois_das_duas_marcacoes(self):
        f = self._export()
        os.environ["CORTES_FAKE_NOW"] = "2026-11-10T10:00:00-03:00"
        pos_postagem.mark("e1", "tiktok", "https://tiktok.com/x")
        self.assertIsNone(pos_postagem.due_date("e1"), "uma rede só não basta (padrão: ambas)")
        pos_postagem.mark("e1", "instagram")
        self.assertEqual(pos_postagem.due_date("e1"), "2026-11-11")
        pos_postagem.sweep()
        self.assertTrue((f / "video.mp4").exists(), "no mesmo dia o vídeo fica")
        os.environ["CORTES_FAKE_NOW"] = "2026-11-11T00:30:00-03:00"
        self.assertEqual(pos_postagem.sweep()["apagados"], 1)
        self.assertFalse((f / "video.mp4").exists())
        for n in ("titulo.txt", "legenda.srt", "descricao.txt", "metadata.json"):
            self.assertTrue((f / n).exists(), f"{n} nunca é apagado")
        self.assertTrue(db.get("exports", "e1")["video_deleted_at"])
        self.assertEqual(pos_postagem.sweep()["apagados"], 0, "idempotente")

    def test_desmarcar_cancela_a_exclusao(self):
        f = self._export()
        os.environ["CORTES_FAKE_NOW"] = "2026-11-10T10:00:00-03:00"
        pos_postagem.mark("e1", "tiktok")
        pos_postagem.mark("e1", "instagram")
        pos_postagem.mark("e1", "instagram", desfazer=True)
        os.environ["CORTES_FAKE_NOW"] = "2026-11-12T10:00:00-03:00"
        pos_postagem.sweep()
        self.assertTrue((f / "video.mp4").exists())
        self.assertEqual(db.query("SELECT state FROM publications WHERE export_id='e1'")[0]["state"],
                         "POSTADO_PELO_USUARIO")

    def test_qualquer_rede_quando_configurado(self):
        self._export()
        cfg = config.load()
        cfg["pos_postagem"]["exigir"] = "qualquer"
        config.save(cfg)
        os.environ["CORTES_FAKE_NOW"] = "2026-11-10T10:00:00-03:00"
        pos_postagem.mark("e1", "tiktok")
        self.assertEqual(pos_postagem.due_date("e1"), "2026-11-11")

    def test_nunca_apaga_fora_de_produzidos(self):
        out = config.PROD / "fora.mp4"
        out.write_text("x", "utf-8")
        db.insert("exports", {"id": "e2", "cut_id": "c2", "video_path": str(out), "folder": str(config.PROD)})
        os.environ["CORTES_FAKE_NOW"] = "2026-11-10T10:00:00-03:00"
        pos_postagem.mark("e2", "tiktok")
        pos_postagem.mark("e2", "instagram")
        os.environ["CORTES_FAKE_NOW"] = "2026-11-12T10:00:00-03:00"
        pos_postagem.sweep()
        self.assertTrue(out.exists())

    def test_nuvem_com_falha_mantem_o_video(self):
        f = self._export()
        cfg = config.load()
        cfg["nuvem"].update({"habilitada": True, "rclone_remote": "teste"})
        config.save(cfg)
        os.environ["CORTES_FAKE_NOW"] = "2026-11-10T10:00:00-03:00"
        pos_postagem.mark("e1", "tiktok")
        pos_postagem.mark("e1", "instagram")
        os.environ["CORTES_FAKE_NOW"] = "2026-11-11T10:00:00-03:00"
        with mock.patch.object(nuvem, "upload", side_effect=RuntimeError("sem rede")):
            self.assertEqual(pos_postagem.sweep()["adiados_nuvem"], 1)
        self.assertTrue((f / "video.mp4").exists())
        with mock.patch.object(nuvem, "upload", return_value="teste:CentralDeCortes/x.mp4"):
            self.assertEqual(pos_postagem.sweep()["apagados"], 1)
        self.assertEqual(db.get("exports", "e1")["cloud_path"], "teste:CentralDeCortes/x.mp4")

    def test_plataforma_invalida(self):
        self._export()
        with self.assertRaises(ValueError):
            pos_postagem.mark("e1", "orkut")


class Pedido(Base):
    def test_pedido_vai_para_a_fila_com_campos_opcionais(self):
        item = orchestrator.request_research("Canal A, Canal B", "live", 5, 1_000_000)
        t = db.query("SELECT payload FROM tasks WHERE item_id=?", (item,))[0]
        self.assertEqual(t["payload"]["pedido"]["quantidade"], 5)
        item2 = orchestrator.request_research("", "podcast")
        self.assertIsNone(db.query("SELECT payload FROM tasks WHERE item_id=?", (item2,))[0]["payload"]["pedido"]["quantidade"])

    def test_termos_do_pedido(self):
        self.assertEqual(research.pedido_terms({"tema": "Canal A, Canal B; Canal C"}), ["Canal A", "Canal B", "Canal C"])
        self.assertEqual(research.pedido_terms({"tema": "", "categoria": "podcast"}), ["podcast"])

    def test_filtro_de_visualizacoes_e_duracao(self):
        fake = [{"url": "u1", "title": "a", "duration": 3600, "view_count": 2_000_000},
                {"url": "u2", "title": "b", "duration": 3600, "view_count": 10_000},
                {"url": "u3", "title": "c", "duration": 50_000, "view_count": 9_000_000}]
        with mock.patch.object(research, "youtube_search", return_value=fake), \
                mock.patch.object(research, "_channel_entries", return_value=(None, [])):
            out = research.collect(config.load(), {"tema": "Canal A", "min_visualizacoes": 1_000_000})
        self.assertEqual([c["url"] for c in out["candidatos"]], ["u1"])
        self.assertEqual(out["tendencias"], [], "pedido não mistura tendências")

    def test_autorizacao_de_qualquer_canal_fica_registrada(self):
        cfg = config.load()
        self.assertIsNone(acquire.authorization_basis({"channel_id": "x"}, cfg))
        cfg["autorizacao"].update({"autorizar_qualquer_canal": True, "autorizado_em": "10/11/2026"})
        self.assertIn("autorizado_pelo_usuario", acquire.authorization_basis({"channel_id": "x"}, cfg))


class Baixado(Base):
    def test_baixar_marca_video_baixado(self):
        from cortes import web
        Postagem._export(self, "e9")
        self.assertIsNone(next(r for r in web.ready_list() if r["id"] == "e9")["baixado_em"])
        web.mark_downloaded("e9")
        web.mark_downloaded("e9")
        r = next(r for r in web.ready_list() if r["id"] == "e9")
        self.assertTrue(r["baixado_em"])
        self.assertEqual(r["vezes_baixado"], 2)
