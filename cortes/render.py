"""Agente 06: finalização — enquadramento, legendas embutidas, áudio normalizado."""
from __future__ import annotations

from pathlib import Path

from . import config, cuts, db, media, storage


def video_chain(fmt: str, w: int, h: int, ass_name: str) -> str:
    if fmt == "vertical":
        # Fundo desfocado + vídeo inteiro centralizado: nunca corta elementos visuais importantes.
        return (f"split[bg][fg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"boxblur=20:2,eq=brightness=-0.08[bgb];[fg]scale={w}:-2[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,ass={ass_name},format=yuv420p")
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"ass={ass_name},format=yuv420p")


def render(cut_id: str, ass_path: Path) -> dict:
    cfg = config.load()
    f = cfg["final"]
    cut = db.get("cuts", cut_id)
    video = db.get("videos", cut["video_id"])
    src, offset = storage.cut_source(cut, video)
    ivs = cut["scores"]["intervalos"]
    seek, dur = cuts.input_window(ivs, offset)
    w, h = (f["largura"], f["altura"]) if f["formato"] == "vertical" else (1920, 1080)
    achain = f"loudnorm={f['loudnorm']}" + (",afftdn=nf=-25" if f["reducao_ruido"] else "") + ",aresample=48000"
    graph, vo, ao = cuts.concat_filter(ivs, offset + seek, video_chain(f["formato"], w, h, ass_path.name), achain)
    out = ass_path.with_name("video.mp4")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seek:.3f}", "-t", f"{dur:.3f}",
            "-i", str(src), "-filter_complex", graph, "-map", vo, "-map", ao, "-c:v", "libx264",
            "-preset", f["preset"], "-crf", str(f["crf"]), "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-threads", str(cfg["hardware"]["threads"]), out.name]
    # cwd = pasta da legenda: evita escapar caminhos do Windows dentro do filtro ass=
    r = media.run(args, 3600, cwd=ass_path.parent)
    if r.returncode != 0:
        raise media.MediaError(f"renderização falhou ({r.returncode}): {r.stderr[-400:]}")
    return {"video": str(out), "expected_duration": round(cuts.total(ivs), 3), "width": w, "height": h,
            "exit_code": r.returncode}
