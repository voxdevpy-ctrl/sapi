"""Vídeos de Página: vídeos curtos no formato de post da SUA página (nome, @ e avatar configuráveis).

Fluxo:  PG_BUSCA (Agente 11, caçador) → aprovação → PG_DOWNLOAD → PG_MONTAGEM (Agente 12, piada + modelo)
        → PG_QC → aprovação final → Prontos.
Cada passo grava um log detalhado em LOGS/pagina/<lote>.jsonl (sucessos e falhas), além do log geral.
"""
from __future__ import annotations

import json
import re
import shutil
import textwrap
import time
from pathlib import Path

from . import config, cota, db, llm, log, media

GB, MB = 1 << 30, 1 << 20
AREA_TOP, AREA_H, W, H = 590, 1330, 1080, 1920
ASSETS = config.ROOT / "assets"
AUTO_TXT = "automática: lote de teste autorizado pelo usuário em 10/11/2026"

REPOST = ["original", "pouco", "muito", "incerto"]
HUNT_SCHEMA = {
    "type": "object", "required": ["escolhidos", "descartados", "observacoes"],
    "properties": {
        "observacoes": {"type": "string"},
        "escolhidos": {"type": "array", "items": {"type": "object",
            "required": ["url", "motivo", "nota_humor", "riscos", "piadas", "parece_ia", "repost", "sinais_ia_repost",
                         "brasileiro"],
            "properties": {"url": {"type": "string"}, "motivo": {"type": "string"},
                           "parece_ia": {"type": "boolean"}, "repost": {"type": "string", "enum": REPOST},
                           "brasileiro": {"type": "boolean"},
                           "sinais_ia_repost": {"type": "string"},
                           "nota_humor": {"type": "number", "minimum": 0, "maximum": 10},
                           "riscos": {"type": "array", "items": {"type": "string"}},
                           "piadas": {"type": "array", "items": {"type": "string"}}}}},
        "descartados": {"type": "array", "items": {"type": "object", "required": ["url", "motivo"],
            "properties": {"url": {"type": "string"}, "motivo": {"type": "string"}}}}},
}
MOUNT_SCHEMA = {
    "type": "object",
    "required": ["seguro", "motivo_inseguro", "o_que_acontece", "legenda_tela", "alternativas", "enquadramento",
                 "descricao_post", "hashtags", "justificativa", "parece_ia", "repost", "sinais_ia_repost"],
    "properties": {
        "seguro": {"type": "boolean"}, "motivo_inseguro": {"type": "string"},
        "parece_ia": {"type": "boolean"}, "repost": {"type": "string", "enum": REPOST},
        "sinais_ia_repost": {"type": "string"},
        "o_que_acontece": {"type": "string"},
        "enquadramento": {"type": "string", "enum": ["recorte", "encaixe"]},
        "legenda_tela": {"type": "string", "maxLength": 110},
        "alternativas": {"type": "array", "items": {"type": "string"}},
        "descricao_post": {"type": "string"}, "hashtags": {"type": "array", "items": {"type": "string"}},
        "justificativa": {"type": "string"}},
}


# ------------------------------------------------------------------ log detalhado do lote
def plog(lote: str, etapa: str, evento: str, item: str | None = None, **dados) -> None:
    rec = {"ts": config.iso(), "lote": lote, "item": item, "etapa": etapa, "evento": evento, **dados}
    f = config.DIRS["logs"] / "pagina" / f"{lote}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def cfg() -> dict:
    return config.load()["pagina"]


def work(item: str) -> Path:
    d = config.DIRS["intermediarios"] / "pagina" / item
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ pedido
def request(quantidade: int = 5, auto_aprovar: bool = False, foco: bool = False) -> str:
    from . import orchestrator
    lote = f"pg_lote_{config.iso()[:19].replace(':', '-')}"
    orchestrator.enqueue("PG_BUSCA", "pagina_lote", lote,
                         {"lote": lote, "quantidade": max(1, min(int(quantidade), 20)), "auto_aprovar": bool(auto_aprovar)})
    if foco:
        c = config.load()
        c["foco"] = "pagina"
        config.save(c)
    plog(lote, "PEDIDO", "lote pedido", quantidade=quantidade, auto_aprovar=auto_aprovar, foco=foco)
    return lote


# ------------------------------------------------------------------ regra IA / repost
def barrado_ia_repost(x: dict, views: int | None, c: dict | None = None, engaj: float | None = None,
                      topo: float | None = None) -> str | None:
    """IA ou muito repostado só passa se for MUITO viral E estiver no topo do engajamento do lote
    (`topo` = nota de corte dos X% melhores; None quando o lote não tem dados de engajamento)."""
    lim = int((c or cfg())["ia_repost_min_visualizacoes"])
    tipo = [n for n, sim in (("feito por IA", x.get("parece_ia")), ("muito repostado", x.get("repost") == "muito"),
                             ("canal com regra de IA", canal_restrito(x, c))) if sim]
    if not tipo:
        return None
    if (views or 0) < lim:
        return (f"{' e '.join(tipo)} com {views or 0:,} visualizações (só entra com {lim:,} ou mais)"
                .replace(",", "."))
    piso = float((c or cfg()).get("ia_repost_piso_por_mil") or 0)
    if engaj is None and (topo is not None or piso):
        return f"{' e '.join(tipo)} sem engajamento medido"
    if engaj is not None and engaj < piso:
        return f"{' e '.join(tipo)} com engajamento insuficiente: {engaj}/mil (piso {piso}/mil)"
    if topo is not None and engaj <= topo:
        return (f"{' e '.join(tipo)} com engajamento insuficiente: {engaj}/mil, fora do topo do lote "
                f"(precisa passar de {topo}/mil)")
    return None


def canal_restrito(x: dict, c: dict | None = None) -> bool:
    nomes = {n.casefold().strip() for n in (c or cfg()).get("canais_regra_ia") or []}
    return bool(x.get("canal")) and x["canal"].casefold().strip() in nomes


def topo_engajamento(cands: list[dict], pct: float) -> float | None:
    """Nota de corte para estar entre os `pct`% de maior engajamento dos candidatos."""
    vals = sorted(x["engajamento_por_mil"] for x in cands if x.get("engajamento_por_mil") is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(len(vals) * (1 - pct / 100)))]


def _norm_title(t: str | None) -> str:
    t = re.sub(r"#\S+", " ", (t or "").lower())
    return " ".join(re.sub(r"[^\w ]+", " ", t).split())


def _video_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def _miniaturas(cands: list[dict], d: Path, lote: str, n: int) -> int:
    """Baixa a miniatura pública dos n candidatos mais vistos para o caçador olhar (IA, marca d'água, repost)."""
    import urllib.request
    pasta = d / "miniaturas"
    pasta.mkdir(exist_ok=True)
    ok = 0
    for x in sorted(cands, key=lambda x: -(x.get("visualizacoes") or 0))[:n]:
        vid = _video_id(x["url"])
        f = pasta / f"{vid}.jpg"
        try:
            if not f.exists():
                with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", timeout=15) as r:
                    f.write_bytes(r.read(2 * MB))
            x["miniatura"] = str(f)
            ok += 1
        except Exception as e:  # noqa: BLE001
            plog(lote, "PG_BUSCA", "miniatura indisponível", url=x["url"], erro=str(e)[:200])
    plog(lote, "PG_BUSCA", "miniaturas baixadas", quantidade=ok)
    return ok


def engajamento(views, curtidas, comentarios) -> float | None:
    """Reação por mil visualizações; comentário pesa 5× (dá mais trabalho que curtir)."""
    if not views or (curtidas is None and comentarios is None):
        return None
    return round(((curtidas or 0) + 5 * (comentarios or 0)) * 1000 / views, 1)


# ------------------------------------------------------------------ brasileiro x gringo
_PT = {"não", "nao", "você", "voce", "vc", "pra", "pro", "mãe", "mae", "pai", "quando", "eu", "meu", "minha", "né",
       "tá", "ta", "isso", "esse", "essa", "gente", "mano", "cara", "vai", "foi", "tem", "olha", "muito", "porque",
       "rsrs", "vó", "vo", "tio", "tia", "irmão", "irmao", "escola", "chefe", "brasil", "brasileiro", "zueira",
       "resenha", "humorbrasileiro", "memesbrasileiros", "engraçado", "comédia", "comedia", "ficou", "aqui", "agora",
       "ele", "ela", "nós", "também", "tambem", "depois", "sempre", "nunca", "coisa", "fazer", "faz", "sabe",
       "está", "esta", "homem", "mulher", "dentro", "para", "de", "do", "da", "um", "uma", "com", "no", "na", "o",
       "que", "se", "mais", "ser", "teve", "malandro", "imitar", "garota", "garoto", "menino", "menina", "criança"}
_EN = {"the", "when", "my", "you", "your", "is", "and", "this", "with", "what", "funny", "of", "to", "it", "he",
       "she", "they", "i'm", "im", "dont", "don't", "lol", "bro", "his", "her", "was", "for", "how", "why", "dad",
       "mom", "prank", "wait", "end", "funniest", "comedy", "fail", "fails", "like", "subscribe", "shorts", "from",
       "really", "up", "everyone", "lucky", "eating", "food", "challenge", "random", "fooled", "at", "in", "on", "so",
       "got", "get", "are", "be", "can", "all", "just", "that", "out", "who", "try", "kid", "girl", "boy", "guy", "man",
       "woman", "baby", "cat", "dog", "save", "nurse", "teacher", "school", "never", "always", "every", "fyp", "pov",
       "omg", "wtf", "best", "ever", "life", "hack", "hacks", "trick", "she's", "he's", "it's", "gets", "makes"}
_ES = {"el", "los", "las", "cuando", "mi", "pero", "esto", "hermano", "jaja", "jajaja", "muy", "del", "y",
       "por", "qué", "risa", "mamá", "papá", "chiste", "gracioso"}


def origem(x: dict) -> str:
    """'br', 'gringo' ou 'incerto', pelo texto do título, descrição, tags e idioma informado pelo YouTube."""
    txt = " ".join(str(v) for v in (x.get("titulo"), x.get("canal"), x.get("descricao"), " ".join(x.get("tags") or [])) if v)
    low = txt.lower()
    words = re.findall(r"[\w'’]+", low)
    pt = sum(w in _PT for w in words) + 2 * len(re.findall(r"[ãõç]", low)) + 3 * bool(re.search(r"k{3,}", low))
    en = sum(w in _EN for w in words) - ("shorts" in words)
    es = sum(w in _ES for w in words) + 3 * bool(re.search(r"(ja){2,}", low)) + 2 * len(re.findall(r"[ñ¿¡]", low))
    lang = (x.get("idioma") or "").lower()
    if lang.startswith("pt"):
        pt += 4
    elif lang:
        en += 3
    fora = max(en, es)
    if pt >= 2 and pt > fora:
        return "br"
    if fora >= 2 and fora > pt:
        return "gringo"
    return "incerto"


def eh_gringo(x: dict) -> bool:
    """Gringo pelo texto; quando o texto não decide, vale o que o caçador disse (`brasileiro`)."""
    o = x.get("origem") or origem(x)
    return o == "gringo" or (o == "incerto" and x.get("brasileiro") is False)


def _mediana(vals: list[float]) -> float | None:
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else round((vals[m - 1] + vals[m]) / 2, 1)


def _detalhes(url: str) -> dict:
    import yt_dlp
    from . import acquire
    with yt_dlp.YoutubeDL(acquire._ydl_opts({"skip_download": True})) as y:
        i = y.extract_info(url, download=False)
    return {"titulo": i.get("title"), "canal": i.get("channel") or i.get("uploader"), "canal_id": i.get("channel_id"),
            "visualizacoes": i.get("view_count"), "curtidas": i.get("like_count"), "comentarios": i.get("comment_count"),
            "duracao_s": i.get("duration"), "postado_em": i.get("upload_date"),
            "descricao": (i.get("description") or "")[:300], "tags": (i.get("tags") or [])[:10],
            "idioma": i.get("language")}


def enrich(cands: list[dict], c: dict, lote: str, n: int | None = None) -> list[dict]:
    """Dados completos dos `n` primeiros (a lista já vem com os brasileiros na frente): comentários, curtidas, canal,
    duração, data e idioma. Filtra duração e mínimo de comentários; devolve os medidos (por engajamento) + o resto."""
    from concurrent.futures import ThreadPoolExecutor
    n = int(c.get("enriquecer_top") or 0) if n is None else n
    if not n:
        return cands
    t0 = time.time()
    top = cands[:n]
    resto = cands[n:]

    def one(x):
        try:
            return x, _detalhes(x["url"]), None
        except Exception as e:  # noqa: BLE001
            return x, None, str(e)[:200]
    out, falhas, fora = [], 0, []
    with ThreadPoolExecutor(4) as ex:
        for x, det, err in ex.map(one, top):
            if err:
                falhas += 1
                plog(lote, "PG_BUSCA", "detalhes indisponíveis", url=x["url"], erro=err)
                resto.append(x)
                continue
            x.update({k: v for k, v in det.items() if v is not None})
            x["medido"] = True
            d = x.get("duracao_s")
            if d is not None and not (c["duracao_min_s"] <= d <= c["duracao_max_s"]):
                fora.append({"url": x["url"], "motivo": f"duração {d:.0f}s"})
                continue
            if (x.get("comentarios") or 0) < int(c.get("min_comentarios") or 0):
                fora.append({"url": x["url"], "motivo": f"{x.get('comentarios') or 0} comentários"})
                continue
            x["engajamento_por_mil"] = engajamento(x.get("visualizacoes"), x.get("curtidas"), x.get("comentarios"))
            if x.get("visualizacoes") and x.get("comentarios") is not None:
                x["comentarios_por_mil"] = round(x["comentarios"] * 1000 / x["visualizacoes"], 2)
            x["origem"] = origem(x)
            out.append(x)
    out.sort(key=lambda x: -(x.get("engajamento_por_mil") or 0))
    plog(lote, "PG_BUSCA", "detalhes coletados", segundos=round(time.time() - t0, 1), com_dados=len(out),
         falhas=falhas, filtrados=fora,
         ranking=[{"url": x["url"], "views": x.get("visualizacoes"), "curtidas": x.get("curtidas"),
                   "comentarios": x.get("comentarios"), "engajamento_por_mil": x.get("engajamento_por_mil"),
                   "origem": x.get("origem"), "titulo": (x.get("titulo") or "")[:60]} for x in out])
    return out + resto


def ordem_para_medir(cands: list[dict]) -> list[dict]:
    """Brasileiros (pelo título) primeiro, depois os incertos, por último os gringos; cada grupo por visualizações."""
    rank = {"br": 0, "incerto": 1, "gringo": 2}
    for x in cands:
        x["origem"] = origem(x)
    return sorted(cands, key=lambda x: (rank[x["origem"]], -(x.get("visualizacoes") or 0)))


def preparar(cands: list[dict], c: dict, lote: str, precisa: int) -> tuple[list[dict], dict]:
    """Mede, calcula as notas do lote e filtra pelo que só depende de métrica:
    - piso geral de engajamento; se o lote vier fraco, mede mais uma leva antes de desistir;
    - gringo só com engajamento BEM acima do normal do lote (fator × mediana)."""
    cands = enrich(ordem_para_medir(cands), c, lote)
    medidos = lambda: [x for x in cands if x.get("engajamento_por_mil") is not None]  # noqa: E731
    piso = float(c.get("engajamento_min_por_mil") or 0)
    bons = lambda: [x for x in medidos() if x["engajamento_por_mil"] >= piso]  # noqa: E731
    extra = int(c.get("enriquecer_extra") or 0)
    nao_medidos = [x for x in cands if not x.get("medido")]
    if len(bons()) < 2 * precisa and extra and nao_medidos:
        plog(lote, "PG_BUSCA", "lote fraco: medindo mais candidatos", bons=len(bons()), precisa=precisa, extra=extra)
        cands = [x for x in cands if x.get("medido")] + enrich(nao_medidos, c, lote, extra)
    vals = [x["engajamento_por_mil"] for x in medidos()]
    med = _mediana(vals)
    topo = topo_engajamento(medidos(), float(c.get("ia_repost_topo_pct") or 25)) if len(vals) >= 4 else None
    lim_gringo = round(max(piso, (med or 0) * float(c.get("gringo_fator_mediana") or 2)), 1)
    notas = {"mediana_por_mil": med, "topo_por_mil": topo, "piso_por_mil": piso, "gringo_min_por_mil": lim_gringo,
             "medidos": len(vals)}
    if not vals:  # sem nenhuma métrica (YouTube não respondeu): segue com o que tem, avisando
        plog(lote, "PG_BUSCA", "sem métricas: seguindo só com visualizações", candidatos=len(cands))
        return cands, notas
    fora, ok = [], []
    for x in medidos():
        e = x["engajamento_por_mil"]
        if e < piso:
            fora.append({"url": x["url"], "motivo": f"engajamento {e}/mil abaixo do piso {piso}/mil"})
        elif x["origem"] == "gringo" and e < lim_gringo:
            fora.append({"url": x["url"], "motivo": f"gringo com {e}/mil (só entra com {lim_gringo}/mil, "
                                                    f"{c.get('gringo_fator_mediana')}× a mediana {med})"})
        else:
            ok.append(x)
    rank = {"br": 0, "incerto": 1, "gringo": 2}
    ok.sort(key=lambda x: (canal_restrito(x, c), rank[x["origem"]], -x["engajamento_por_mil"]))
    notas.update({"elegiveis": len(ok), "filtrados": len(fora),
                  "brasileiros": sum(x["origem"] == "br" for x in ok)})
    plog(lote, "PG_BUSCA", "notas do lote", **notas, fora=fora)
    if len(ok) < precisa:
        plog(lote, "PG_BUSCA", "lote fraco: menos vídeos bons que o pedido", elegiveis=len(ok), precisa=precisa)
    return ok, notas


_CAMPOS_CACADOR = ("url", "titulo", "canal", "origem", "visualizacoes", "curtidas", "comentarios", "engajamento_por_mil",
                   "comentarios_por_mil", "duracao_s", "postado_em", "mesmo_titulo_em_outros_videos", "miniatura")


# ------------------------------------------------------------------ PG_BUSCA (Agente 11)
def collect(c: dict, lote: str) -> list[dict]:
    from . import research
    seen = {r["source_url"] for r in db.query("SELECT source_url FROM page_videos")}
    out, vistos, titulos = {}, 0, {}
    for q in c["consultas"]:
        t0 = time.time()
        try:
            if q.startswith("#"):
                import urllib.parse
                hits = research._flat(f"https://www.youtube.com/hashtag/{urllib.parse.quote(q[1:])}/shorts", 40)
            else:
                hits = research.youtube_search(q, 20, cc_only=False)
        except Exception as e:  # noqa: BLE001
            plog(lote, "PG_BUSCA", "falha na consulta", consulta=q, erro=str(e)[:300])
            continue
        ok = 0
        for h in hits:
            vistos += 1
            nt = _norm_title(h.get("title"))
            if len(nt) >= 8:  # o mesmo título em vídeos diferentes = sinal de repost
                titulos.setdefault(nt, set()).add(h.get("id") or h.get("url"))
            url = h.get("url") or ""
            if "/shorts/" not in url and not h.get("id"):
                continue
            if "/shorts/" not in url:
                url = f"https://www.youtube.com/shorts/{h['id']}"
            d, v = h.get("duration"), h.get("view_count") or 0
            # a aba Shorts não informa a duração: é conferida no download
            if url in seen or url in out or (d is not None and not (c["duracao_min_s"] <= d <= c["duracao_max_s"])) \
                    or v < c["min_visualizacoes"]:
                continue
            out[url] = {"url": url, "titulo": h.get("title"), "canal": h.get("channel") or h.get("uploader"),
                        "visualizacoes": v, "duracao_s": d}
            ok += 1
        plog(lote, "PG_BUSCA", "consulta feita", consulta=q, resultados=len(hits), aproveitados=ok,
             segundos=round(time.time() - t0, 1))
    for x in out.values():
        x["mesmo_titulo_em_outros_videos"] = max(0, len(titulos.get(_norm_title(x["titulo"]), ())) - 1)
    plog(lote, "PG_BUSCA", "coleta concluída", candidatos=len(out), vistos=vistos)
    return list(out.values())


def h_busca(task) -> dict:
    p = task["payload"] or {}
    lote, n = p.get("lote") or task["item_id"], p.get("quantidade") or 5
    c = cfg()
    reservas = 3 if p.get("auto_aprovar") else 0  # lote automático: reservas cobrem vídeos descartados no caminho
    cands = collect(c, lote)
    if not cands:
        raise RuntimeError("nenhum vídeo curto de humor passou nos filtros (duração/visualizações/repetidos)")
    total = len(cands)
    cands, notas = preparar(cands, c, lote, n + reservas)
    if not cands:
        raise RuntimeError(f"nenhum candidato com engajamento suficiente (notas do lote: {notas})")
    topo = notas["topo_por_mil"]
    db.kv_set(f"pg_engaj_topo:{lote}", "" if topo is None else str(topo))
    _miniaturas(cands, work(lote), lote, len(cands))
    f = work(lote) / "candidatos.json"
    # uma linha por candidato e só os campos úteis: o caçador lê a lista inteira
    f.write_text("[\n" + ",\n".join(json.dumps({k: x.get(k) for k in _CAMPOS_CACADOR if x.get(k) is not None},
                                               ensure_ascii=False) for x in cands) + "\n]\n", "utf-8")
    piso_ia = float(c.get("ia_repost_piso_por_mil") or 0)
    prompt = (f"Lote {lote}. Candidatos reais em {f} ({len(cands)}, um por linha: leia o arquivo INTEIRO). "
              f"Escolha os {n + reservas} melhores (em ordem de preferência) para a página de humor "
              f"'{c['nome']}' ({c['arroba']}). Para cada um, proponha 3 piadas curtas para o topo do vídeo. "
              f"PRIORIZE VÍDEOS BRASILEIROS (campo origem='br'); os gringos da lista só estão aí porque têm engajamento "
              f"bem acima do normal do lote. Depois disso, priorize engajamento (curtidas + 5×comentários por mil "
              f"visualizações), não só visualizações. Notas do lote: {json.dumps(notas, ensure_ascii=False)}. "
              f"Canais com a mesma regra da IA: {', '.join(c.get('canais_regra_ia') or []) or 'nenhum'}. "
              f"Abra as miniaturas. Regra do dono da página: vídeo feito por IA, muito repostado ou desses canais só se tiver "
              f"{c['ia_repost_min_visualizacoes']:,} visualizações ou mais".replace(",", ".")
              + f" E engajamento_por_mil de pelo menos {piso_ia}"
              + (f" e acima de {topo} (topo do lote)" if topo is not None else "")
              + "; senão, descarte. Prefira originais.")
    t0 = time.time()
    res = llm.call_agent("11-cacador", prompt, HUNT_SCHEMA, task_id=task["id"], item_id=lote, tools=["Read"])
    by = {x["url"]: x for x in cands}
    validos = [x for x in res["escolhidos"] if x["url"] in by]
    barrados = []
    for x in list(validos):
        info = {**by[x["url"]], **x, "origem": by[x["url"]].get("origem")}
        motivo = barrado_ia_repost(info, info.get("visualizacoes"), c, info.get("engajamento_por_mil"), topo)
        if not motivo and info.get("engajamento_por_mil") is not None and eh_gringo(info) \
                and info["engajamento_por_mil"] < notas["gringo_min_por_mil"]:
            motivo = (f"gringo (o caçador confirmou) com {info['engajamento_por_mil']}/mil; só entra com "
                      f"{notas['gringo_min_por_mil']}/mil")
        if motivo:
            validos.remove(x)
            barrados.append({"url": x["url"], "motivo": "regra: " + motivo, "sinais": x.get("sinais_ia_repost")})
    # ordem final: originais brasileiros primeiro; gringos depois; IA/muito repostado (liberados) no fim
    validos.sort(key=lambda x: (bool(x.get("parece_ia") or x.get("repost") == "muito"
                                     or canal_restrito(by[x["url"]], c)),
                                eh_gringo({**by[x["url"]], **x, "origem": by[x["url"]].get("origem")})))
    chosen = validos[:n + reservas]
    inventado = [x["url"] for x in res["escolhidos"] if x["url"] not in by]
    if barrados:
        plog(lote, "PG_BUSCA", "barrados pelas regras IA/repost/gringo", barrados=barrados)
    res["descartados"] = res["descartados"] + barrados
    escolhidos = [{**by[x["url"]], **x, "origem": by[x["url"]].get("origem")} for x in chosen]
    plog(lote, "PG_BUSCA", "agente 11 respondeu", segundos=round(time.time() - t0, 1),
         escolhidos=escolhidos, descartados=res["descartados"],
         urls_fora_da_lista=inventado, observacoes=res["observacoes"])
    return {"lote": lote, "quantidade": n, "auto_aprovar": bool(p.get("auto_aprovar")), "candidatos": total,
            "elegiveis": len(cands), "escolhidos": escolhidos, "fora_da_lista": inventado,
            "barrados_ia_repost": barrados, "engaj_topo": topo, "notas": notas,
            "descartados": res["descartados"], "observacoes": res["observacoes"]}


# ------------------------------------------------------------------ PG_DOWNLOAD
def h_download(task) -> dict:
    import yt_dlp
    from . import acquire
    pv = db.get("page_videos", task["item_id"])
    d = work(pv["id"])
    cota.ensure(150 * MB, "o download do vídeo de página")
    t0 = time.time()
    opts = acquire._ydl_opts({"format": "bv*[height<=1920]+ba/b", "merge_output_format": "mp4",
                              "outtmpl": str(d / "fonte.%(ext)s"), "continuedl": True})
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(pv["source_url"], download=True)
    files = [p for p in d.glob("fonte.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if not files:
        raise RuntimeError("download não produziu arquivo")
    src = files[0]
    pr = media.probe(src)
    meta = {"titulo": info.get("title"), "canal": info.get("channel") or info.get("uploader"),
            "canal_url": info.get("channel_url") or info.get("uploader_url"),
            "descricao_fonte": (info.get("description") or "")[:1500], "tags": (info.get("tags") or [])[:15],
            "visualizacoes": info.get("view_count"), "curtidas": info.get("like_count"),
            "comentarios": info.get("comment_count"),
            "duracao": pr["duration"], "probe": pr, "arquivo": str(src), "bytes": src.stat().st_size,
            "sha256": media.sha256(src)}
    c = cfg()
    if not (c["duracao_min_s"] - 1 <= pr["duration"] <= c["duracao_max_s"] + 5):
        meta["rejeitado"] = f"duração {pr['duration']:.0f}s fora da faixa {c['duracao_min_s']}–{c['duracao_max_s']}s"
    plog(pv["lote"], "PG_DOWNLOAD", "rejeitado" if meta.get("rejeitado") else "baixado", pv["id"],
         segundos=round(time.time() - t0, 1), **{k: v for k, v in meta.items() if k != "descricao_fonte"})
    return meta


# ------------------------------------------------------------------ PG_MONTAGEM (Agente 12)
def frames(src: Path, dur: float, d: Path) -> list[str]:
    out = []
    for i, frac in enumerate((0.1, 0.35, 0.6, 0.85)):
        f = d / f"quadro_{i + 1}.jpg"
        media.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * frac:.2f}", "-i", str(src), "-frames:v", "1",
                   "-vf", "scale=480:-2", "-q:v", "4", str(f)], 120)
        if f.exists():
            out.append(str(f))
    return out


def wrap(text: str) -> tuple[int, list[str]]:
    """Maior fonte em que a piada cabe em até 2 linhas (ou 3 nas fontes menores)."""
    text = " ".join(text.split())
    for size, maxl in ((52, 2), (48, 2), (44, 3), (40, 3)):
        chars = int(972 / (size * 0.56))
        lines = textwrap.wrap(text, chars)
        if len(lines) <= maxl:
            return size, lines
    return 40, textwrap.wrap(text, int(972 / (40 * 0.56)))[:3]


def clean(text: str) -> str:
    # a fonte do modelo não tem emoji: remove símbolos fora do plano básico para não virar quadradinho
    return "".join(ch for ch in text if ord(ch) < 0x2000 or ch in "…“”‘’–—").strip()


def render(src: Path, caption: str, d: Path, out: Path, pref: str = "recorte") -> dict:
    c = cfg()
    pr = media.probe(src)
    vw, vh = pr["video"]["width"], pr["video"]["height"]
    s = max(W / vw, AREA_H / vh)
    lost = 1 - (W * AREA_H) / (vw * s * vh * s)
    # recorte preenche a área (como as páginas fazem); o montador pede "encaixe" se houver texto/ação nas bordas.
    # Acima do limite (ex.: vídeo deitado) recortar perderia demais: encaixa sempre.
    modo = "recorte" if pref == "recorte" and lost <= c["recorte_max"] else "encaixe_com_fundo_desfocado"
    if modo == "recorte":
        vchain = f"[0:v]scale={W}:{AREA_H}:force_original_aspect_ratio=increase,crop={W}:{AREA_H},setsar=1[vid]"
    else:
        vchain = (f"[0:v]split[a][b];[a]scale={W}:{AREA_H}:force_original_aspect_ratio=increase,crop={W}:{AREA_H},"
                  f"boxblur=20:2,eq=brightness=-0.12[bg];[b]scale={W}:{AREA_H}:force_original_aspect_ratio=decrease[fg];"
                  f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[vid]")
    for name, fname in (("bold.ttf", "arialbd.ttf"),):
        if not (d / name).exists():
            shutil.copy(Path(r"C:\Windows\Fonts") / fname, d / name)
    avatar = Path(c.get("avatar") or "").expanduser()
    shutil.copy(avatar if avatar.is_file() else ASSETS / "avatar-pagina.png", d / "avatar.png")
    (d / "nome.txt").write_text(c["nome"], "utf-8")
    (d / "arroba.txt").write_text(c["arroba"], "utf-8")
    size, lines = wrap(clean(caption))
    lh = int(size * 1.25)
    y0 = 395 if len(lines) < 3 else 380
    draws = [f"drawtext=fontfile=bold.ttf:textfile=nome.txt:fontsize=50:fontcolor=0x111111:x=268:y=190",
             f"drawtext=fontfile=bold.ttf:textfile=arroba.txt:fontsize=38:fontcolor=0x8a8d93:x=268:y=258"]
    for i, ln in enumerate(lines):
        (d / f"linha{i}.txt").write_text(ln, "utf-8")
        draws.append(f"drawtext=fontfile=bold.ttf:textfile=linha{i}.txt:fontsize={size}:fontcolor=0x111111:x=54:y={y0 + i * lh}")
    has_audio = bool(pr["audio"])
    graph = (vchain + ";[1:v]scale=190:190,format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
             "a='if(lte(hypot(X-95,Y-95),94),255,0)'[av];"
             f"color=white:s={W}x{H}:r=30[cv];[cv][vid]overlay=0:{AREA_TOP}:shortest=1[c1];"
             f"[c1][av]overlay=54:150,{','.join(draws)},format=yuv420p[vo]")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-loop", "1", "-i", "avatar.png",
            "-filter_complex", graph + (";[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[ao]" if has_audio else ""),
            "-map", "[vo]"] + (["-map", "[ao]"] if has_audio else []) + [
            "-t", f"{pr['duration']:.3f}", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            "-threads", str(config.load()["hardware"]["threads"]), str(out)]
    t0 = time.time()
    r = media.run(args, 1800, cwd=d)
    if r.returncode != 0:
        raise media.MediaError(f"montagem falhou ({r.returncode}): {r.stderr[-400:]}")
    return {"modo_video": modo, "perda_recorte": round(lost, 3), "fonte_px": size, "linhas": lines,
            "segundos_render": round(time.time() - t0, 1), "duracao_fonte": pr["duration"], "tem_audio": has_audio}


def h_montagem(task) -> dict:
    pv = db.get("page_videos", task["item_id"])
    m = pv["meta"]
    d = work(pv["id"])
    src = Path(m["arquivo"])
    imgs = frames(src, m["duracao"], d)
    plog(pv["lote"], "PG_MONTAGEM", "quadros extraídos", pv["id"], quadros=imgs)
    ctx = d / "contexto.json"
    ctx.write_text(json.dumps({"pagina": cfg()["nome"], "arroba": cfg()["arroba"], "titulo_fonte": m["titulo"],
                               "canal_fonte": m["canal"], "link_fonte": pv["source_url"],
                               "descricao_fonte": m.get("descricao_fonte"), "tags_fonte": m.get("tags"),
                               "duracao_s": m["duracao"], "piadas_sugeridas_pelo_cacador": pv["jokes"],
                               "quadros": imgs, "exemplo_de_tom": cfg()["exemplos_tom"]},
                              ensure_ascii=False, indent=1), "utf-8")
    t0 = time.time()
    res = llm.call_agent("12-montador", f"Monte o vídeo de página. Contexto (leia também os quadros): {ctx}",
                         MOUNT_SCHEMA, task_id=task["id"], item_id=pv["id"], tools=["Read"])
    plog(pv["lote"], "PG_MONTAGEM", "agente 12 respondeu", pv["id"], segundos=round(time.time() - t0, 1), resposta=res)
    out = {"llm": res, "quadros": imgs}
    t = db.kv_get(f"pg_engaj_topo:{pv['lote']}")
    motivo = res["seguro"] and barrado_ia_repost({**res, "canal": m.get("canal") or pv.get("channel")},
                                                 m.get("visualizacoes") or pv.get("views"), None,
                                                 m.get("engajamento_por_mil"), float(t) if t else None)
    if motivo:
        res["seguro"], res["motivo_inseguro"] = False, "regra IA/repost: " + motivo
        plog(pv["lote"], "PG_MONTAGEM", "barrado pela regra IA/repost", pv["id"], motivo=motivo,
             sinais=res.get("sinais_ia_repost"))
    if not res["seguro"]:
        plog(pv["lote"], "PG_MONTAGEM", "vídeo recusado pelo montador", pv["id"], motivo=res["motivo_inseguro"])
        return out
    day = config.iso()[:10]
    final_dir = config.DIRS["produzidos"] / "pagina" / day / pv["id"]
    final_dir.mkdir(parents=True, exist_ok=True)
    cota.ensure(60 * MB, "a montagem do vídeo de página")
    video = final_dir / "video.mp4"
    info = render(src, res["legenda_tela"], d, video, res.get("enquadramento") or "recorte")
    tags = " ".join(h if h.startswith("#") else "#" + h for h in res["hashtags"])
    creditos = f"🎥 Créditos: {m['canal']} ({pv['source_url']})"
    desc = f"{res['descricao_post'].strip()}\n\n{creditos}\n\n{tags}".strip()
    (final_dir / "legenda_tela.txt").write_text(clean(res["legenda_tela"]), "utf-8")
    (final_dir / "descricao.txt").write_text(desc, "utf-8")
    (final_dir / "metadata.json").write_text(json.dumps({"pagina": pv["id"], "fonte": pv["source_url"], "canal": m["canal"],
                                                         "render": info, "agente12": res}, ensure_ascii=False, indent=1), "utf-8")
    plog(pv["lote"], "PG_MONTAGEM", "montado", pv["id"], video=str(video), **info)
    return {**out, "video": str(video), "pasta": str(final_dir), "descricao": desc, "creditos": creditos, "render": info}


# ------------------------------------------------------------------ PG_QC
def h_qc(task) -> dict:
    pv = db.get("page_videos", task["item_id"])
    v = Path(pv["final_path"])
    p = media.probe(v)
    vol = media.volume_stats(v)
    ok, err = media.decode_check(v, 600)
    capa = v.with_name("capa.jpg")
    media.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{min(1.5, p['duration'] / 2):.2f}", "-i", str(v),
               "-frames:v", "1", "-q:v", "3", str(capa)], 120)
    exp_d = pv["meta"]["duracao"]
    checks = {
        "resolucao_1080x1920": (p["video"]["width"], p["video"]["height"]) == (W, H),
        "duracao_igual_a_fonte": abs(p["duration"] - exp_d) <= 0.5,
        "audio_presente": bool(p["audio"]),
        "audio_nao_mudo": vol["max_db"] is not None and vol["max_db"] > -40,
        "decodifica_inteiro": ok,
        "tamanho_razoavel": 0 < v.stat().st_size < 150 * MB,
        "capa_gerada": capa.exists(),
    }
    res = {"testes": checks, "falhas": [k for k, x in checks.items() if not x], "duracao": p["duration"],
           "duracao_fonte": exp_d, "volume": vol, "bytes": v.stat().st_size, "erro_decodificacao": err if not ok else "",
           "capa": str(capa)}
    plog(pv["lote"], "PG_QC", "qc concluído", pv["id"], **res)
    return res


# ------------------------------------------------------------------ efeitos aprovados pelo supervisor
def on_success(task, out) -> None:
    from . import orchestrator
    st = task["stage"]
    if st == "PG_BUSCA":
        lote = out["lote"]
        known = {r["source_url"] for r in db.query("SELECT source_url FROM page_videos")}
        db.kv_set(f"pg_qtd:{lote}", str(out["quantidade"]))
        for k, x in enumerate(out["escolhidos"]):
            if x["url"] in known:
                continue
            pid = "pg_" + db.new_id("v")[-10:]
            auto = out["auto_aprovar"]
            if auto and k >= out["quantidade"]:  # reserva: só entra se algum dos primeiros for descartado
                db.insert("page_videos", {"id": pid, "lote": lote, "source_url": x["url"], "title": x.get("titulo"),
                                          "channel": x.get("canal"), "views": x.get("visualizacoes"),
                                          "duration": x.get("duracao_s"), "jokes": x["piadas"], "auto": 1,
                                          "meta": {"motivo": x["motivo"], "nota_humor": x["nota_humor"], "riscos": x["riscos"],
                                                   "parece_ia": x.get("parece_ia"), "repost": x.get("repost"),
                                                   "sinais_ia_repost": x.get("sinais_ia_repost"),
                                                   "curtidas": x.get("curtidas"), "comentarios": x.get("comentarios"),
                                                   "engajamento_por_mil": x.get("engajamento_por_mil"),
                                                   "postado_em": x.get("postado_em"), "origem": x.get("origem"),
                                                   "brasileiro": x.get("brasileiro")},
                                          "state": "RESERVA", "created_at": config.iso()})
                plog(lote, "APROVACAO_1", "guardado como reserva", pid, url=x["url"])
                continue
            db.insert("page_videos", {"id": pid, "lote": lote, "source_url": x["url"], "title": x.get("titulo"),
                                      "channel": x.get("canal"), "views": x.get("visualizacoes"),
                                      "duration": x.get("duracao_s"), "jokes": x["piadas"],
                                      "meta": {"motivo": x["motivo"], "nota_humor": x["nota_humor"], "riscos": x["riscos"],
                                                   "parece_ia": x.get("parece_ia"), "repost": x.get("repost"),
                                                   "sinais_ia_repost": x.get("sinais_ia_repost"),
                                                   "curtidas": x.get("curtidas"), "comentarios": x.get("comentarios"),
                                                   "engajamento_por_mil": x.get("engajamento_por_mil"),
                                                   "postado_em": x.get("postado_em"), "origem": x.get("origem"),
                                                   "brasileiro": x.get("brasileiro")},
                                      "state": "APROVADO" if auto else "AGUARDANDO_APROVACAO",
                                      "approval1": AUTO_TXT if auto else None, "auto": int(auto),
                                      "created_at": config.iso()})
            plog(lote, "APROVACAO_1", "aprovado automaticamente" if auto else "aguardando sua aprovação", pid,
                 url=x["url"])
            if auto:
                orchestrator.enqueue("PG_DOWNLOAD", "pagina", pid)
    elif st == "PG_DOWNLOAD":
        pv = db.get("page_videos", task["item_id"])
        if out.get("rejeitado"):
            db.update("page_videos", pv["id"], {"meta": {**(pv["meta"] or {}), **out}, "state": "DESCARTADO",
                                                "duration": out["duracao"]})
            return
        db.update("page_videos", pv["id"], {"meta": {**(pv["meta"] or {}), **out}, "state": "BAIXADO",
                                            "duration": out["duracao"]})
        orchestrator.enqueue("PG_MONTAGEM", "pagina", pv["id"])
    elif st == "PG_MONTAGEM":
        pv = db.get("page_videos", task["item_id"])
        if not out["llm"]["seguro"]:
            db.update("page_videos", pv["id"], {"state": "DESCARTADO", "caption": None,
                                                "meta": {**pv["meta"], "recusa": out["llm"]["motivo_inseguro"]}})
            return
        db.update("page_videos", pv["id"], {"state": "MONTADO", "caption": clean(out["llm"]["legenda_tela"]),
                                            "final_path": out["video"], "folder": out["pasta"],
                                            "description": out["descricao"], "hashtags": out["llm"]["hashtags"],
                                            "meta": {**pv["meta"], "montagem": out["render"],
                                                     "alternativas": out["llm"]["alternativas"],
                                                     "o_que_acontece": out["llm"]["o_que_acontece"],
                                                     "montador_ia": out["llm"].get("parece_ia"),
                                                     "montador_repost": out["llm"].get("repost"),
                                                     "montador_sinais": out["llm"].get("sinais_ia_repost")}})
        orchestrator.enqueue("PG_QC", "pagina", pv["id"])
    elif st == "PG_QC":
        pv = db.get("page_videos", task["item_id"])
        db.update("page_videos", pv["id"], {"cover": out["capa"]})
        if pv["auto"]:
            approve_final(pv["id"], AUTO_TXT)
        else:
            db.update("page_videos", pv["id"], {"state": "AGUARDANDO_APROVACAO_FINAL"})
            plog(pv["lote"], "APROVACAO_2", "aguardando sua aprovação final", pv["id"])


def approve_final(pid: str, how: str) -> str:
    pv = db.get("page_videos", pid)
    eid = "exp_" + pid
    if not db.get("exports", eid):
        db.insert("exports", {"id": eid, "cut_id": pid, "video_path": pv["final_path"], "folder": pv["folder"],
                              "produced_at": config.iso(), "exported_at": config.iso(), "scheduled_for": config.iso(),
                              "qc_status": "APROVADO", "title": pv["caption"], "description": pv["description"],
                              "hashtags": pv["hashtags"], "platform": "manual", "state": "PRONTO_PARA_PUBLICAR"})
    db.update("page_videos", pid, {"state": "PRONTO", "approval2": how})
    plog(pv["lote"], "APROVACAO_2", "pronto para postar", pid, aprovacao=how, export=eid)
    return eid


def decide(pid: str, score: int) -> dict:
    """Seus 5 botões na seção Vídeos de Página: positivo aprova, zero ou negativo descarta."""
    from . import orchestrator
    pv = db.get("page_videos", pid)
    if not pv:
        raise ValueError("vídeo de página não encontrado")
    how = f"usuário: {score:+d} em {config.iso()}"
    if pv["state"] == "AGUARDANDO_APROVACAO":
        db.update("page_videos", pid, {"state": "APROVADO" if score > 0 else "DESCARTADO", "approval1": how})
        if score > 0:
            orchestrator.enqueue("PG_DOWNLOAD", "pagina", pid)
    elif pv["state"] == "AGUARDANDO_APROVACAO_FINAL":
        if score > 0:
            approve_final(pid, how)
        else:
            db.update("page_videos", pid, {"state": "DESCARTADO", "approval2": how})
    else:
        raise ValueError(f"nada para aprovar neste estado ({pv['state']})")
    log.event(item_id=pid, agent="usuario", requested="avaliar vídeo de página", result=how)
    plog(pv["lote"], "DECISAO", how, pid)
    return {"ok": True}


def _promote_reserves() -> bool:
    """Lote automático que perdeu vídeos no caminho: reservas entram no lugar até completar a quantidade."""
    from . import orchestrator
    moved = False
    for r in db.query("SELECT DISTINCT lote FROM page_videos WHERE state='RESERVA'"):
        lote = r["lote"]
        qtd = int(db.kv_get(f"pg_qtd:{lote}") or 0)
        ativos = db.query("SELECT COUNT(*) n FROM page_videos WHERE lote=? AND state NOT IN ('RESERVA','DESCARTADO','BLOQUEADO')", (lote,))[0]["n"]
        for pv in db.query("SELECT id FROM page_videos WHERE lote=? AND state='RESERVA' ORDER BY rowid LIMIT ?",
                           (lote, max(0, qtd - ativos))):
            db.update("page_videos", pv["id"], {"state": "APROVADO", "approval1": AUTO_TXT + " (reserva)"})
            orchestrator.enqueue("PG_DOWNLOAD", "pagina", pv["id"])
            plog(lote, "APROVACAO_1", "reserva promovida no lugar de um descartado", pv["id"])
            moved = True
    return moved


def maybe_release_focus() -> None:
    """Fim do lote em foco: devolve a Central para a produção normal e registra o resumo."""
    if _promote_reserves():
        return
    c = config.load()
    if c.get("foco") != "pagina":
        return
    open_ = db.query("SELECT COUNT(*) n FROM tasks WHERE stage LIKE 'PG_%' "
                     "AND state NOT IN ('CONCLUIDO','BLOQUEADO','CANCELADO','DESCARTADO')")[0]["n"]
    if open_:
        return
    c["foco"] = ""
    config.save(c)
    rows = db.query("SELECT lote, state, COUNT(*) n FROM page_videos GROUP BY lote, state")
    for lote in {r["lote"] for r in rows}:
        plog(lote, "FIM", "lote encerrado; produção normal retomada",
             estados={r["state"]: r["n"] for r in rows if r["lote"] == lote})
    log.event(agent="gerente-geral", requested="foco em Vídeos de Página", result="lote encerrado; produção normal retomada")


def listing() -> list[dict]:
    rows = db.query("SELECT * FROM page_videos ORDER BY created_at DESC LIMIT 60")
    for r in rows:
        e = db.get("exports", "exp_" + r["id"]) or {}
        r["export_id"] = e.get("id")
        r["video_url"] = f"/media/final/{e['id']}" if e else (f"/media/pagina/{r['id']}" if r.get("final_path") else None)
        r["baixado_em"], r["vezes_baixado"] = e.get("downloaded_at"), e.get("download_count") or 0
        r["video_excluido_em"] = e.get("video_deleted_at")
        from . import pos_postagem
        r["marcacoes"] = pos_postagem.marks(e["id"]) if e else {}
        task = db.query("SELECT stage, state, error FROM tasks WHERE item_id=? ORDER BY updated_at DESC LIMIT 1", (r["id"],))
        r["tarefa"] = task[0] if task else None
    return rows


def _supervise(a, task, out, error, stage_desc):
    """Comum aos supervisores 11/12: registra no log do lote tudo o que foi auditado."""
    pv = db.get("page_videos", task["item_id"]) if task["item_type"] == "pagina" else None
    lote = (pv or {}).get("lote") or (task.get("payload") or {}).get("lote") or task["item_id"]
    rep = a.report(stage_desc)
    plog(lote, task["stage"], f"supervisor: {rep['status']}", task["item_id"], erro=error,
         evidencias=rep["evidencias"], problemas=rep["problemas"], ressalvas=rep["ressalvas"], correcoes=rep["correcoes"])
    return rep


def re_url(s: str) -> bool:
    return bool(re.match(r"https://www\.youtube\.com/shorts/[\w-]{6,}$", s or ""))
