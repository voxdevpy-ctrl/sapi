"""Regressões das correções feitas a partir dos logs reais (09/11/2026)."""
from pathlib import Path

from cortes import config, db, orchestrator, storage, subtitles

from .base import Base, make_video


def utt(i, s, e, txt):
    return {"id": i, "speaker": "P1", "start": s, "end": e, "low_conf": False, "text": txt,
            "words": [{"w": " " + txt, "s": s, "e": e, "p": 0.9}]}


class Ajustes(Base):
    def test_ultima_legenda_nunca_passa_do_fim(self):
        # caso real: última palavra curta perto do fim era esticada para 0,7 s e passava do vídeo
        ivs = [[0.0, 10.0]]
        blocks = subtitles.build_blocks([utt(0, 1.0, 3.0, "frase"), utt(1, 9.8, 9.95, "Isso")], ivs, 28)
        self.assertTrue(all(b["end"] <= 9.95 for b in blocks), blocks)
        self.assertTrue(all(b["start"] < b["end"] for b in blocks))

    def test_compressao_pulada_quando_original_ja_eficiente(self):
        vid = self.video_record(make_video("ok.mp4"))
        cfg = config.load()
        cfg["compressao"]["pular_abaixo_mbps"] = 1000.0  # força o caminho "já eficiente"
        config.save(cfg)
        out = storage.compress(vid)
        v = db.get("videos", vid)
        self.assertTrue(out["skipped"])
        self.assertEqual(v["compressed_path"], v["original_path"], "usa o próprio original validado")
        self.assertTrue(Path(v["compressed_path"]).exists())
        self.assertIn("compressão pulada", v["compression_note"])
        self.assertEqual(storage.finalize_compression(vid), 0, "nada é apagado")

    def test_estrangeiro_so_se_muito_viral_e_limitado(self):
        cfg = config.load()
        mk = lambda i, lang, views: ({"topic": f"t{i}", "source_url": f"u{i}", "language": lang, "reason": "r"},
                                     {"url": f"u{i}", "visualizacoes": views})
        items = [mk(1, "pt", 10), mk(2, "en", 50_000), mk(3, "en", 3_000_000), mk(4, "en", 2_000_000),
                 mk(5, "es", 5_000_000), mk(6, None, 0)]
        by_id = {f"c{i}": c for i, (_, c) in enumerate(items)}
        keep, barred = orchestrator.filter_language([p for p, _ in items], by_id, cfg)
        topics = [p["topic"] for p in keep]
        self.assertIn("t1", topics)
        self.assertIn("t6", topics, "idioma desconhecido não é barrado (será detectado no áudio)")
        self.assertNotIn("t2", topics, "estrangeiro pouco visto é barrado")
        self.assertEqual(sorted(t for t in topics if t in ("t3", "t4", "t5")), ["t3", "t5"], "no máximo 2, os mais vistos")
        self.assertEqual(len(barred), 2)
