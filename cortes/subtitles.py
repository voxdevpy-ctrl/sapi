"""Agente 05 (parte determinística): blocos de legenda sincronizados, SRT e ASS."""
from __future__ import annotations

import re
from pathlib import Path

from . import config

REVIEW_SCHEMA = {
    "type": "object", "required": ["blocos", "incertezas"],
    "properties": {
        "blocos": {"type": "array", "items": {"type": "object", "required": ["i", "texto", "destaque"],
                   "properties": {"i": {"type": "integer"}, "texto": {"type": "string"},
                                  "destaque": {"type": "string", "description": "uma palavra do bloco ou vazio"}}}},
        "incertezas": {"type": "array", "items": {"type": "string"}}},
}


TRANSLATION_AUDIT_SCHEMA = {
    "type": "object", "required": ["avaliacoes", "resumo"],
    "properties": {
        "resumo": {"type": "string"},
        "avaliacoes": {"type": "array", "items": {"type": "object", "required": ["i", "veredito", "motivo"],
                       "properties": {"i": {"type": "integer"}, "veredito": {"type": "string", "enum": ["OK", "ERRO"]},
                                      "motivo": {"type": "string"}}}}},
}


def _balanced(text: str, limit: int) -> list[str]:
    """Divide em N pedaços de tamanho parecido (evita sobra de uma palavra solta no fim)."""
    words = text.split()
    if len(text) <= limit or len(words) < 2:
        return [text]
    n = -(-len(text) // limit)
    while True:
        target, pieces, cur = len(text) / n, [], ""
        for w in words:
            if cur and (len(cur) + 1 + len(w) > limit or (len(cur) >= target and len(pieces) < n - 1)):
                pieces.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        pieces.append(cur)
        if all(len(p) <= limit for p in pieces) or n >= len(words):
            return pieces
        n += 1


def fit(blocks: list[dict], max_chars: int) -> list[dict]:
    """Garante no máximo 2 linhas por legenda: blocos longos (comum após tradução) são divididos
    em sequência, repartindo o tempo do bloco proporcionalmente ao texto. Nunca altera o início
    do primeiro pedaço nem o fim do último (a sincronia com a fala é preservada)."""
    out = []
    limit = max_chars * 2
    for b in blocks:
        pieces = _balanced(b["text"], limit)
        total = sum(len(p) for p in pieces) or 1
        t = b["start"]
        for k, p in enumerate(pieces):
            end = b["end"] if k == len(pieces) - 1 else round(t + (b["end"] - b["start"]) * len(p) / total, 3)
            hl = b.get("destaque") if b.get("destaque") and b["destaque"].lower() in p.lower() else ""
            out.append({**b, "start": t, "end": end, "text": p, "destaque": hl, "src_i": b["i"]})
            t = end
    for n, b in enumerate(out):
        b["i"] = n
    return out


def map_time(t: float, intervals, snap: bool = False) -> float | None:
    """Tempo no original → tempo no corte (None se caiu num silêncio removido; com snap, gruda na emenda)."""
    acc = 0.0
    for a, b in intervals:
        if a <= t <= b:
            return acc + t - a
        if snap and t < a:
            return acc
        acc += b - a
    return acc if snap else None


def build_blocks(utterances: list[dict], intervals, max_chars: int) -> list[dict]:
    start, end = intervals[0][0], intervals[-1][1]
    blocks: list[dict] = []
    cur = None
    for u in utterances:
        for w in u["words"]:
            if w["e"] <= start or w["s"] >= end:
                continue
            # snap: a ponta de uma palavra que o Whisper esticou para dentro do silêncio cortado gruda na emenda
            s, e = map_time(max(w["s"], start), intervals, True), map_time(min(w["e"], end), intervals, True)
            if e - s < 0.02:
                e = s + 0.02
            if s is None or e is None:
                continue
            text = w["w"].strip()
            if not text:
                continue
            new = (cur is None or cur["speaker"] != u["speaker"] or s - cur["end"] > 0.8
                   or len(cur["text"]) + len(text) + 1 > max_chars * 2 or e - cur["start"] > 3.5
                   or cur["text"].endswith((".", "?", "!")) and e - cur["start"] > 1.2)
            if new:
                cur = {"start": s, "end": e, "text": text, "speaker": u["speaker"], "low_conf": False}
                blocks.append(cur)
            else:
                cur["text"] += " " + text
                cur["end"] = e
            cur["low_conf"] |= w["p"] < 0.35
    total = sum(b - a for a, b in intervals) - 0.05  # nenhuma legenda passa do fim do vídeo (QC srt_sincronia)
    blocks = [b for b in blocks if b["start"] < total - 0.1]
    for i, b in enumerate(blocks):
        b["i"] = i
        b["end"] = max(b["end"], b["start"] + 0.7)
        if i + 1 < len(blocks):
            b["end"] = min(b["end"], blocks[i + 1]["start"] - 0.02)
        b["end"] = round(min(b["end"], total), 3)
    return blocks


def two_lines(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    best, best_diff = [text], 1e9
    for k in range(1, len(words)):
        l1, l2 = " ".join(words[:k]), " ".join(words[k:])
        diff = abs(len(l1) - len(l2))
        if diff < best_diff:
            best, best_diff = [l1, l2], diff
    return best


def _ts(t: float, sep=",") -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_srt(blocks, path: Path, max_chars: int) -> None:
    out = []
    for n, b in enumerate(blocks, 1):
        out.append(f"{n}\n{_ts(b['start'])} --> {_ts(b['end'])}\n" + "\n".join(two_lines(b["text"], max_chars)) + "\n")
    path.write_text("\n".join(out), "utf-8")


def write_ass(blocks, path: Path, style: dict, width: int, height: int) -> None:
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,{style['fonte']},{style['tamanho']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,4,1,2,60,60,{style['margem_v']},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for b in blocks:
        txt = r"\N".join(two_lines(b["text"], style["max_chars"]))
        txt = txt.replace("{", "(").replace("}", ")")
        hl = (b.get("destaque") or "").strip()
        if hl:
            txt = re.sub(rf"(?<!\w)({re.escape(hl)})(?!\w)", r"{\\c&H00D7FF&}\1{\\c&HFFFFFF&}", txt, count=1)
        lines.append(f"Dialogue: 0,{_ts(b['start'], '.')[1:-1]},{_ts(b['end'], '.')[1:-1]},Default,,0,0,0,,{txt}")
    path.write_text(head + "\n".join(lines) + "\n", "utf-8")


def parse_srt(path: Path) -> list[dict]:
    """Leitura estrita para validação estrutural."""
    raw = Path(path).read_text("utf-8").strip()
    if not raw:
        raise ValueError("SRT vazio")
    out = []
    for chunk in re.split(r"\n\s*\n", raw):
        lines = chunk.splitlines()
        m = re.match(r"(\d\d):(\d\d):(\d\d),(\d{3}) --> (\d\d):(\d\d):(\d\d),(\d{3})$", lines[1] if len(lines) > 1 else "")
        if not lines[0].strip().isdigit() or not m or len(lines) < 3:
            raise ValueError(f"bloco SRT inválido: {chunk[:60]!r}")
        g = list(map(int, m.groups()))
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        out.append({"n": int(lines[0]), "start": s, "end": e, "lines": lines[2:]})
    return out


def style() -> dict:
    return config.load()["final"]["legenda"]
