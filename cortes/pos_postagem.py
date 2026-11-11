"""Marcações de postagem (TikTok / Instagram) e limpeza do vídeo final no dia seguinte.

Pedido do usuário (10/11/2026): depois de marcado como postado, o vídeo final é apagado no dia seguinte
para liberar espaço. Título, descrição, hashtags, legendas e metadados NUNCA são apagados — só o .mp4 final,
e só dentro de PRODUZIDOS. Com a nuvem ligada, o vídeo só é apagado depois de a cópia ser conferida.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from . import config, db, log, nuvem

PLATAFORMAS = {"tiktok": "TikTok", "instagram": "Instagram"}


def marks(export_id: str) -> dict:
    return {r["platform"]: r for r in db.query(
        "SELECT platform, url, posted_at FROM postings WHERE export_id=?", (export_id,))}


def mark(export_id: str, plataforma: str, url: str | None = None, desfazer: bool = False) -> dict:
    if plataforma not in PLATAFORMAS:
        raise ValueError("plataforma inválida")
    e = db.get("exports", export_id)
    if not e:
        raise KeyError(export_id)
    with db.tx() as c:
        if desfazer:
            c.execute("DELETE FROM postings WHERE export_id=? AND platform=?", (export_id, plataforma))
        else:
            c.execute("INSERT INTO postings(export_id, platform, url, posted_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(export_id, platform) DO UPDATE SET url=COALESCE(excluded.url, postings.url)",
                      (export_id, plataforma, (url or "").strip() or None, config.iso()))
    m = marks(export_id)
    # compatível com o registro antigo (aba Prontos / futuro Agente 10)
    pub = db.query("SELECT id FROM publications WHERE export_id=?", (export_id,))
    data = {"state": "POSTADO_PELO_USUARIO" if m else "PRONTO", "url": next((x["url"] for x in m.values() if x["url"]), None),
            "published_at": min((x["posted_at"] for x in m.values()), default=None), "error": None}
    if pub:
        db.update("publications", pub[0]["id"], data)
    elif m:
        db.insert("publications", {"id": db.new_id("pub"), "export_id": export_id, "platform": e["platform"],
                                   "idem_key": f"{export_id}:{e['platform']}", **data})
    db.update("exports", export_id, {"state": "POSTADO_PELO_USUARIO" if m else "PRONTO_PARA_PUBLICAR"})
    log.event(agent="usuario", item_id=export_id, requested=f"{'desmarcar' if desfazer else 'marcar'} postado {plataforma}",
              result="ok")
    return {"marcacoes": m, "exclusao_prevista": due_date(export_id)}


def due_date(export_id: str) -> str | None:
    """Data (YYYY-MM-DD) a partir da qual o vídeo pode ser apagado, ou None se ainda não cumpre a regra."""
    cfg = config.load()["pos_postagem"]
    if not cfg["excluir_video"]:
        return None
    m = marks(export_id)
    need = set(PLATAFORMAS) if cfg["exigir"] == "ambas" else None
    if not m or (need and not need.issubset(m)):
        return None
    last = max(config.parse(x["posted_at"]) for x in m.values())
    return (last.date() + timedelta(days=1)).isoformat()


def sweep() -> dict:
    """Apaga os vídeos finais cuja data de exclusão chegou. Idempotente; falha não trava a produção."""
    out = {"apagados": 0, "bytes": 0, "adiados_nuvem": 0}
    today = config.now().date().isoformat()
    root = config.DIRS["produzidos"].resolve()
    for e in db.query("SELECT id, video_path, folder FROM exports WHERE video_deleted_at IS NULL "
                      "AND id IN (SELECT export_id FROM postings)"):
        due = due_date(e["id"])
        if not due or due > today or not e["video_path"]:
            continue
        path = Path(e["video_path"]).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".mp4":
            log.event(agent="pos-postagem", item_id=e["id"], error=f"fora de PRODUZIDOS, não apagado: {path}", state="FALHA")
            continue
        cloud = None
        if path.exists() and config.load()["nuvem"]["habilitada"]:
            try:
                cloud = nuvem.upload(path, f"{Path(e['folder']).parent.name}/{Path(e['folder']).name}/{e['id']}.mp4")
            except Exception as ex:  # noqa: BLE001 — sem cópia conferida, não apaga
                out["adiados_nuvem"] += 1
                log.event(agent="pos-postagem", item_id=e["id"], error=f"cópia na nuvem falhou; vídeo mantido: {ex}",
                          state="FALHA")
                continue
        size = path.stat().st_size if path.exists() else 0
        path.unlink(missing_ok=True)
        db.update("exports", e["id"], {"video_deleted_at": config.iso(), "cloud_path": cloud})
        db.insert("retention_log", {"ts": config.iso(), "video_id": e["id"], "action": "video_final_apagado_pos_postagem",
                                    "path": str(path), "bytes": size, "detail": f"nuvem: {cloud or 'desligada'}"})
        out["apagados"] += 1
        out["bytes"] += size
    if out["apagados"] or out["adiados_nuvem"]:
        log.event(agent="pos-postagem", requested="limpeza pós-postagem", result=str(out), state="CONCLUIDO")
    return out
