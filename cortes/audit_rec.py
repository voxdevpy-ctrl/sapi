"""Auditoria periódica do recomendador (seção 10.2): detecta bolha e concentração."""
from __future__ import annotations

from collections import Counter

from . import db, recommend


def _hhi(values) -> float:
    c = Counter(v for v in values if v)
    n = sum(c.values())
    return round(sum((x / n) ** 2 for x in c.values()), 3) if n else 0.0


def run(last: int = 60) -> dict:
    rows = db.query("SELECT channel, niche, rec_explain FROM opportunities WHERE rec_explain IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT ?", (last,))
    strat = Counter((r["rec_explain"] or {}).get("estrategia") for r in rows)
    w = recommend.weights("opportunity")
    single = [f for f, v in w.items() if v["n"] == 1 and abs(v["peso"]) > 0.3]
    res = {"amostra": len(rows), "concentracao_canal_hhi": _hhi(r["channel"] for r in rows),
           "concentracao_nicho_hhi": _hhi(r["niche"] for r in rows), "estrategias": dict(strat),
           "caracteristicas_com_peso_excessivo_de_1_avaliacao": single}
    alerts = []
    if res["concentracao_canal_hhi"] > 0.35:
        alerts.append("fontes concentradas em poucos canais")
    if res["concentracao_nicho_hhi"] > 0.5:
        alerts.append("nichos concentrados (possível bolha)")
    if rows and strat.get("exploracao", 0) / len(rows) < 0.1:
        alerts.append("pouca exploração de conteúdos novos")
    if rows and len(rows) >= 20 and not strat.get("reavaliacao"):
        alerts.append("nenhuma segunda chance oferecida")
    if single:
        alerts.append("avaliações isoladas com influência excessiva")
    res["alertas"] = alerts or ["nenhum comportamento de bolha detectado"]
    return res
