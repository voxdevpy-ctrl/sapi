"""Aprendizado editorial explicável + recomendação com exploração, diversidade e segunda chance.

Não é um modelo de IA treinado: são pesos por característica calculados a partir das decisões,
com decaimento temporal (recência) e suavização bayesiana (uma avaliação isolada pesa pouco).
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from . import config, db

STOP = set("a o e de da do das dos em no na nos nas um uma para por com que se ao à é os as sobre como "
           "mais the of and to in".split())


def keywords(text: str, n: int = 3) -> list[str]:
    words = [w for w in re.findall(r"[a-zà-ú0-9]{4,}", (text or "").lower()) if w not in STOP]
    return list(dict.fromkeys(words))[:n]


def opportunity_features(o: dict) -> dict:
    return {"nicho": (o.get("niche") or "").lower() or None, "canal": o.get("channel_id") or o.get("channel"),
            "formato": (o.get("features") or {}).get("formato"), "tema": keywords(o.get("topic", "")),
            "idioma": (o.get("language") or "")[:2] or None}


def flatten(features: dict) -> list[str]:
    out = []
    for k, v in (features or {}).items():
        for x in (v if isinstance(v, list) else [v]):
            if x not in (None, ""):
                out.append(f"{k}={str(x).lower()}")
    return out


def weights(kind: str | None = None, now=None) -> dict[str, dict]:
    """peso ∈ [-1,1] por característica, com n_eff (avaliações efetivas, ponderadas pela recência)."""
    cfg = config.load()["recomendacao"]
    now = now or config.now()
    q = "SELECT * FROM decisions WHERE undone=0 AND is_test=0" + (" AND kind=?" if kind else "")
    acc: dict[str, list[float]] = {}
    for d in db.query(q, (kind,) if kind else ()):
        age = max(0.0, (now - config.parse(d["decided_at"])).total_seconds() / 86400)
        decay = 0.5 ** (age / cfg["meia_vida_dias"])
        for f in flatten(d["features"] if isinstance(d["features"], dict) else {}):
            a = acc.setdefault(f, [0.0, 0.0, 0.0, None])
            a[0] += decay * d["score"] / 100
            a[1] += decay
            a[2] += 1
            a[3] = max(a[3] or d["decided_at"], d["decided_at"])
    return {f: {"peso": s / (n + cfg["suavizacao"]), "n_eff": n, "n": cnt, "ultima": last}
            for f, (s, n, cnt, last) in acc.items()}


def affinity(features: dict, w: dict) -> dict:
    fl = flatten(features)
    if not fl:
        return {"afinidade": 0.0, "novidade": 1.0, "incerteza": 1.0, "ultima": None, "detalhe": []}
    known = [w[f] for f in fl if f in w]
    aff = sum(k["peso"] for k in known) / len(fl)
    novelty = sum(1 for f in fl if f not in w) / len(fl)  # nunca avaliado (decaimento não "zera" histórico)
    unc = sum(1 / math.sqrt(1 + (w[f]["n_eff"] if f in w else 0)) for f in fl) / len(fl)
    last = max((k["ultima"] for k in known if k["ultima"]), default=None)
    det = sorted(((f, round(w[f]["peso"], 2), w[f]["n"]) for f in fl if f in w), key=lambda x: -abs(x[1]))
    return {"afinidade": aff, "novidade": novelty, "incerteza": unc, "ultima": last, "detalhe": det[:3]}


def _fresh(o: dict, now) -> float:
    """1 = publicado hoje, decai em ~10 dias; datas aceitas: AAAAMMDD ou AAAA-MM-DD..."""
    p = (o.get("published_at") or "").replace("-", "")[:8]
    if not (len(p) == 8 and p.isdigit()):
        return 0.3
    try:
        dt = datetime(int(p[:4]), int(p[4:6]), int(p[6:8]), 12, tzinfo=now.tzinfo)
    except ValueError:  # ex.: timestamp epoch em vez de data
        return 0.3
    return math.exp(-max(0, (now - dt).days) / 10)


def rank(candidates: list[dict], limit: int, now=None) -> list[dict]:
    """Seleciona e ordena candidatos equilibrando preferência, exploração, reavaliação e diversidade."""
    cfg = config.load()["recomendacao"]
    now = now or config.now()
    w = weights("opportunity", now)
    scored = []
    for o in candidates:
        feats = opportunity_features(o)
        a = affinity(feats, w)
        days = (now - config.parse(a["ultima"])).days if a["ultima"] else 999
        if a["novidade"] >= 0.5:
            bucket = "exploracao"
            val = 0.5 + 0.5 * a["incerteza"]
        elif a["afinidade"] > 0.05:
            bucket = "preferencia"
            val = a["afinidade"]
        else:
            bucket = "reavaliacao"
            # rejeição forte reduz muito, mas nunca zera; o tempo desde a última avaliação reabre a chance
            strength = min(1.0, abs(a["afinidade"]) * 2)
            val = max(cfg["piso_segunda_chance"], 1 - 0.85 * strength) * min(1.0, days / 30)
        val += 0.25 * _fresh(o, now) + 0.15 * float(o.get("confidence") or 0.5)
        scored.append({**o, "_f": feats, "_a": a, "_bucket": bucket, "_val": val, "_days": days})

    unc = sum(s["_a"]["incerteza"] for s in scored) / len(scored) if scored else 1
    eps = min(cfg["exploracao_max"], max(cfg["exploracao_min"], cfg["exploracao_min"] + (cfg["exploracao_max"] - cfg["exploracao_min"]) * unc))
    quota = {"preferencia": round(limit * (1 - eps)), "exploracao": 0, "reavaliacao": 0}
    rest = limit - quota["preferencia"]
    quota["reavaliacao"] = rest // 3
    quota["exploracao"] = rest - quota["reavaliacao"]

    chosen: list[dict] = []

    def penalty(s):
        p = 0.0
        for c in chosen:
            if s["_f"]["nicho"] and s["_f"]["nicho"] == c["_f"]["nicho"]:
                p += 0.3
            if s["_f"]["canal"] and s["_f"]["canal"] == c["_f"]["canal"]:
                p += 0.4
            p += 0.15 * len(set(s["_f"]["tema"]) & set(c["_f"]["tema"]))
        return p

    pool = list(scored)
    for bucket in ("preferencia", "exploracao", "reavaliacao", None):  # None = completa com o melhor restante
        while pool and len(chosen) < limit:
            cands = [s for s in pool if bucket is None or s["_bucket"] == bucket]
            if not cands or (bucket and sum(c["_bucket"] == bucket for c in chosen) >= quota[bucket]):
                break
            best = max(cands, key=lambda s: s["_val"] - penalty(s))
            pool.remove(best)
            chosen.append(best)
    for s in chosen:
        s["rec_explain"] = explain(s, eps)
    return chosen


def explain(s: dict, eps: float) -> dict:
    a = s["_a"]
    det = ", ".join(f"{f} ({p:+.2f}, {n} aval.)" for f, p, n in a["detalhe"]) or "sem histórico"
    msg = {"preferencia": f"Compatível com preferências conhecidas: {det}.",
           "exploracao": f"Exploração: {a['novidade']:.0%} das características ainda não avaliadas.",
           "reavaliacao": f"Segunda chance: histórico neutro/negativo ({det}); última avaliação há {s['_days']} dias."}
    return {"estrategia": s["_bucket"], "mensagem": msg[s["_bucket"]], "valor": round(s["_val"], 3),
            "taxa_exploracao": round(eps, 2)}


def record_profile_changes(before: dict, after: dict) -> None:
    for f, new in after.items():
        old = before.get(f, {"peso": 0.0})["peso"]
        if abs(new["peso"] - old) >= 0.1 and new["n_eff"] >= 2:
            direction = "aumentou" if new["peso"] > old else "diminuiu"
            db.insert("profile_changes", {"ts": config.iso(), "feature": f, "old": round(old, 3),
                                          "new": round(new["peso"], 3), "n_eff": round(new["n_eff"], 2),
                                          "explanation": f"Prioridade de '{f}' {direction} após {new['n']} avaliações "
                                                         f"(peso {old:+.2f} → {new['peso']:+.2f})."})


def profile_summary(kind: str, top: int = 8) -> dict:
    w = weights(kind)
    ranked = sorted(w.items(), key=lambda kv: kv[1]["peso"])
    fmt = lambda xs: [{"caracteristica": f, "peso": round(v["peso"], 2), "avaliacoes": v["n"]} for f, v in xs]
    return {"positivas": fmt(reversed(ranked[-top:])), "negativas": fmt(ranked[:top]), "total": len(w)}


def profile_text_for_agents() -> str:
    lines = []
    for kind in ("opportunity", "cut"):
        p = profile_summary(kind, 6)
        pos = "; ".join(f"{x['caracteristica']} {x['peso']:+}" for x in p["positivas"] if x["peso"] > 0.05)
        neg = "; ".join(f"{x['caracteristica']} {x['peso']:+}" for x in p["negativas"] if x["peso"] < -0.05)
        lines.append(f"{'Assuntos' if kind == 'opportunity' else 'Cortes'} — favorece: {pos or 'sem dados'}; "
                     f"evita: {neg or 'sem dados'}")
    return "\n".join(lines) + "\n(Preferências orientam, não proíbem: mantenha diversidade.)"
