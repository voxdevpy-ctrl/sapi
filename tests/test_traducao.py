"""Tradução das legendas de vídeos estrangeiros para português."""
from cortes import config, orchestrator, subtitles, supervisors

from .base import Base


def blk(i, s, e, t):
    return {"i": i, "start": s, "end": e, "text": t, "speaker": "P1", "low_conf": False}


class Traducao(Base):
    def test_precisa_traduzir_so_idioma_estrangeiro(self):
        self.assertTrue(orchestrator.needs_translation("en"))
        self.assertFalse(orchestrator.needs_translation("pt"))
        self.assertFalse(orchestrator.needs_translation(None))
        cfg = config.load()
        cfg["traducao"]["habilitada"] = False
        config.save(cfg)
        self.assertFalse(orchestrator.needs_translation("en"))

    def test_fit_divide_bloco_longo_preservando_sincronia(self):
        longo = "Eu acho que esse medo é compreensível, mas sinceramente exagerado para a maioria das pessoas hoje"
        out = subtitles.fit([blk(0, 1.0, 5.0, longo), blk(1, 5.2, 6.0, "Exatamente.")], 28)
        partes = [b for b in out if b["src_i"] == 0]
        self.assertGreater(len(partes), 1)
        self.assertEqual((partes[0]["start"], partes[-1]["end"]), (1.0, 5.0), "início e fim originais preservados")
        self.assertTrue(all(len(b["text"]) <= 56 for b in out))
        self.assertTrue(all(a["end"] <= b["start"] for a, b in zip(out, out[1:])))
        self.assertEqual([b["i"] for b in out], list(range(len(out))))
        self.assertEqual(" ".join(b["text"] for b in partes), longo, "nenhuma palavra perdida")

    def _out(self, base, edited, review=None, missing=()):
        final = subtitles.fit(edited, 28)
        p = config.PROD / "t.srt"
        subtitles.write_srt(final, p, 28)
        return {"base": base, "blocks": edited, "final": final, "srt": str(p), "incertezas": [], "revisado": True,
                "idioma_original": "en", "traduzido": True, "sem_traducao": list(missing),
                "revisao_traducao": review if review is not None else {"avaliacoes": [], "resumo": "ok"}}

    def test_supervisor_aprova_traducao_fiel(self):
        base = [blk(0, 0.0, 2.0, "I think that fear is understandable"), blk(1, 2.1, 4.0, "but it is exaggerated.")]
        ed = [blk(0, 0.0, 2.0, "Eu acho esse medo compreensível"), blk(1, 2.1, 4.0, "mas é exagerado.")]
        rep = supervisors.legendas({"id": "t", "stage": "LEGENDAS", "item_id": "c"}, self._out(base, ed), None)
        self.assertTrue(rep["pode_avancar"], rep["problemas"])

    def test_supervisor_reprova_bloco_sem_traducao_ou_erro_de_sentido(self):
        base = [blk(0, 0.0, 2.0, "I think that fear is understandable"), blk(1, 2.1, 4.0, "but it is exaggerated.")]
        ed = [blk(0, 0.0, 2.0, "Eu acho esse medo compreensível"), blk(1, 2.1, 4.0, "but it is exaggerated.")]
        task = {"id": "t", "stage": "LEGENDAS", "item_id": "c"}
        self.assertFalse(supervisors.legendas(task, self._out(base, ed, missing=[1]), None)["pode_avancar"])
        ed2 = [blk(0, 0.0, 2.0, "Eu acho esse medo compreensível"), blk(1, 2.1, 4.0, "e é totalmente justificado.")]
        review = {"avaliacoes": [{"i": 1, "veredito": "ERRO", "motivo": "sentido invertido"}], "resumo": ""}
        rep = supervisors.legendas(task, self._out(base, ed2, review), None)
        self.assertFalse(rep["pode_avancar"])
        self.assertTrue(any("invertido" in p for p in rep["problemas"]))

    def test_supervisor_exige_auditoria_e_tamanho_coerente(self):
        base = [blk(0, 0.0, 2.0, "Absolutely.")]
        ed = [blk(0, 0.0, 2.0, "Com certeza, e além disso eu diria que todo mundo deveria pensar muito sobre isso.")]
        task = {"id": "t", "stage": "LEGENDAS", "item_id": "c"}
        out = self._out(base, ed)
        out["revisao_traducao"] = None
        rep = supervisors.legendas(task, out, None)
        self.assertTrue(any("sem auditoria" in p for p in rep["problemas"]))
        self.assertTrue(any("tamanho da tradução suspeito" in p for p in rep["problemas"]))
