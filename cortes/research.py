"""Agente 01 (parte determinística): coleta de candidatos verificáveis — nada é inventado aqui.

Fontes gratuitas, sem chave: RSS do Google Trends, buscas do YouTube via yt-dlp (com filtro
Creative Commons) e últimos envios de canais autorizados configurados. O agente de IA apenas
analisa, compara e resume esses candidatos; ele não pode introduzir links fora desta lista.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import config, log

AGENT = "01-pesquisador"
CC_FILTER = "EgIwAQ%253D%253D"  # filtro "Creative Commons" da busca do YouTube

OPPORTUNITY_SCHEMA = {
    "type": "object", "required": ["oportunidades", "descartados"],
    "properties": {
        "oportunidades": {"type": "array", "items": {"type": "object",
            "required": ["candidato_id", "assunto", "resumo", "motivo", "nicho", "formato", "evidencias",
                         "potencial", "observacoes_uso", "confianca", "idioma"],
            "properties": {"candidato_id": {"type": "string"}, "assunto": {"type": "string"},
                           "resumo": {"type": "string"}, "motivo": {"type": "string"},
                           "nicho": {"type": "string"}, "formato": {"type": "string",
                                                                    "description": "podcast|entrevista|debate|palestra|aula|outro"},
                           "evidencias": {"type": "array", "items": {"type": "string"}},
                           "potencial": {"type": "string", "description": "alto|medio|baixo + justificativa curta"},
                           "observacoes_uso": {"type": "string"},
                           "confianca": {"type": "number", "minimum": 0, "maximum": 1},
                           "idioma": {"type": "string", "description": "código ISO 639-1 do idioma FALADO no vídeo "
                                      "(pt, en, es...), estimado por título, descrição e canal; 'desconhecido' se não der"}}}},
        "descartados": {"type": "array", "items": {"type": "object", "required": ["candidato_id", "motivo"],
            "properties": {"candidato_id": {"type": "string"}, "motivo": {"type": "string"}}}}},
}


def google_trends(geo: str) -> list[dict]:
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    req = urllib.request.Request(url, headers={"User-Agent": "cortes-local/0.1"})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=30).read())
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for it in root.iter("item"):
        out.append({"termo": it.findtext("title"), "trafego": it.findtext("ht:approx_traffic", namespaces=ns),
                    "data": it.findtext("pubDate"),
                    "noticias": [n.findtext("ht:news_item_title", namespaces=ns) for n in it.findall("ht:news_item", ns)][:2]})
    return out


def _flat(url: str, limit: int) -> list[dict]:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit,
            "js_runtimes": {"node": {}}, "socket_timeout": 30}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    return [e for e in (info.get("entries") or []) if e]


def youtube_search(query: str, limit: int = 8, cc_only: bool = True) -> list[dict]:
    q = urllib.parse.quote_plus(query)
    url = f"https://www.youtube.com/results?search_query={q}&gl=BR&hl=pt-BR" + (f"&sp={CC_FILTER}" if cc_only else "")
    return _flat(url, limit)


def pedido_terms(pedido: dict) -> list[str]:
    """Termos de um pedido feito na Central: cada nome/termo separado por vírgula vira uma busca."""
    tema = (pedido.get("tema") or "").replace(";", ",").replace("\n", ",")
    termos = [t.strip() for t in tema.split(",") if t.strip()]
    cat = (pedido.get("categoria") or "").strip()
    return termos or ([cat] if cat else [])


def _channel_entries(name: str, limit: int) -> tuple[str | None, list[dict]]:
    """Acha o canal pelo nome (resultado de busca cujo canal contém o nome) e lista lives e vídeos dele."""
    key = name.lower().replace(" ", "")
    hits = youtube_search(name, 8, cc_only=False)
    ch = next((h.get("channel_id") for h in hits
               if key in (h.get("channel") or h.get("uploader") or "").lower().replace(" ", "")), None)
    if not ch:
        return None, []
    out = []
    for tab in ("streams", "videos"):
        try:
            out += _flat(f"https://www.youtube.com/channel/{ch}/{tab}", limit)
        except Exception:  # noqa: BLE001 — canal sem aba de lives
            pass
    return ch, out


def collect(cfg: dict | None = None, pedido: dict | None = None) -> dict:
    cfg = cfg or config.load()
    p = cfg["pesquisa"]
    cands, errors, trends = {}, [], []
    pedido = pedido or {}
    min_views = int(pedido.get("min_visualizacoes") or p.get("min_visualizacoes") or 0)
    max_dur = p.get("duracao_max_fonte_s") or 0
    if p.get("tendencias", True) and not pedido:
        try:
            trends = google_trends(p["geo"])[:15]
        except Exception as e:  # noqa: BLE001
            errors.append(f"google trends: {e}")

    def add(entries, origem):
        for e in entries:
            url = e.get("url") or e.get("webpage_url")
            if not url or (e.get("duration") or 0) < p.get("duracao_min_fonte_s", 300):  # curto rende poucos cortes
                continue
            if max_dur and (e.get("duration") or 0) > max_dur:  # live gigante: download/transcrição pesados demais
                continue
            if min_views and (e.get("view_count") or 0) < min_views:
                continue
            cid = f"c{len(cands) + 1}"
            cands.setdefault(url, {"candidato_id": cid, "url": url, "titulo": e.get("title"),
                                   "canal": e.get("channel") or e.get("uploader"), "canal_id": e.get("channel_id"),
                                   "duracao_s": e.get("duration"), "visualizacoes": e.get("view_count"),
                                   "publicado": e.get("upload_date") or e.get("timestamp"), "origem": origem})

    a = cfg["autorizacao"]
    # com "qualquer canal" autorizado pelo usuário, a busca dispensa o filtro Creative Commons
    cc = a["aceitar_creative_commons"] and not a.get("autorizar_qualquer_canal")
    if pedido:
        cat = (pedido.get("categoria") or "").strip()
        terms = pedido_terms(pedido)
        queries = [f"{t} {cat}".strip() if cat and cat.lower() not in t.lower() else t for t in terms]
        if not cc:  # canal do próprio criador (lives e vídeos), além da busca
            for t in terms:
                try:
                    ch, entries = _channel_entries(t, 30)
                    add(entries, f"canal:{t}" + (f" ({ch})" if ch else " (canal não encontrado)"))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"canal de {t!r}: {str(e)[:120]}")
    else:
        queries = list(p["consultas"]) + [f"{t['termo']} podcast" for t in trends[:5] if t.get("termo")] \
            + [f"{t['termo']} entrevista" for t in trends[:5] if t.get("termo")]
    for q in queries:
        try:
            add(youtube_search(q, 8, cc_only=cc), f"busca:{q}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"busca {q!r}: {str(e)[:120]}")
    authorized = list(cfg["autorizacao"]["canais_autorizados"]) + list(cfg["autorizacao"]["canais_proprios"])
    for ch in ([] if pedido else authorized + list(p["canais"])):
        try:
            add(_flat(f"https://www.youtube.com/channel/{ch}/videos", 6), f"canal:{ch}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"canal {ch}: {str(e)[:120]}")
    for f in ([] if pedido else config.DIRS["entrada"].glob("*")):
        if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm", ".m4v"):
            from . import media
            try:
                dur = media.probe(f)["duration"]
            except media.MediaError as e:  # arquivo ainda sendo copiado: tenta na próxima pesquisa
                errors.append(f"entrada local {f.name}: {e}")
                continue
            add([{"url": f"local:{f}", "title": f.stem, "channel": "local", "channel_id": "local",
                  "duration": dur}], "entrada_local")
    out = {"coletado_em": config.iso(), "pedido": pedido or None, "tendencias": trends, "candidatos": list(cands.values()), "erros": errors}
    log.event(agent=AGENT, requested="coletar candidatos", result=f"{len(cands)} candidatos, {len(errors)} erros")
    return out


def prompt(collect_file: str, niches: list[str], profile: str, limit: int, pedido: dict | None = None) -> str:
    extra = ""
    if pedido:
        extra = (f"PEDIDO DO USUÁRIO (prioridade máxima): tema {pedido.get('tema') or '-'}; "
                 f"categoria {pedido.get('categoria') or '-'}; mínimo de visualizações {pedido.get('min_visualizacoes') or 0}. "
                 "Proponha só candidatos que atendam ao pedido e varie criadores e vídeos.\n")
    return (extra +f"Leia o arquivo {collect_file} (candidatos coletados de fontes reais e tendências do Google Trends). "
            f"Selecione até {limit} oportunidades com melhor potencial de gerar cortes. "
            f"Nichos configurados (podem estar vazios): {niches or 'livre'}.\n"
            f"Perfil editorial aprendido:\n{profile}\n"
            "PRIORIDADE: conteúdo falado em português do Brasil, para o público brasileiro. Vídeo estrangeiro só se for "
            "MUITO viral e relevante para brasileiros (diga isso no motivo). Informe o idioma FALADO em 'idioma'.\n"
            "Use apenas candidato_id existentes no arquivo. Não invente tendências, números ou datas; "
            "evidências devem citar dados do arquivo (visualizações, data, termo em alta) ou de buscas que você fizer.")
