"""Ritmo dos cortes (10/11/2026): silêncio inútil sai, pausa que dá sentido fica."""
import unittest

from cortes import cuts, subtitles


def W(w, s, e):
    return {"w": " " + w, "s": s, "e": e, "p": 0.9}


def U(i, spk, words):
    return {"id": i, "speaker": spk, "start": words[0]["s"], "end": words[-1]["e"], "low_conf": False,
            "text": "".join(w["w"] for w in words), "words": words}


def removed(ivs, start, end):
    return round(end - start - cuts.total(ivs), 3)


class Ritmo(unittest.TestCase):
    def test_silencio_no_meio_da_frase_vira_pausa_curta(self):
        u = [U(0, "A", [W("eu", 0.0, 0.3), W("acho", 1.5, 1.9), W("que", 2.0, 2.2)])]
        ivs = cuts.keep_intervals(u, 0.0, 2.3)
        self.assertAlmostEqual(removed(ivs, 0.0, 2.3), 1.2 - 0.15, places=2)

    def test_pausa_dramatica_fica_mais_longa_que_fim_de_frase(self):
        dram = [U(0, "A", [W("sabe", 0.0, 0.5), W("quem?", 0.6, 1.0), W("Ele.", 3.0, 3.5)])]
        fim = [U(0, "A", [W("sabe", 0.0, 0.5), W("isso.", 0.6, 1.0), W("Ele", 3.0, 3.5)])]
        kd = cuts.total(cuts.keep_intervals(dram, 0.0, 3.6))
        kf = cuts.total(cuts.keep_intervals(fim, 0.0, 3.6))
        self.assertAlmostEqual(kd - kf, 0.85 - 0.4, places=2)

    def test_pausa_pequena_nao_e_mexida(self):
        u = [U(0, "A", [W("um", 0.0, 0.4), W("dois", 0.6, 1.0)])]
        self.assertEqual(cuts.keep_intervals(u, 0.0, 1.1), [[0.0, 1.1]])

    def test_silencio_medido_corrige_palavra_esticada(self):
        # Whisper diz que "é" vai até 2.0, mas o áudio já está em silêncio desde 0.6
        u = [U(0, "A", [W("é", 0.3, 2.0), W("verdade", 2.5, 3.0)])]
        sem = removed(cuts.keep_intervals(u, 0.0, 3.1), 0.0, 3.1)
        com = removed(cuts.keep_intervals(u, 0.0, 3.1, sil=[(0.6, 2.5)]), 0.0, 3.1)
        self.assertGreater(com, sem + 0.9)
        blocks = subtitles.build_blocks(u, cuts.keep_intervals(u, 0.0, 3.1, sil=[(0.6, 2.5)]), 28)
        self.assertEqual(blocks[0]["text"], "é verdade", "nenhuma palavra some da legenda")

    def test_nao_cria_pedacinhos(self):
        u = [U(0, "A", [W("a", 0.0, 0.1), W("b", 1.0, 1.1), W("c", 2.0, 2.1)])]
        for a, b in cuts.keep_intervals(u, 0.0, 2.2):
            self.assertGreaterEqual(b - a, 0.35 - 1e-6)

    def test_emendas_tem_microfade(self):
        g, _, _ = cuts.concat_filter([[0, 1], [2, 3]], 0)
        self.assertEqual(g.count("afade=t=in"), 2)
        g1, _, _ = cuts.concat_filter([[0, 1]], 0)
        self.assertNotIn("afade", g1)


if __name__ == "__main__":
    unittest.main()
