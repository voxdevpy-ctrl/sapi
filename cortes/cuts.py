"""Agente 04 (parte determinística): materializa candidatos, remove silêncios internos e gera prévias."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, db, media, storage

AGENT = "04-editor"
DRAMATICA = ("?", "!", "…", "...", ":")   # depois disso a pausa dá sentido à frase: fica mais longa
FIM_FRASE = (".", ";")

SELECTION_SCHEMA = {
    "type": "object", "required": ["cortes", "observacoes"],
    "properties": {
        "observacoes": {"type": "string"},
        "cortes": {"type": "array", "items": {
            "type": "object",
            "required": ["inicio_id", "fim_id", "titulo_provisorio", "assunto", "resumo", "motivo",
                         "contexto_anterior", "contexto_posterior", "notas", "abertura", "estilo", "alertas"],
            "properties": {
                "inicio_id": {"type": "integer"}, "fim_id": {"type": "integer"},
                "titulo_provisorio": {"type": "string"}, "assunto": {"type": "string"},
                "resumo": {"type": "string"}, "motivo": {"type": "string"},
                "contexto_anterior": {"type": "string"}, "contexto_posterior": {"type": "string"},
                "abertura": {"type": "string", "description": "pergunta|afirmacao_forte|historia|humor|dado|outro"},
                "estilo": {"type": "string", "description": "historia|explicacao|debate|humor|emocao|opiniao|outro"},
                "alertas": {"type": "array", "items": {"type": "string"}},
                "notas": {"type": "object", "required": list(config.DEFAULTS["cortes"]["pesos"]),
                          "properties": {k: {"type": "number", "minimum": 0, "maximum": 10}
                                         for k in config.DEFAULTS["cortes"]["pesos"]}}}}}},
}


def load_transcript(video_id: str) -> dict:
    t = db.get("transcripts", video_id, "video_id")
    return json.loads(Path(t["path"]).read_text("utf-8"))


def silences(src: Path, offset: float, start: float, end: float, db_th: float) -> list[tuple[float, float]]:
    """Silêncios reais do áudio no trecho do corte (tempo do original). Vazio se não der para medir."""
    try:
        r = media.run(["ffmpeg", "-hide_banner", "-ss", f"{max(0.0, start - offset):.3f}", "-t", f"{end - start:.3f}",
                       "-i", str(src), "-vn", "-af", f"silencedetect=n={db_th}dB:d=0.15", "-f", "null", "-"], 600)
    except Exception:
        return []
    base = max(0.0, start - offset) + offset
    st = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr)]
    en = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", r.stderr)]
    return [(base + x, base + y) for x, y in zip(st, en + [end - base] * (len(st) - len(en)))]


def _pause_to_keep(prev: dict, same_speaker: bool, r: dict) -> float:
    w = prev["w"].strip()
    if not same_speaker:
        return r["pausa_pergunta_resposta"] if w.endswith("?") else r["pausa_troca_locutor"]
    if w.endswith(DRAMATICA):
        return r["pausa_dramatica"]
    if w.endswith(FIM_FRASE):
        return r["pausa_fim_frase"]
    return r["pausa_meio_frase"]


def keep_intervals(utts: list[dict], start: float, end: float, sil: list | None = None,
                   ritmo: dict | None = None) -> list[list[float]]:
    """Faixa do corte menos o silêncio inútil entre palavras.

    Cada pausa é reduzida ao tamanho que o contexto pede (meio de frase, fim de frase, pausa dramática,
    troca de locutor). `sil` (silêncios medidos no áudio) corrige o fim das palavras, que o Whisper
    costuma esticar para dentro do silêncio.
    """
    r = {**config.DEFAULTS["ritmo"], **(ritmo or {})}
    words = [(w, u["speaker"]) for u in utts for w in u["words"] if w["e"] > start and w["s"] < end]
    words.sort(key=lambda x: x[0]["s"])
    remove = []
    for (a, sa), (b, sb) in zip(words, words[1:]):
        gs, ge = a["e"], b["s"]
        for s0, s1 in sil or []:
            if s0 < ge + 0.05 and s1 > gs - 0.05:   # silêncio medido encostado nesta pausa
                gs = min(gs, max(s0, a["s"] + min(0.6 * (a["e"] - a["s"]), 0.3)))
                ge = max(ge, min(s1, b["s"] + 0.1))
        keep = _pause_to_keep(a, sa == sb, r)
        if ge - gs - keep < r["min_removido"]:
            continue
        cut_from = max(gs + keep * 0.55, gs + r["folga_palavra"])   # um pouco mais de respiro no fim da fala
        cut_to = min(ge - keep * 0.45, ge - r["folga_palavra"])
        if cut_to - cut_from >= r["min_removido"]:
            remove.append((cut_from, cut_to))
    out, cur = [], start
    for cf, ct in remove:
        if cf - cur < r["min_trecho"] or ct >= end - r["min_trecho"]:
            continue   # não cria pedacinhos que dão "soluço" na imagem
        out.append([cur, cf])
        cur = ct
    out.append([cur, end])
    return [[round(x, 3), round(y, 3)] for x, y in out]


def total(intervals) -> float:
    return sum(b - a for a, b in intervals)


def score(notas: dict, pesos: dict) -> float:
    return round(sum(pesos[k] * float(notas.get(k, 0)) for k in pesos) / sum(pesos.values()), 2)


def materialize(video_id: str, selection: dict, participants: dict) -> list[str]:
    """Converte a resposta do editor em registros de corte (estado PENDENTE até a prévia)."""
    cfg = config.load()["cortes"]
    tr = load_transcript(video_id)
    utts = {u["id"]: u for u in tr["utterances"]}
    ritmo = config.load()["ritmo"]
    video = db.get("videos", video_id)
    try:
        src, off = storage.cut_source({}, video)
    except FileNotFoundError:
        src, off = None, 0.0

    def sil_for(a: float, b: float) -> list:
        return silences(src, off, a, b, ritmo["silencio_db"]) if src else []

    ids = []
    for c in selection["cortes"]:
        a, b = utts.get(c["inicio_id"]), utts.get(c["fim_id"])
        if not a or not b or b["id"] < a["id"]:
            continue  # supervisor editorial registra a inconsistência
        start = max(0.0, a["start"] - 0.15)
        end = min(tr["duration"], b["end"] + 0.35)
        span = [u for u in tr["utterances"] if a["id"] <= u["id"] <= b["id"]]
        intervals = keep_intervals(span, start, end, sil_for(start, end), ritmo)
        speakers = sorted({u["speaker"] for u in span})
        text = "\n".join(f"{participants.get(u['speaker'], {}).get('name') or u['speaker']}: {u['text']}" for u in span)
        alerts = list(c["alertas"])
        if any(u["low_conf"] for u in span):
            alerts.append("contém trechos de baixa confiança na transcrição")
        cid = db.new_id("cut")
        dur = total(intervals)
        db.insert("cuts", {
            "id": cid, "video_id": video_id, "start": start, "end": end, "title_tmp": c["titulo_provisorio"],
            "topic": c["assunto"], "summary": c["resumo"], "transcript": text,
            "participants": {s: participants.get(s, {"label": s}) for s in speakers},
            "context_before": c["contexto_anterior"], "context_after": c["contexto_posterior"],
            "reason": c["motivo"], "scores": {"notas": c["notas"], "intervalos": intervals,
                                              "ids": [a["id"], b["id"]], "ritmo_versao": ritmo["versao"],
                                              "silencio_removido_s": round(end - start - total(intervals), 2)},
            "score_total": score(c["notas"], cfg["pesos"]), "alerts": alerts,
            "features": {"estilo": c["estilo"], "abertura": c["abertura"], "assunto": c["assunto"].lower(),
                         "duracao": "curto" if dur < 35 else "medio" if dur < 60 else "longo",
                         "participantes": [participants.get(s, {}).get("name") or s for s in speakers]},
            "criteria_version": cfg["versao_criterios"], "state": "PENDENTE", "created_at": config.iso(),
            "is_test": db.get("videos", video_id)["is_test"]})
        ids.append(cid)
    return ids


def concat_filter(intervals, offset: float, video_chain: str = "", audio_chain: str = "") -> tuple[str, str, str]:
    parts, labels = [], []
    for i, (a, b) in enumerate(intervals):
        a, b = a - offset, b - offset
        fade = ""
        if len(intervals) > 1:   # micro-fade de 12 ms em cada emenda: evita estalo no áudio
            fade = f",afade=t=in:d=0.012,afade=t=out:st={max(0.0, b - a - 0.012):.3f}:d=0.012"
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS{fade}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    graph = ";".join(parts) + f";{''.join(labels)}concat=n={len(intervals)}:v=1:a=1[vc][ac]"
    vout, aout = "[vc]", "[ac]"
    if video_chain:
        graph += f";[vc]{video_chain}[vo]"
        vout = "[vo]"
    if audio_chain:
        graph += f";[ac]{audio_chain}[ao]"
        aout = "[ao]"
    return graph, vout, aout


def input_window(intervals, offset: float) -> tuple[float, float]:
    """Busca rápida (-ss) até perto do corte para não decodificar o vídeo inteiro."""
    first = intervals[0][0] - offset
    seek = max(0.0, first - 2)
    return seek, intervals[-1][1] - offset - seek + 1


def make_preview(cut_id: str) -> str:
    cfg = config.load()
    cut = db.get("cuts", cut_id)
    video = db.get("videos", cut["video_id"])
    src, offset = storage.cut_source(cut, video)
    ivs = cut["scores"]["intervalos"]
    seek, dur = input_window(ivs, offset)
    graph, vo, ao = concat_filter(ivs, offset + seek, f"scale=-2:{cfg['previa']['altura']}")
    out = config.DIRS["intermediarios"] / "previas" / f"{cut_id}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = media.ffmpeg(["-ss", f"{seek:.3f}", "-t", f"{dur:.3f}", "-i", str(src), "-filter_complex", graph,
                      "-map", vo, "-map", ao, "-c:v", "libx264", "-preset", "veryfast",
                      "-crf", str(cfg["previa"]["crf"]), "-c:a", "aac", "-b:a", "96k",
                      "-movflags", "+faststart", "-threads", str(cfg["hardware"]["threads"]), str(out)], 1800)
    if r.returncode != 0:
        raise media.MediaError(f"prévia falhou: {r.stderr[-300:]}")
    db.update("cuts", cut_id, {"preview_path": str(out)})
    return str(out)
