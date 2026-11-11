import os
from pathlib import Path
from unittest import mock

from cortes import config, db, storage

from .base import Base, make_video


class Armazenamento(Base):
    def test_compressao_valida_remove_original_e_registra_retencao(self):
        vid = self.video_record(make_video("grande.mp4", 8, "1280x720"), acquired_at="2026-11-01T10:00:00-03:00")
        orig = Path(db.get("videos", vid)["original_path"])
        out = storage.compress(vid)
        self.assertTrue(orig.exists(), "original só sai depois da auditoria aprovar")
        self.assertTrue(storage.compress(vid)["reused"], "repetição reaproveita a cópia validada")
        storage.finalize_compression(vid)
        v = db.get("videos", vid)
        self.assertTrue(Path(v["compressed_path"]).exists())
        self.assertFalse(orig.exists(), "original removido após validação + auditoria")
        self.assertEqual(storage.finalize_compression(vid), 0, "idempotente")
        self.assertEqual(v["retention_until"], "2026-11-15T10:00:00-03:00")  # 14 dias corridos
        self.assertLess(v["compressed_size"], v["original_size"])
        self.assertEqual(len(v["compressed_sha256"]), 64)
        self.assertGreater(out["saving"], 0.15)
        self.assertTrue(db.query("SELECT 1 FROM retention_log WHERE action='ORIGINAL_REMOVIDO'"))

    def test_falha_de_compressao_preserva_original(self):
        bad = config.PROD.parent / "corrompido.mp4"
        bad.write_bytes(make_video("ok.mp4").read_bytes()[:20000])
        vid = self.video_record(make_video("ok.mp4"))
        orig = Path(db.get("videos", vid)["original_path"])
        orig.write_bytes(bad.read_bytes())  # original danificado → ffprobe/ffmpeg falham
        with self.assertRaises(Exception):
            storage.compress(vid)
        self.assertTrue(orig.exists(), "original nunca pode ser apagado quando a compressão falha")
        self.assertIsNone(db.get("videos", vid)["compressed_path"])

    def test_sem_espaco_nao_apaga_original(self):
        vid = self.video_record(make_video("ok.mp4"))
        with mock.patch("cortes.media.free_disk_mb", return_value=10):
            with self.assertRaises(storage.NoSpace):
                storage.compress(vid)
        self.assertTrue(Path(db.get("videos", vid)["original_path"]).exists())

    def _compressed(self, acquired="2026-11-01T10:00:00-03:00"):
        vid = self.video_record(make_video("grande.mp4", 8, "1280x720"), acquired_at=acquired)
        storage.compress(vid)
        storage.finalize_compression(vid)
        return vid

    def test_exclusao_apos_14_dias_idempotente(self):
        vid = self._compressed()
        path = Path(db.get("videos", vid)["compressed_path"])
        os.environ["CORTES_FAKE_NOW"] = "2026-11-14T10:00:00-03:00"  # 13 dias: ainda não
        self.assertEqual(storage.retention_sweep()["excluidos"], 0)
        self.assertTrue(path.exists())
        os.environ["CORTES_FAKE_NOW"] = "2026-11-20T08:00:00-03:00"  # sistema ficou dias desligado
        s = storage.retention_sweep()
        self.assertEqual(s["excluidos"], 1)
        self.assertGreater(s["bytes"], 0)
        self.assertFalse(path.exists())
        v = db.get("videos", vid)
        self.assertEqual(v["retention_state"], "EXCLUIDO")
        self.assertEqual(storage.retention_sweep()["excluidos"], 0, "segunda execução não faz nada")
        self.assertEqual(len(db.query("SELECT * FROM retention_log WHERE action='COMPRIMIDO_EXCLUIDO'")), 1)

    def test_dependencia_ativa_gera_excecao(self):
        vid = self._compressed()
        db.insert("tasks", {"id": "t1", "stage": "TRANSCRICAO", "item_type": "video", "item_id": vid, "state": "EM_EXECUCAO"})
        os.environ["CORTES_FAKE_NOW"] = "2026-12-01T10:00:00-03:00"
        s = storage.retention_sweep()
        self.assertEqual((s["excluidos"], s["excecoes"]), (0, 1))
        self.assertTrue(Path(db.get("videos", vid)["compressed_path"]).exists())
        self.assertEqual(db.get("videos", vid)["retention_state"], "EXCECAO")
        db.update("tasks", "t1", {"state": "CONCLUIDO"})
        self.assertEqual(storage.retention_sweep()["excluidos"], 1, "limpa assim que a dependência acaba")

    def test_corte_pendente_preserva_somente_o_trecho(self):
        vid = self._compressed()
        db.insert("cuts", {"id": "cut_a", "video_id": vid, "start": 2.0, "end": 5.0, "state": "ADIADO",
                           "scores": {"intervalos": [[2.0, 5.0]]}})
        db.insert("cuts", {"id": "cut_b", "video_id": vid, "start": 1.0, "end": 3.0, "state": "REJEITADO",
                           "scores": {"intervalos": [[1.0, 3.0]]}})
        os.environ["CORTES_FAKE_NOW"] = "2026-12-01T10:00:00-03:00"
        storage.retention_sweep()
        a = db.get("cuts", "cut_a")
        self.assertTrue(Path(a["segment_path"]).exists(), "trecho do corte adiado preservado")
        self.assertFalse(db.get("cuts", "cut_b")["segment_path"], "corte rejeitado não preserva nada")
        self.assertFalse(Path(db.get("videos", vid)["compressed_path"]).exists())
        src, off = storage.cut_source(a, db.get("videos", vid))
        self.assertEqual((src, off), (Path(a["segment_path"]), 0.0))  # trecho começa em max(0, 2-3) = 0
        db.update("cuts", "cut_a", {"state": "REJEITADO"})
        self.assertEqual(storage.retention_sweep()["trechos_removidos"], 1)
        self.assertFalse(Path(a["segment_path"]).exists())

    def test_nunca_exclui_fora_de_originais(self):
        vid = self._compressed()
        final = config.DIRS["produzidos"] / "2026-11-10" / "12-00" / "video.mp4"
        final.parent.mkdir(parents=True)
        final.write_bytes(b"x" * 100)
        db.update("videos", vid, {"compressed_path": str(final)})  # registro adulterado apontando p/ produzidos
        os.environ["CORTES_FAKE_NOW"] = "2026-12-01T10:00:00-03:00"
        storage.retention_sweep()
        self.assertTrue(final.exists(), "vídeos finais nunca são excluídos")
        self.assertTrue(db.query("SELECT 1 FROM events WHERE result LIKE 'RECUSADO%'"))
