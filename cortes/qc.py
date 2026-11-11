"""Agente 07: controle de qualidade independente do arquivo real produzido."""
from __future__ import annotations

from pathlib import Path

from . import media, subtitles

REQUIRED_TESTS = ["existe", "reproduz", "duracao", "resolucao", "audio", "clipping", "tela_preta",
                  "silencio", "srt_valida", "srt_sincronia", "legenda_na_tela", "inicio_fim"]


def check(video: Path, srt: Path, expected_duration: float, width: int, height: int,
          style: dict, preview_duration: float | None = None) -> dict:
    t: dict[str, dict] = {}

    def add(name, ok, detail, severe=True):
        t[name] = {"ok": bool(ok), "detalhe": detail, "grave": severe}

    add("existe", video.exists() and video.stat().st_size > 10_000, f"{video.stat().st_size if video.exists() else 0} bytes")
    if not t["existe"]["ok"]:
        return {"status": "REPROVADO", "testes": t}
    ok, err = media.decode_check(video, 900)
    add("reproduz", ok, err or "decodificação completa sem erros")
    p = media.probe(video)
    add("duracao", abs(p["duration"] - expected_duration) <= 0.35,
        f"{p['duration']:.2f}s (esperado {expected_duration:.2f}s)")
    v = p["video"] or {}
    add("resolucao", (v.get("width"), v.get("height")) == (width, height), f"{v.get('width')}x{v.get('height')}")
    add("audio", bool(p["audio"]), str(p["audio"]))
    vol = media.volume_stats(video)
    add("clipping", vol["max_db"] is not None and vol["max_db"] <= -0.1, f"pico {vol['max_db']} dB, média {vol['mean_db']} dB")
    blacks = media.detect(video, "black")
    add("tela_preta", not [b for b in blacks if b[1] - b[0] > 1.0], f"{len(blacks)} intervalos pretos")
    sil = media.detect(video, "silence")
    long_sil = [s for s in sil if min(s[1], p["duration"]) - s[0] > 3]
    add("silencio", not long_sil, f"{len(long_sil)} silêncios >3s", severe=False)
    try:
        blocks = subtitles.parse_srt(srt)
        add("srt_valida", bool(blocks), f"{len(blocks)} blocos")
        overl = [b for a, b in zip(blocks, blocks[1:]) if b["start"] < a["end"]]
        outside = [b for b in blocks if b["end"] > p["duration"] + 0.05 or b["start"] < 0]
        add("srt_sincronia", not overl and not outside, f"{len(overl)} sobreposições, {len(outside)} fora da duração")
        widest = max((len(line) for b in blocks for line in b["lines"]), default=0)
        approx_px = widest * style["tamanho"] * 0.55
        lines_ok = all(len(b["lines"]) <= 2 for b in blocks)
        add("legenda_na_tela", approx_px <= width - 120 and lines_ok,
            f"linha mais larga ~{approx_px:.0f}px de {width - 120}px úteis; máx. 2 linhas: {lines_ok}")
    except (ValueError, OSError) as e:
        add("srt_valida", False, str(e))
        add("srt_sincronia", False, "não avaliável")
        add("legenda_na_tela", False, "não avaliável")
    if preview_duration is not None:
        add("inicio_fim", abs(preview_duration - p["duration"]) <= 0.35,
            f"final {p['duration']:.2f}s vs prévia aprovada {preview_duration:.2f}s")
    else:
        add("inicio_fim", False, "prévia aprovada indisponível para comparação")
    severe_fail = [k for k, x in t.items() if not x["ok"] and x["grave"]]
    soft_fail = [k for k, x in t.items() if not x["ok"] and not x["grave"]]
    status = "REPROVADO" if severe_fail else "APROVADO_COM_RESSALVAS" if soft_fail else "APROVADO"
    return {"status": status, "testes": t, "falhas": severe_fail, "ressalvas": soft_fail, "probe": p}
