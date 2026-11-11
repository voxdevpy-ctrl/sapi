"""Agente 02: verificação preliminar de disponibilidade, base de autorização e aquisição."""
from __future__ import annotations

import difflib
import shutil
from pathlib import Path

from . import config, cota, db, log, media

AGENT = "02-localizador"
LOCAL_PREFIX = "local:"


def _ydl_opts(extra: dict | None = None) -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "js_runtimes": {"node": {}},
         "socket_timeout": 30, "retries": 3}
    o.update(extra or {})
    return o


def authorization_basis(info: dict, cfg: dict) -> str | None:
    a = cfg["autorizacao"]
    ch = info.get("channel_id") or ""
    if info.get("_local"):
        return "conteudo_local_fornecido_pelo_usuario"
    if ch and ch in a["canais_proprios"]:
        return "conteudo_proprio"
    if ch and ch in a["canais_autorizados"]:
        return f"canal_autorizado: {a['canais_autorizados'][ch]}"
    lic = (info.get("license") or "").lower()
    if a["aceitar_creative_commons"] and "creative commons" in lic:
        return "creative_commons (reuso permitido; atribuição obrigatória na descrição)"
    if a.get("autorizar_qualquer_canal"):
        return f"autorizado_pelo_usuario: qualquer canal ({a.get('autorizado_em') or 'decisão registrada na configuração'})"
    return None


def _local_info(url: str) -> dict:
    p = Path(url[len(LOCAL_PREFIX):])
    if not p.exists():
        return {"_error": f"arquivo local não encontrado: {p}"}
    pr = media.probe(p)
    return {"_local": True, "title": p.stem, "duration": pr["duration"], "channel": "local",
            "channel_id": "local", "webpage_url": url, "height": (pr["video"] or {}).get("height"),
            "formats": [{"vcodec": (pr["video"] or {}).get("codec"), "acodec": (pr["audio"] or {}).get("codec")}]}


def download_estimate(info: dict) -> tuple[int, bool]:
    """Bytes previstos do download e se haverá junção de vídeo + áudio."""
    fmts = info.get("requested_formats") or [info]
    dur = float(info.get("duration") or 0)
    total = 0
    for f in fmts:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and dur:
            size = f["tbr"] * 1000 / 8 * dur
        total += int(size or dur * 4e6 / 8 / len(fmts))  # sem informação: ~4 Mbps
    return int(total * 1.05), len(fmts) > 1


def check_availability(url: str, expected_title: str | None = None) -> dict:
    """Confirma existência, acessibilidade, duração, método legítimo e autorização — sem baixar."""
    cfg = config.load()
    res = {"checked_at": config.iso(), "url": url, "method": None, "problems": [], "ok": False}
    try:
        if url.startswith(LOCAL_PREFIX):
            info = _local_info(url)
            res["method"] = "arquivo_local"
        else:
            import yt_dlp
            with yt_dlp.YoutubeDL(_ydl_opts({"skip_download": True})) as y:
                info = y.extract_info(url, download=False)
            res["method"] = "yt-dlp"
    except Exception as e:  # noqa: BLE001 — qualquer falha = indisponível, com motivo
        res["problems"].append(f"inacessível: {str(e)[:300]}")
        return res
    if info.get("_error"):
        res["problems"].append(info["_error"])
        return res
    fmts = info.get("formats") or []
    res.update({"title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
                "channel_id": info.get("channel_id"), "duration": info.get("duration"),
                "upload_date": info.get("upload_date"), "view_count": info.get("view_count"),
                "like_count": info.get("like_count"), "license": info.get("license"), "language": info.get("language"),
                "description": (info.get("description") or "")[:1500],
                "best_height": max((f.get("height") or 0 for f in fmts), default=info.get("height") or 0)})
    if info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        res["problems"].append(f"transmissão não finalizada ({info['live_status']})")
    if info.get("availability") not in (None, "public", "unlisted"):
        res["problems"].append(f"acesso restrito ({info.get('availability')})")
    if any(f.get("has_drm") for f in fmts):
        res["problems"].append("conteúdo com DRM — não será contornado")
    if not (res["duration"] or 0) >= 60:
        res["problems"].append(f"duração insuficiente ou desconhecida ({res['duration']})")
    has_v = any((f.get("vcodec") or "none") != "none" for f in fmts)
    has_a = any((f.get("acodec") or "none") != "none" for f in fmts)
    if not (has_v and has_a):
        res["problems"].append("sem formatos de vídeo e áudio utilizáveis")
    if expected_title and res["title"]:
        sim = difflib.SequenceMatcher(None, expected_title.lower(), res["title"].lower()).ratio()
        res["title_similarity"] = round(sim, 2)
        if sim < 0.45:
            res["problems"].append(f"título real difere do apresentado (similaridade {sim:.2f})")
    basis = authorization_basis(info, cfg)
    res["authorization"] = basis
    if not basis:
        res["problems"].append("sem base de autorização (não é próprio, autorizado nem Creative Commons)")
    res["ok"] = not res["problems"]
    return res


def acquire(opportunity_id: str, task_id: str | None = None) -> dict:
    o = db.get("opportunities", opportunity_id)
    if o["state"] != "APROVADO":
        raise PermissionError(f"oportunidade {opportunity_id} não aprovada (estado {o['state']})")
    done = db.query("SELECT * FROM videos WHERE opportunity_id=?", (opportunity_id,))
    if done:  # repetição após queda/reprovação: não baixa nem cria outro vídeo
        v = done[0]
        return {"video_id": v["id"], "files": [v["original_path"]], "probe": v["probe"],
                "availability": o.get("availability") or {}, "reused": True}
    vid = "vid_" + (task_id or db.new_id("x"))[-10:]  # determinístico por tarefa: retomada reaproveita a pasta
    folder = config.DIRS["originais"] / vid
    partial = [p for p in folder.glob(f"{vid}.original.*") if not p.name.endswith((".part", ".ytdl"))] if folder.exists() else []
    av = check_availability(o["source_url"], o["source_title"]) if not partial else (o.get("availability") or {"ok": True})
    log.event(task_id=task_id, item_id=opportunity_id, agent=AGENT, requested="reverificar disponibilidade",
              result="ok" if av["ok"] else "; ".join(av["problems"]), state="EM_EXECUCAO")
    if not av["ok"]:
        raise RuntimeError("fonte deixou de estar disponível: " + "; ".join(av["problems"]))
    folder.mkdir(parents=True, exist_ok=True)
    if partial:  # arquivo já trazido numa tentativa anterior (ex.: fonte local já movida)
        dest = partial[0]
    elif o["source_url"].startswith(LOCAL_PREFIX):
        src = Path(o["source_url"][len(LOCAL_PREFIX):])
        dest = folder / f"{vid}.original{src.suffix.lower()}"
        shutil.move(str(src), dest)  # a ENTRADA_LOCAL funciona como caixa de entrada
    else:
        import yt_dlp
        fmt = "bv*[height<=1080]+ba/b[height<=1080]/b"
        with yt_dlp.YoutubeDL(_ydl_opts({"format": fmt, "skip_download": True})) as y:
            est, merge = download_estimate(y.extract_info(o["source_url"], download=False))
        already = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
        # juntar vídeo + áudio no fim exige o dobro por alguns instantes
        cota.ensure(max(0, est * (2 if merge else 1) - already), "o download")
        avail = cota.room() + already
        got, over = {}, []

        def guard(d):
            if d.get("status") == "downloading":
                got[d.get("filename")] = d.get("downloaded_bytes") or 0
                if sum(got.values()) * (2 if merge else 1) > avail:
                    over.append(1)
                    raise cota.LimitReached("armazenamento: o download passaria do teto; pausado (o parcial é retomado depois)")
        opts = _ydl_opts({"format": fmt, "merge_output_format": "mp4", "progress_hooks": [guard],
                          "outtmpl": str(folder / f"{vid}.original.%(ext)s"), "continuedl": True})
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                y.extract_info(o["source_url"], download=True)
        except Exception as e:  # noqa: BLE001
            if over:
                raise cota.LimitReached(str(e)[:300]) from e
            raise
        files = [p for p in folder.glob(f"{vid}.original.*") if not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("download não produziu arquivo")
        dest = files[0]
    pr = media.probe(dest)
    if av.get("duration") and abs(pr["duration"] - av["duration"]) > 2:
        raise RuntimeError(f"arquivo incompleto: {pr['duration']:.0f}s de {av['duration']:.0f}s")
    db.insert("videos", {"id": vid, "opportunity_id": opportunity_id, "url": o["source_url"],
                         "title": av.get("title"), "channel": av.get("channel"), "duration": pr["duration"],
                         "acquired_at": config.iso(), "original_path": str(dest), "original_size": dest.stat().st_size,
                         "probe": pr, "state": "ADQUIRIDO", "retention_state": "PENDENTE_COMPRESSAO",
                         "is_test": o["is_test"]})
    return {"video_id": vid, "files": [str(dest)], "probe": pr, "availability": av}
