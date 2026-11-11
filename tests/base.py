import os
import shutil
import subprocess
import unittest
from pathlib import Path

from cortes import config, db

FIX = Path(os.environ["CORTES_PRODUCAO"]).parent / "cortes_fixtures"


def make_video(name: str, seconds: int = 8, size: str = "640x360") -> Path:
    """Vídeo sintético com tom de áudio (material de teste próprio)."""
    FIX.mkdir(exist_ok=True)
    p = FIX / name
    if not p.exists():
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", str(seconds),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-c:a", "aac", str(p)], check=True)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        db.reset_connection()
        if config.PROD.exists():
            shutil.rmtree(config.PROD)
        config.ensure_dirs()
        os.environ.pop("CORTES_FAKE_NOW", None)

    def tearDown(self):
        os.environ.pop("CORTES_FAKE_NOW", None)

    @staticmethod
    def video_record(src: Path, acquired_at: str | None = None, **kw) -> str:
        """Simula um original adquirido dentro de ORIGINAIS."""
        vid = db.new_id("vid")
        folder = config.DIRS["originais"] / vid
        folder.mkdir(parents=True)
        dest = folder / f"{vid}.original.mp4"
        shutil.copy(src, dest)
        from cortes import media
        db.insert("videos", {"id": vid, "opportunity_id": kw.get("opp"), "url": "local:teste", "title": "teste",
                             "channel": "local", "duration": media.probe(dest)["duration"],
                             "acquired_at": acquired_at or config.iso(), "original_path": str(dest),
                             "original_size": dest.stat().st_size, "probe": media.probe(dest),
                             "state": "ADQUIRIDO", "retention_state": "PENDENTE_COMPRESSAO", "is_test": 1})
        return vid

    @staticmethod
    def opportunity(state="AGUARDANDO_APROVACAO", ok=True, **kw) -> str:
        oid = db.new_id("opp")
        data = {"id": oid, "created_at": config.iso(), "topic": kw.get("topic", "tema de teste"), "summary": "s",
                "reason": "r", "source_url": kw.get("url", f"local:{oid}"), "source_title": "t",
                "channel": kw.get("channel", "canal"), "channel_id": kw.get("channel_id", "ch1"),
                "niche": kw.get("niche", "tecnologia"), "features": {"formato": kw.get("formato", "podcast")},
                "availability": {"ok": ok, "problems": [] if ok else ["indisponível"]}, "state": state,
                "is_test": kw.get("is_test", 0)}
        db.insert("opportunities", data)
        return oid
