from collections import Counter

from cortes import config, db, recommend

from .base import Base


def cand(i, niche, channel, topic="assunto qualquer"):
    return {"id": f"o{i}", "topic": f"{topic} {i}", "niche": niche, "channel_id": channel,
            "features": {"formato": "podcast"}, "confidence": 0.6, "published_at": "20261107"}


class Recomendacao(Base):
    def decide_hist(self, niche, channel, score, days_ago, topic="assunto qualquer", n=1, is_test=0):
        for _ in range(n):
            when = config.iso(config.now() - config.timedelta(days=days_ago))
            feats = recommend.opportunity_features(cand(0, niche, channel, topic))
            db.insert("decisions", {"kind": "opportunity", "item_id": "x", "score": score, "label": "",
                                    "features": feats, "decided_at": when, "is_test": is_test})

    def test_sem_historico_explora(self):
        res = recommend.rank([cand(i, f"n{i}", f"c{i}") for i in range(6)], 4)
        self.assertEqual(len(res), 4)
        self.assertTrue(all(r["rec_explain"]["estrategia"] == "exploracao" for r in res))

    def test_preferencia_conhecida_sem_monopolio(self):
        self.decide_hist("tecnologia", "cA", 100, 2, n=6)
        pool = [cand(i, "tecnologia", "cA") for i in range(8)] + [cand(10 + i, f"novo{i}", f"z{i}", f"tema{i} inédito")
                                                                  for i in range(5)]
        res = recommend.rank(pool, 6)
        strat = Counter(r["rec_explain"]["estrategia"] for r in res)
        self.assertGreaterEqual(strat["preferencia"], 1)
        self.assertGreaterEqual(strat["exploracao"], 1, "sempre há espaço para novidades")

    def test_diversidade_de_fontes(self):
        self.decide_hist("tecnologia", "cA", 100, 1, n=5)
        pool = [cand(i, "tecnologia", "cA") for i in range(10)] + [cand(20, "tecnologia", "cB"), cand(21, "tecnologia", "cC")]
        res = recommend.rank(pool, 4)
        self.assertGreater(len({r["channel_id"] for r in res}), 1, "não concentra tudo num canal")

    def test_segunda_chance_rejeicao_forte(self):
        self.decide_hist("politica", "cP", -100, 2, n=2)
        recent = recommend.rank([cand(1, "politica", "cP")], 1)[0]
        with db.tx() as c:
            c.execute("DELETE FROM decisions")
        self.decide_hist("politica", "cP", -100, 60, n=2)
        old = recommend.rank([cand(1, "politica", "cP")], 1)[0]
        self.assertEqual(old["rec_explain"]["estrategia"], "reavaliacao")
        self.assertGreater(old["_val"], recent["_val"], "o tempo reabre a oportunidade")
        self.assertGreater(old["_val"], 0, "rejeição forte reduz, mas nunca zera")

    def test_avaliacao_isolada_pesa_pouco(self):
        self.decide_hist("culinaria", "cK", 100, 0)
        w = recommend.weights("opportunity")
        self.assertLessEqual(w["nicho=culinaria"]["peso"], 0.26)

    def test_mudanca_recente_de_preferencia(self):
        self.decide_hist("games", "cG", -100, 120, n=4)
        self.decide_hist("games", "cG", 100, 3, n=4)
        self.assertGreater(recommend.weights("opportunity")["nicho=games"]["peso"], 0.3)

    def test_dados_de_teste_nao_contaminam_perfil(self):
        self.decide_hist("esporte", "cE", 100, 1, n=5, is_test=1)
        self.assertNotIn("nicho=esporte", recommend.weights("opportunity"))

    def test_explicacao(self):
        self.decide_hist("ciencia", "cS", 50, 5, n=3)
        r = recommend.rank([cand(1, "ciencia", "cS")], 1)[0]
        self.assertIn("nicho=ciencia", r["rec_explain"]["mensagem"])
