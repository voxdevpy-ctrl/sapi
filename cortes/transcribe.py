"""Agente 03 (parte determinística): transcrição local (faster-whisper) + separação de vozes (sherpa-onnx)."""
from __future__ import annotations

import gc
import json
import tarfile
import urllib.request
import wave
from pathlib import Path

from . import config, cota, db, log, media

AGENT = "03-transcritor"
MODELS = config.ROOT / "modelos"
DIAR_FILES = {
    # pyannote segmentation 3.0 (MIT) convertido para ONNX pelo projeto sherpa-onnx (Apache-2.0)
    "seg": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
            "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
            "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"),
    # WeSpeaker ResNet34 VoxCeleb (Apache-2.0) — embeddings de locutor
    "emb": ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
            "wespeaker_en_voxceleb_resnet34_LM.onnx", "wespeaker_en_voxceleb_resnet34_LM.onnx"),
}


def ensure_models() -> dict:
    """Baixa (uma única vez) os modelos leves de diarização. Retorna caminhos e tamanhos."""
    d = MODELS / "diarizacao"
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for key, (url, rel) in DIAR_FILES.items():
        target = d / rel
        if not target.exists():
            tmp = d / Path(url).name
            urllib.request.urlretrieve(url, tmp)
            log.event(agent=AGENT, requested=f"baixar modelo {key}", performed=url,
                      result=f"{tmp.stat().st_size} bytes", files=[str(tmp)])
            if tmp.suffix == ".bz2":
                with tarfile.open(tmp) as t:
                    t.extractall(d, filter="data")
                tmp.unlink()
        out[key] = str(target)
    return out


def extract_audio(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = media.ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)], 3600)
    if r.returncode != 0:
        raise media.MediaError(f"extração de áudio falhou: {r.stderr[-300:]}")


def read_wav(wav: Path):
    import numpy as np
    with wave.open(str(wav)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise ValueError("WAV precisa ser 16 kHz mono")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def whisper_words(wav: Path, cfg: dict) -> tuple[list[dict], str, float]:
    from faster_whisper import WhisperModel
    t = cfg["transcricao"]
    model = WhisperModel(t["modelo"], device="cpu", compute_type=t["compute_type"],
                         cpu_threads=cfg["hardware"]["threads"], download_root=str(MODELS / "whisper"))
    # Áudio entregue como array (WAV 16 kHz gerado pelo FFmpeg): evita o decodificador PyAV,
    # incompatível entre faster-whisper 1.2.1 e av 19 (argumento metadata_errors removido).
    lang = None if t["idioma"] in ("auto", "", None) else t["idioma"]  # None = detecção automática
    segs, info = model.transcribe(read_wav(wav), language=lang, beam_size=t["beam_size"],
                                  word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
    out = []
    for s in segs:
        out.append({"start": float(s.start), "end": float(s.end), "text": s.text.strip(),
                    "avg_logprob": float(s.avg_logprob), "no_speech": float(s.no_speech_prob),
                    "words": [{"w": w.word, "s": round(float(w.start), 2), "e": round(float(w.end), 2),
                               "p": round(float(w.probability), 3)}
                              for w in (s.words or [])]})
    del model
    gc.collect()
    return out, info.language, round(float(info.language_probability), 3)


def diarize(wav: Path, cfg: dict) -> list[tuple[float, float, int]]:
    import sherpa_onnx
    m = ensure_models()
    sc = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=m["seg"]),
            num_threads=cfg["hardware"]["threads"]),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=m["emb"], num_threads=cfg["hardware"]["threads"]),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=cfg["transcricao"].get("limiar_vozes", 0.6)),
        min_duration_on=0.3, min_duration_off=0.5)
    sd = sherpa_onnx.OfflineSpeakerDiarization(sc)
    res = sd.process(read_wav(wav)).sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in res]


def _speaker_for(s: float, e: float, turns: list[tuple[float, float, int]]) -> int | None:
    """Locutor com maior sobreposição; sem sobreposição (bordas/pausas), o turno mais próximo até 2 s."""
    best, best_ov, near, near_d = None, 0.0, None, 2.0
    for a, b, spk in turns:
        ov = min(b, e) - max(a, s)
        if ov > best_ov:
            best, best_ov = spk, ov
        d = max(a - e, s - b, 0.0)
        if d < near_d:
            near, near_d = spk, d
    return best if best is not None else near


def merge(segments: list[dict], turns: list[tuple[float, float, int]]) -> dict:
    """Atribui locutor por palavra (maior sobreposição) e reagrupa em falas por locutor."""
    labels: dict[int, str] = {}
    utts: list[dict] = []
    for seg in segments:
        words = seg["words"] or [{"w": seg["text"], "s": seg["start"], "e": seg["end"], "p": 0.5}]
        for w in words:
            spk = _speaker_for(w["s"], w["e"], turns)
            lab = "?" if spk is None else labels.setdefault(spk, f"P{len(labels) + 1}")
            if utts and utts[-1]["speaker"] == lab and w["s"] - utts[-1]["end"] < 1.5:
                u = utts[-1]
            else:
                u = {"speaker": lab, "start": w["s"], "end": w["e"], "words": []}
                utts.append(u)
            u["words"].append(w)
            u["end"] = w["e"]
    for i, u in enumerate(utts):
        u["id"] = i
        u["text"] = "".join(w["w"] for w in u["words"]).strip()
        probs = [w["p"] for w in u["words"]]
        u["conf"] = round(sum(probs) / len(probs), 3)
        u["low_conf"] = u["conf"] < 0.55 or sum(p < 0.35 for p in probs) > max(2, len(probs) * 0.25)
    overlaps = []
    for (a1, b1, s1), (a2, b2, s2) in zip(turns, turns[1:]):
        if s1 != s2 and a2 < b1 - 0.3:
            overlaps.append({"start": round(a2, 2), "end": round(min(b1, b2), 2)})
    speakers = {}
    for u in utts:
        sp = speakers.setdefault(u["speaker"], {"total_s": 0.0, "first": u["start"], "turns": 0})
        sp["total_s"] = round(sp["total_s"] + u["end"] - u["start"], 1)
        sp["turns"] += 1
    return {"utterances": utts, "speakers": speakers, "overlaps": overlaps}


def compact(transcript: dict) -> str:
    """Formato enxuto para os agentes de IA: [id|mm:ss-mm:ss|P1|conf] texto."""
    def ts(x):
        return f"{int(x // 60):02d}:{x % 60:05.2f}"
    lines = []
    for u in transcript["utterances"]:
        flag = "*" if u["low_conf"] else ""
        lines.append(f"[{u['id']}|{ts(u['start'])}-{ts(u['end'])}|{u['speaker']}{flag}] {u['text']}")
    return "\n".join(lines)


def relabel(video_id: str, merges: dict[str, str]) -> None:
    """Unifica rótulos de voz (ex.: P3→P2 quando a diarização dividiu uma pessoa). Idempotente."""
    t_row = db.get("transcripts", video_id, "video_id")
    path = Path(t_row["path"])
    t = json.loads(path.read_text("utf-8"))
    if not any(u["speaker"] in merges for u in t["utterances"]):
        return
    for u in t["utterances"]:
        u["speaker"] = merges.get(u["speaker"], u["speaker"])
    speakers: dict = {}
    for u in t["utterances"]:
        sp = speakers.setdefault(u["speaker"], {"total_s": 0.0, "first": u["start"], "turns": 0})
        sp["total_s"] = round(sp["total_s"] + u["end"] - u["start"], 1)
        sp["turns"] += 1
    t["speakers"] = speakers
    t["merged_labels"] = {**t.get("merged_labels", {}), **merges}
    path.write_text(json.dumps(t, ensure_ascii=False), "utf-8")
    path.with_name("transcricao_compacta.txt").write_text(compact(t), "utf-8")


def transcribe(video_id: str, task_id: str | None = None) -> dict:
    cfg = config.load()
    v = db.get("videos", video_id)
    if media.free_ram_mb() < cfg["hardware"]["ram_minima_mb"]:
        raise MemoryError(f"RAM livre insuficiente ({media.free_ram_mb()} MB); tarefa será reagendada")
    work = config.DIRS["intermediarios"] / video_id
    wav = work / "audio16k.wav"
    cota.ensure(int(float(v.get("duration") or 0) * 32000) + 50 * (1 << 20), "o áudio da transcrição")
    extract_audio(Path(v["compressed_path"]), wav)
    segs, lang, lang_p = whisper_words(wav, cfg)
    log.event(task_id=task_id, item_id=video_id, agent=AGENT, requested="transcrever",
              result=f"{len(segs)} segmentos, idioma {lang} (confiança {lang_p})")
    turns = diarize(wav, cfg)
    log.event(task_id=task_id, item_id=video_id, agent=AGENT, requested="separar vozes",
              result=f"{len(turns)} turnos, {len({t[2] for t in turns})} vozes")
    t = merge(segs, turns)
    t.update({"video_id": video_id, "language": lang, "language_prob": lang_p, "duration": v["probe"]["duration"],
              "model": cfg["transcricao"]["modelo"], "diarization_turns": len(turns)})
    path = work / "transcricao.json"
    path.write_text(json.dumps(t, ensure_ascii=False), "utf-8")
    (work / "transcricao_compacta.txt").write_text(compact(t), "utf-8")
    wav.unlink(missing_ok=True)  # intermediário temporário reconstituível
    low = [{"id": u["id"], "start": u["start"], "end": u["end"]} for u in t["utterances"] if u["low_conf"]]
    db.insert("transcripts", {"video_id": video_id, "path": str(path), "language": lang,
                              "participants": {k: {"label": k, "name": None, **s} for k, s in t["speakers"].items()},
                              "low_conf": low, "created_at": config.iso()}, replace=True)
    return {"files": [str(path), str(work / "transcricao_compacta.txt")], "utterances": len(t["utterances"]),
            "speakers": list(t["speakers"]), "low_conf": len(low), "language": lang, "language_prob": lang_p}
