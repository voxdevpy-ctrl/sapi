"""Compressão dos originais, validação de integridade e retenção de 14 dias."""
from __future__ import annotations

from pathlib import Path

from . import config, cota, db, log, media

AGENT = "02-localizador"
NEEDS_SOURCE = ("PENDENTE", "AGUARDANDO_APROVACAO", "ADIADO", "APROVADO", "BLOQUEADO")
SOURCE_STAGES = ("COMPRESSAO", "TRANSCRICAO", "SELECAO", "PREVIA")


class NoSpace(RuntimeError):
    pass


def _inside_originais(p: str | Path) -> bool:
    try:
        return Path(p).resolve().is_relative_to(config.DIRS["originais"].resolve())
    except OSError:
        return False


def validate_copy(src_probe: dict, path: Path) -> list[str]:
    """Lista de problemas da cópia (vazia = válida)."""
    problems = []
    if not path.exists() or path.stat().st_size == 0:
        return ["arquivo ausente ou vazio"]
    try:
        p = media.probe(path)
    except media.MediaError as e:
        return [str(e)]
    if src_probe.get("video") and not p["video"]:
        problems.append("sem trilha de vídeo")
    if src_probe.get("audio") and not p["audio"]:
        problems.append("sem trilha de áudio")
    tol = max(0.5, src_probe["duration"] * 0.005)
    if abs(p["duration"] - src_probe["duration"]) > tol:
        problems.append(f"duração divergente {p['duration']:.2f}s vs {src_probe['duration']:.2f}s")
    ok, err = media.decode_check(path, max(1800, int(p["duration"] * 1.5)))  # lives de 2 h levam mais de 30 min neste PC
    if not ok:
        problems.append(f"falha na decodificação: {err}")
    return problems


def compress(video_id: str, task_id: str | None = None) -> dict:
    """Gera e valida a cópia de trabalho. NÃO apaga o original: isso só ocorre em finalize_compression,
    depois que o supervisor aprova. Idempotente: uma repetição reaproveita a cópia já registrada."""
    cfg = config.load()
    c, hw = cfg["compressao"], cfg["hardware"]
    v = db.get("videos", video_id)
    src = Path(v["original_path"])
    out = src.with_name(f"{video_id}.comprimido.mp4")
    if v["compressed_path"] and Path(v["compressed_path"]).exists()             and media.sha256(v["compressed_path"]) == v["compressed_sha256"]:
        return {"files": [v["compressed_path"]], "note": v["compression_note"], "reused": True}
    if not src.exists():
        raise FileNotFoundError(f"original ausente e nenhuma cópia validada registrada: {src}")
    sp = media.probe(src)
    need = src.stat().st_size // (1 << 20) + hw["disco_margem_mb"]
    if media.free_disk_mb(src.parent) < need:
        raise NoSpace(f"espaço insuficiente: precisa ~{need} MB livres; original preservado")

    playable_original = src.suffix.lower() in (".mp4", ".m4v", ".mov", ".mkv", ".webm")
    bitrate = src.stat().st_size * 8 / max(1.0, sp["duration"])
    if playable_original and bitrate < c.get("pular_abaixo_mbps", 1.0) * 1e6:
        # Medido neste PC: abaixo de ~1 Mbps (padrão do YouTube) o encode levava até 1 h e não economizava nada
        problems = validate_copy(sp, src)
        if problems:
            raise media.MediaError("original reprovado na validação: " + "; ".join(problems))
        h = media.sha256(src)
        note = f"original já eficiente ({bitrate / 1e6:.2f} Mbps): compressão pulada, integridade validada"
        acquired = config.parse(v["acquired_at"])
        data = {"compressed_path": str(src), "compressed_size": src.stat().st_size, "compressed_sha256": h,
                "original_sha256": h, "original_size": src.stat().st_size, "compressed_at": config.iso(),
                "compression_note": note, "probe": sp,
                "retention_until": config.iso(acquired + config.timedelta(days=cfg["retencao_dias"])),
                "retention_state": "ATIVO"}
        db.update("videos", video_id, data)
        log.event(task_id=task_id, item_id=video_id, agent=AGENT, requested="comprimir original", result=note)
        return {"files": [str(src)], "note": note, "sha256": h, "saving": 0.0, "skipped": True, **data}

    tmp = src.with_name(f"{video_id}.comp.tmp.mp4")
    tmp.unlink(missing_ok=True)
    # a cópia é no máximo do tamanho do original (senão a economia mínima reprova); teto vigiado pelo ffmpeg (-fs)
    cota.ensure(min(src.stat().st_size, int(sp["duration"] * 3e6 / 8)), "a compressão")
    fs = cota.room() - 64 * (1 << 20)
    vf = f"scale=-2:'min({c['altura_max']},ih)'"
    args = ["-i", str(src), "-map", "0:v:0?", "-map", "0:a:0?", "-vf", vf, "-c:v", c["codec"],
            "-preset", c["preset"], "-crf", str(c["crf"]), "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-b:a", f"{c['audio_kbps']}k", "-movflags", "+faststart", "-threads", str(hw["threads"]),
            "-fs", str(max(fs, 1 << 20)), str(tmp)]
    r = media.ffmpeg(args, timeout=6 * 3600)
    if tmp.exists() and tmp.stat().st_size >= fs - 8 * (1 << 20):
        tmp.unlink(missing_ok=True)
        raise cota.LimitReached("armazenamento: a compressão passaria do teto; original preservado, aguardando espaço")
    log.event(task_id=task_id, item_id=video_id, agent=AGENT, requested="comprimir original",
              performed=" ".join(args[:2] + ["..."]), exit_code=r.returncode, error=r.stderr[-300:] or None)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise media.MediaError(f"ffmpeg falhou ({r.returncode}); original preservado")
    problems = validate_copy(sp, tmp)
    if problems:
        tmp.unlink(missing_ok=True)
        raise media.MediaError("cópia comprimida reprovada: " + "; ".join(problems) + "; original preservado")

    orig_size, comp_size = src.stat().st_size, tmp.stat().st_size
    saving = 1 - comp_size / orig_size
    orig_hash = media.sha256(src)
    if saving < c["economia_minima"] and playable_original and not validate_copy(sp, src):
        tmp.unlink(missing_ok=True)
        note = f"recompressão economizaria só {saving:.0%}; original já eficiente mantido como cópia de trabalho"
        final, final_size, h = src, orig_size, orig_hash
    else:
        tmp.replace(out)
        h = media.sha256(out)
        note = f"economia de {saving:.0%} ({orig_size >> 20} MB → {comp_size >> 20} MB)"
        final, final_size = out, comp_size

    acquired = config.parse(v["acquired_at"])
    data = {"compressed_path": str(final), "compressed_size": final_size, "compressed_sha256": h,
            "original_sha256": orig_hash, "original_size": orig_size, "compressed_at": config.iso(),
            "compression_note": note, "probe": media.probe(final),
            "retention_until": config.iso(acquired + config.timedelta(days=cfg["retencao_dias"])),
            "retention_state": "ATIVO"}
    db.update("videos", video_id, data)
    return {"files": [str(final)], "note": note, "sha256": h, "saving": saving, **data}


def finalize_compression(video_id: str) -> int:
    """Remove o original não comprimido após a auditoria aprovar a cópia (idempotente)."""
    v = db.get("videos", video_id)
    src, comp = Path(v["original_path"]), Path(v["compressed_path"] or "")
    if src == comp or not src.exists():
        return 0
    if not (comp.exists() and media.sha256(comp) == v["compressed_sha256"]):
        raise media.MediaError("cópia de trabalho não confere com o hash registrado; original preservado")
    freed = _delete(src, video_id, "ORIGINAL_REMOVIDO", v["compression_note"] or "cópia validada")
    db.update("videos", video_id, {"freed_bytes": (v["freed_bytes"] or 0) + freed})
    return freed


def cut_source(cut: dict, video: dict) -> tuple[Path, float]:
    """Arquivo-fonte de um corte e o deslocamento de tempo (trecho preservado ou original comprimido)."""
    if cut.get("segment_path") and Path(cut["segment_path"]).exists():
        return Path(cut["segment_path"]), float(cut.get("segment_offset") or 0)
    if not video.get("compressed_path") or not Path(video["compressed_path"]).exists():
        raise FileNotFoundError("material de origem indisponível (já removido pela retenção?)")
    return Path(video["compressed_path"]), 0.0


def _preserve_segment(cut: dict, video: dict) -> None:
    src = Path(video["compressed_path"])
    start = max(0.0, cut["start"] - 3)
    dur = cut["end"] + 3 - start
    out = config.DIRS["originais"] / video["id"] / "trechos" / f"{cut['id']}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = media.ffmpeg(["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}", "-c:v", "libx264",
                      "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out)], 3600)
    if r.returncode != 0 or validate_copy({"duration": min(dur, video["probe"]["duration"] - start),
                                           "video": True, "audio": True}, out):
        out.unlink(missing_ok=True)
        raise media.MediaError(f"falha ao preservar trecho do corte {cut['id']}")
    db.update("cuts", cut["id"], {"segment_path": str(out), "segment_offset": start})


def _delete(path: Path, video_id: str, action: str, detail: str) -> int:
    if not _inside_originais(path):
        log.event(item_id=video_id, agent="retencao", result="RECUSADO: fora da categoria ORIGINAIS",
                  files=[str(path)], state="BLOQUEADO")
        return 0
    size = path.stat().st_size if path.exists() else 0
    path.unlink(missing_ok=True)
    db.insert("retention_log", {"ts": config.iso(), "video_id": video_id, "action": action,
                                "path": str(path), "bytes": size, "detail": detail})
    return size


def retention_sweep() -> dict:
    """Idempotente: pode rodar a qualquer momento, inclusive após dias desligado."""
    now = config.now()
    summary = {"excluidos": 0, "excecoes": 0, "bytes": 0, "trechos_removidos": 0}
    for v in db.query("SELECT * FROM videos WHERE retention_state IN ('ATIVO','EXCECAO') AND retention_until IS NOT NULL"):
        if config.parse(v["retention_until"]) > now:
            continue
        active = db.query(f"SELECT stage FROM tasks WHERE item_id=? AND stage IN ({','.join('?'*len(SOURCE_STAGES))}) "
                          f"AND state IN {db.ACTIVE}", (v["id"], *SOURCE_STAGES))
        if active:
            if v["retention_state"] != "EXCECAO":
                db.update("videos", v["id"], {"retention_state": "EXCECAO"})
                db.insert("retention_log", {"ts": config.iso(), "video_id": v["id"], "action": "EXCECAO",
                                            "path": v["compressed_path"], "bytes": 0,
                                            "detail": "etapas ativas: " + ",".join(a["stage"] for a in active)})
            summary["excecoes"] += 1
            continue
        path = Path(v["compressed_path"] or "")
        if path.exists() and not _inside_originais(path):
            log.event(item_id=v["id"], agent="retencao", result="RECUSADO: fora da categoria ORIGINAIS",
                      files=[str(path)], state="BLOQUEADO")
            summary["excecoes"] += 1
            continue
        try:
            for cut in db.query(f"SELECT * FROM cuts WHERE video_id=? AND state IN {NEEDS_SOURCE} "
                                "AND (segment_path IS NULL OR segment_path='')", (v["id"],)):
                if path.exists():
                    _preserve_segment(cut, v)
        except media.MediaError as e:
            db.update("videos", v["id"], {"retention_state": "EXCECAO"})
            db.insert("retention_log", {"ts": config.iso(), "video_id": v["id"], "action": "EXCECAO",
                                        "path": str(path), "bytes": 0, "detail": str(e)})
            summary["excecoes"] += 1
            continue
        try:
            freed = _delete(path, v["id"], "COMPRIMIDO_EXCLUIDO", "retenção vencida") if str(path) not in ("", ".") else 0
        except OSError as e:  # ex.: arquivo aberto por outro processo no Windows — tenta na próxima varredura
            db.insert("retention_log", {"ts": config.iso(), "video_id": v["id"], "action": "EXCECAO",
                                        "path": str(path), "bytes": 0, "detail": f"exclusão adiada: {e}"})
            summary["excecoes"] += 1
            continue
        db.update("videos", v["id"], {"retention_state": "EXCLUIDO", "deleted_at": config.iso(),
                                      "freed_bytes": (v["freed_bytes"] or 0) + freed})
        summary["excluidos"] += 1
        summary["bytes"] += freed
    for cut in db.query("SELECT * FROM cuts WHERE segment_path IS NOT NULL AND segment_path<>'' "
                        "AND state IN ('REJEITADO','CONCLUIDO')"):
        summary["bytes"] += _delete(Path(cut["segment_path"]), cut["video_id"], "TRECHO_EXCLUIDO",
                                    f"corte {cut['id']} encerrado ({cut['state']})")
        db.update("cuts", cut["id"], {"segment_path": ""})
        summary["trechos_removidos"] += 1
    log.event(agent="retencao", requested="varredura de retenção", result=str(summary), state="CONCLUIDO")
    return summary


def storage_overview() -> dict:
    vids = db.query("SELECT * FROM videos")
    live = [v for v in vids if v["retention_state"] in ("ATIVO", "EXCECAO")]
    occupied = sum(Path(v["compressed_path"]).stat().st_size for v in live
                   if v["compressed_path"] and Path(v["compressed_path"]).exists())
    saved = sum(max(0, (v["original_size"] or 0) - (v["compressed_size"] or 0)) for v in vids if v["compressed_size"])
    return {"ocupado_originais": occupied, "economia_compressao": saved,
            "disco_livre_mb": media.free_disk_mb(config.PROD),
            "proximas_exclusoes": sorted(({"video": v["id"], "titulo": v["title"], "quando": v["retention_until"],
                                           "estado": v["retention_state"]} for v in live), key=lambda x: x["quando"] or ""),
            "limpezas": db.query("SELECT * FROM retention_log ORDER BY id DESC LIMIT 50")}
