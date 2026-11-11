"""Cota de armazenamento (10/11/2026): pausa em 10 GB, teto rígido de 11 GB."""
from unittest import mock

from cortes import acquire, config, cota, db, orchestrator

from .base import Base

GB = cota.GB


class Cota(Base):
    def _uso(self, gb):
        return mock.patch.object(cota, "usage_bytes", return_value=int(gb * GB))

    def test_pausa_no_limite_e_volta_quando_libera(self):
        orchestrator.enqueue("AQUISICAO", "opportunity", "o1")
        orchestrator.enqueue("PESQUISA", "batch", "p1")
        with self._uso(10.0):
            self.assertTrue(cota.paused())
            self.assertEqual(orchestrator.next_task()["stage"], "PESQUISA", "só etapas leves no limite")
            db.query("UPDATE tasks SET state='CONCLUIDO' WHERE stage='PESQUISA'")
            self.assertIsNone(orchestrator.next_task(), "download não começa com a cota cheia")
        with self._uso(9.5):
            self.assertEqual(orchestrator.next_task()["stage"], "AQUISICAO")

    def test_compressao_pode_rodar_pausada_porque_libera_espaco(self):
        orchestrator.enqueue("COMPRESSAO", "video", "v1")
        with self._uso(10.2):
            self.assertEqual(orchestrator.next_task()["stage"], "COMPRESSAO")

    def test_teto_rigido_com_tolerancia(self):
        with self._uso(10.5):
            cota.ensure(int(0.4 * GB), "teste")
            with self.assertRaises(cota.LimitReached):
                cota.ensure(int(0.6 * GB), "teste")

    def test_etapa_que_nao_cabe_espera_sem_gastar_tentativa(self):
        orchestrator.enqueue("FINALIZACAO", "cut", "c1")
        t = orchestrator.next_task()
        with self._uso(10.9):
            self.assertIsNone(orchestrator.run_task(t))
        t2 = db.query("SELECT * FROM tasks WHERE id=?", (t["id"],))[0]
        self.assertEqual((t2["state"], t2["attempts"]), ("PENDENTE", 0))
        self.assertTrue(t2["not_before"])
        self.assertIn("armazenamento", t2["error"])

    def test_limite_configuravel(self):
        cfg = config.load()
        cfg["armazenamento"].update({"limite_gb": 5, "tolerancia_gb": 0.5})
        config.save(cfg)
        self.assertEqual(cota.limits(), (5 * GB, int(5.5 * GB)))

    def test_estimativa_do_download(self):
        info = {"duration": 100, "requested_formats": [{"filesize": 1000}, {"filesize_approx": 500}]}
        self.assertEqual(acquire.download_estimate(info), (int(1500 * 1.05), True))
        est, merge = acquire.download_estimate({"duration": 3600, "tbr": 2000})
        self.assertFalse(merge)
        self.assertAlmostEqual(est, 2000 * 1000 / 8 * 3600 * 1.05, delta=1)

    def test_uso_real_soma_a_pasta_toda(self):
        (config.PROD / "x.bin").write_bytes(b"0" * 5000)
        self.assertGreaterEqual(cota.usage_bytes(fresh=True), 5000)


class FocoTema(Base):
    def test_foco_tema_so_roda_o_assunto(self):
        from cortes import db, orchestrator
        c = config.load()
        c["foco"], c["foco_termos"] = "tema", ["gta 6", "gta vi"]
        config.save(c)
        orchestrator.enqueue("PESQUISA", "batch", "outro", {"pedido": {"tema": "podcast de futebol"}})
        orchestrator.enqueue("PESQUISA", "batch", "gta", {"pedido": {"tema": "GTA 6 ao vivo, Canal Exemplo"}})
        self.assertEqual(orchestrator.next_task()["item_id"], "gta")
        db.query("UPDATE tasks SET state='CONCLUIDO' WHERE item_id='gta'")
        self.assertIsNone(orchestrator.next_task(), "fora do tema fica parado")
        c["foco"] = ""
        config.save(c)
        self.assertEqual(orchestrator.next_task()["item_id"], "outro", "sem foco volta tudo")
