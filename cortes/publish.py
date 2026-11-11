"""Agentes 08/09: textos de publicação, organização das pastas e publicação idempotente."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, log

TEXT_SCHEMA = {
    "type": "object", "required": ["titulo", "alternativas", "descricao", "hashtags", "justificativa"],
    "properties": {"titulo": {"type": "string"}, "alternativas": {"type": "array", "items": {"type": "string"}},
                   "descricao": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
                   "justificativa": {"type": "string"}},
}


def next_slot(now: datetime | None = None) -> datetime:
    """Próximo horário de publicação livre conforme a agenda configurada."""
    cfg = config.load()["agenda"]
    now = now or config.now()
    taken = {r["scheduled_for"] for r in db.query("SELECT scheduled_for FROM exports WHERE scheduled_for IS NOT NULL")}
    day = now.date()
    for _ in range(366):
        for hhmm in sorted(cfg["horarios"]):
            h, m = map(int, hhmm.split(":"))
            slot = datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
            if slot > now + timedelta(minutes=30) and config.iso(slot) not in taken:
                return slot
        day += timedelta(days=1)
    raise RuntimeError("agenda sem horários livres")


def organize(cut_id: str, final_dir: Path, texts: dict, qc_report: dict) -> dict:
    cut = db.get("cuts", cut_id)
    existing = db.get("exports", cut_id, "cut_id")
    if existing and existing["folder"] and Path(existing["folder"], "metadata.json").exists():
        return {"folder": existing["folder"], "export_id": existing["id"], "reused": True}
    slot = next_slot()
    folder = config.DIRS["produzidos"] / slot.strftime("%Y-%m-%d") / slot.strftime("%H-%M")
    folder.mkdir(parents=True, exist_ok=True)
    exported_at = config.iso(datetime.fromtimestamp((final_dir / "video.mp4").stat().st_mtime).astimezone())
    shutil.copy2(final_dir / "video.mp4", folder / "video.mp4")
    shutil.copy2(final_dir / "legenda.srt", folder / "legenda.srt")
    if (final_dir / "legenda_original.srt").exists():  # vídeo estrangeiro: legenda no idioma falado
        shutil.copy2(final_dir / "legenda_original.srt", folder / "legenda_original.srt")
    (folder / "titulo.txt").write_text(texts["titulo"], "utf-8")
    desc = texts["descricao"].rstrip()
    missing = [h for h in texts["hashtags"] if h.lower() not in desc.lower()]  # a IA às vezes já as inclui
    (folder / "descricao.txt").write_text(desc + (f"\n\n{' '.join(missing)}" if missing else ""), "utf-8")
    video = db.get("videos", cut["video_id"])
    eid = existing["id"] if existing else db.new_id("exp")
    meta = {"export_id": eid, "corte_id": cut_id, "video_origem": video["id"], "fonte": video["url"],
            "canal_fonte": video["channel"], "inicio_original_s": cut["start"], "fim_original_s": cut["end"],
            "participantes": cut["participants"], "idioma_original": video.get("language"),
            "legenda_traduzida": (final_dir / "legenda_original.srt").exists(), "titulo": texts["titulo"], "alternativas": texts["alternativas"],
            "hashtags": texts["hashtags"], "plataforma": config.load()["agenda"]["plataforma"],
            "data_producao": cut["created_at"], "data_exportacao": exported_at,
            "publicacao_programada": config.iso(slot), "data_publicacao": None,
            "qc": qc_report["status"], "justificativa_textos": texts["justificativa"]}
    (folder / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    db.insert("exports", {"id": eid, "cut_id": cut_id, "video_path": str(folder / "video.mp4"),
                          "srt_path": str(folder / "legenda.srt"), "folder": str(folder),
                          "produced_at": cut["created_at"], "exported_at": exported_at,
                          "scheduled_for": config.iso(slot), "qc_status": qc_report["status"], "qc": qc_report,
                          "title": texts["titulo"], "description": texts["descricao"], "hashtags": texts["hashtags"],
                          "platform": meta["plataforma"], "state": "ORGANIZADO"}, replace=True)
    (final_dir / "video.mp4").unlink(missing_ok=True)  # só após registrar o destino
    return {"folder": str(folder), "export_id": eid, "scheduled_for": config.iso(slot)}


class ManualAdapter:
    """Sem integração oficial configurada: deixa pronto e diz isso — nunca alega publicação."""
    name = "manual"

    def publish(self, export: dict) -> dict:
        return {"state": "PRONTO_PARA_PUBLICAR",
                "error": "nenhuma integração oficial autorizada; arquivo pronto em " + export["folder"]}


ADAPTERS = {"manual": ManualAdapter}


def publish(export_id: str) -> dict:
    export = db.get("exports", export_id)
    adapter = ADAPTERS[config.load()["publicacao"]["adaptador"]]()
    key = f"{export_id}:{export['platform']}"
    pub = db.get("publications", key, "idem_key")
    if pub and pub["state"] == "PUBLICADO":
        return {"publication_id": pub["id"], "skipped": "já publicado"}
    if pub and pub["state"] == "ENVIANDO":
        # interrupção durante o envio: não reenviar às cegas (evita duplicar); exige conferência
        db.update("publications", pub["id"], {"state": "VERIFICAR_NA_PLATAFORMA"})
        return {"publication_id": pub["id"], "skipped": "estado incerto; conferir na plataforma"}
    pid = pub["id"] if pub else db.new_id("pub")
    if not pub:
        db.insert("publications", {"id": pid, "export_id": export_id, "platform": export["platform"],
                                   "idem_key": key, "state": "ENVIANDO", "attempts": 0})
    db.update("publications", pid, {"state": "ENVIANDO", "attempts": (pub["attempts"] if pub else 0) + 1})
    res = adapter.publish(export)
    if res["state"] == "PUBLICADO" and not (res.get("external_id") or res.get("url")):
        res = {**res, "state": "VERIFICAR_NA_PLATAFORMA", "error": "plataforma não devolveu identificador"}
    db.update("publications", pid, {"state": res["state"], "external_id": res.get("external_id"),
                                    "url": res.get("url"), "error": res.get("error"),
                                    "published_at": config.iso() if res["state"] == "PUBLICADO" else None})
    db.update("exports", export_id, {"state": res["state"]})
    if res["state"] == "PUBLICADO":
        meta = Path(export["folder"]) / "metadata.json"
        m = json.loads(meta.read_text("utf-8"))
        m.update({"data_publicacao": config.iso(), "url_publicacao": res.get("url")})
        meta.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")
    log.event(item_id=export_id, agent="09-distribuidor", requested=f"publicar via {adapter.name}",
              result=res["state"], error=res.get("error"))
    return {"publication_id": pid, **res}
