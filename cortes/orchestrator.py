"""Gerente-geral (lógica determinística) — executa uma tarefa por vez, convoca o supervisor
correspondente, decide avançar/repetir/bloquear e persiste tudo para retomada após falhas."""
from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from . import (acquire, config, cota, cuts, pagina, db, llm, log, media, publish, qc, recommend, render, research,
               storage, subtitles, supervisors, transcribe)

ORDER = ["PG_QC", "PG_MONTAGEM", "PG_DOWNLOAD", "PG_BUSCA", "PUBLICACAO", "ORGANIZACAO", "TEXTOS", "QC", "FINALIZACAO", "LEGENDAS", "PREVIA", "SELECAO",
         "ANALISE", "TRANSCRICAO", "COMPRESSAO", "AQUISICAO", "VERIFICACAO", "PESQUISA"]
NEXT = {"COMPRESSAO": "TRANSCRICAO", "TRANSCRICAO": "ANALISE", "ANALISE": "SELECAO",
        "SELECAO": "PREVIA", "LEGENDAS": "FINALIZACAO", "FINALIZACAO": "QC", "QC": "TEXTOS",
        "TEXTOS": "ORGANIZACAO", "ORGANIZACAO": "PUBLICACAO"}
CUT_STAGES = {"LEGENDAS", "FINALIZACAO", "QC", "TEXTOS", "ORGANIZACAO", "PUBLICACAO"}
MAX_INTERRUPTIONS = 8
VIDEO_STAGES = {"COMPRESSAO", "TRANSCRICAO", "ANALISE", "SELECAO", "PREVIA"}

ANALYSIS_SCHEMA = {
    "type": "object", "required": ["participantes", "assuntos", "resumo", "momentos", "observacoes_audio_video"],
    "properties": {
        "participantes": {"type": "array", "items": {"type": "object",
            "required": ["rotulo", "nome", "papel", "evidencia_ids", "fonte_evidencia", "tentativas", "mesmo_que"],
            "properties": {"rotulo": {"type": "string"}, "nome": {"type": ["string", "null"]},
                           "papel": {"type": "string"}, "evidencia_ids": {"type": "array", "items": {"type": "integer"}},
                           "fonte_evidencia": {"type": "string"}, "tentativas": {"type": "string"},
                           "mesmo_que": {"type": ["string", "null"],
                                         "description": "rótulo principal, se a separação de vozes dividiu esta pessoa"}}}},
        "assuntos": {"type": "array", "items": {"type": "object", "required": ["titulo", "inicio_id", "fim_id"],
            "properties": {"titulo": {"type": "string"}, "inicio_id": {"type": "integer"}, "fim_id": {"type": "integer"}}}},
        "resumo": {"type": "string"},
        "momentos": {"type": "array", "items": {"type": "object",
            "required": ["inicio_id", "fim_id", "tipo", "descricao", "depende_de_contexto"],
            "properties": {"inicio_id": {"type": "integer"}, "fim_id": {"type": "integer"}, "tipo": {"type": "string"},
                           "descricao": {"type": "string"}, "depende_de_contexto": {"type": "string"}}}},
        "observacoes_audio_video": {"type": "string"}},
}
REVIEW_SCHEMA = {"type": "object", "required": ["avaliacoes"], "properties": {"avaliacoes": {"type": "array", "items": {
    "type": "object", "required": ["corte", "veredito", "motivo"],
    "properties": {"corte": {"type": "string"}, "veredito": {"type": "string", "enum": ["APROVADO", "APROVADO_COM_RESSALVAS", "REPROVADO"]},
                   "motivo": {"type": "string"}}}}}}


def enqueue(stage: str, item_type: str, item_id: str, payload: dict | None = None, not_before: str | None = None) -> None:
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO tasks(id,stage,item_type,item_id,state,payload,not_before,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (db.new_id("tsk"), stage, item_type, item_id, "PENDENTE",
                                                 json.dumps(payload or {}), not_before, config.iso(), config.iso()))
        # etapa que aguardava reexecução (ex.: QC após nova finalização) volta à fila
        c.execute("UPDATE tasks SET state='PENDENTE', updated_at=? WHERE stage=? AND item_id=? "
                  "AND state='AGUARDANDO_REEXECUCAO'", (config.iso(), stage, item_id))


def _feedback(task) -> str:
    fb = (task.get("payload") or {}).get("feedback")
    return f"\nATENÇÃO — a tentativa anterior foi reprovada pelo supervisor: {fb}" if fb else ""


def work_dir(item: str) -> Path:
    d = config.DIRS["intermediarios"] / item
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ handlers (nível 2)
def h_pesquisa(task):
    cfg = config.load()
    pedido = (task.get("payload") or {}).get("pedido") or None  # pedido feito na Central (tema/categoria/quantidade)
    col = research.collect(cfg, pedido)
    f = work_dir("pesquisa") / f"{task['id']}.json"
    f.write_text(json.dumps(col, ensure_ascii=False, indent=1), "utf-8")
    limit = int((pedido or {}).get("quantidade") or cfg["pesquisa"]["max_oportunidades"])
    if not col["candidatos"]:
        return {"collect_file": str(f), "llm": {"oportunidades": [], "descartados": []}, "proposals": []}
    # Sem WebSearch/WebFetch: títulos/descrições de terceiros podem conter injeção de prompt;
    # sem ferramentas de rede, um agente enganado não tem como enviar arquivos para fora.
    res = llm.call_agent("01-pesquisador", research.prompt(str(f), cfg["pesquisa"]["nichos"],
                         recommend.profile_text_for_agents(), int(limit * 1.5), pedido) + _feedback(task),
                         research.OPPORTUNITY_SCHEMA, task_id=task["id"], tools=["Read"])
    by_id = {c["candidato_id"]: c for c in col["candidatos"]}
    known = {r["source_url"] for r in db.query("SELECT source_url FROM opportunities")}
    picked = {o["candidato_id"]: o for o in res["oportunidades"]}
    # Arquivos da ENTRADA_LOCAL foram colocados pelo usuário: intenção explícita, sempre vão à fila
    for c in col["candidatos"]:
        if c["origem"] == "entrada_local" and c["candidato_id"] not in picked:
            motivo = next((d["motivo"] for d in res["descartados"] if d["candidato_id"] == c["candidato_id"]), "")
            picked[c["candidato_id"]] = {
                "candidato_id": c["candidato_id"], "assunto": c["titulo"], "resumo": "Material fornecido pelo usuário.",
                "motivo": "Arquivo colocado em ENTRADA_LOCAL." + (f" Observação da pesquisa: {motivo}" if motivo else ""),
                "nicho": "", "formato": "outro", "evidencias": ["fornecido diretamente pelo usuário"],
                "potencial": "a avaliar", "observacoes_uso": "", "confianca": 0.5, "idioma": "desconhecido"}
    props, local = [], []
    for o in picked.values():
        c = by_id.get(o["candidato_id"])
        if not c or c["url"] in known:
            continue
        p = {"id": db.new_id("opp"), "created_at": config.iso(), "topic": o["assunto"], "summary": o["resumo"],
             "reason": o["motivo"], "source_url": c["url"], "source_title": c["titulo"], "channel": c["canal"],
             "channel_id": c["canal_id"], "published_at": str(c.get("publicado") or ""), "niche": o["nicho"],
             "evidence": o["evidencias"], "potential": o["potencial"], "usage_notes": o["observacoes_uso"],
             "confidence": o["confianca"], "features": {"formato": o["formato"]}, "state": "PENDENTE",
             "language": None if o["idioma"] in ("", "desconhecido") else o["idioma"].lower()[:2],
             "language_source": "estimado pela pesquisa"}
        (local if c["origem"] == "entrada_local" else props).append(p)
    props, barrados = filter_language(props, by_id, cfg)
    for p in local:
        p["rec_explain"] = {"estrategia": "entrada_local", "mensagem": "Fornecido por você em ENTRADA_LOCAL.", "valor": 9}
    chosen = local + recommend.rank(props, max(0, limit - len(local)))
    # só são gravadas em on_success (após a auditoria): tentativa reprovada não deixa órfãs
    return {"collect_file": str(f), "llm": res, "estrangeiros_barrados": barrados,
            "proposals": [{k: v for k, v in p.items() if not k.startswith("_")} for p in chosen]}


def filter_language(props: list[dict], by_id: dict, cfg: dict) -> tuple[list[dict], list[str]]:
    """Conteúdo em português tem prioridade. Estrangeiro só passa se for MUITO viral (visualizações mínimas)
    e, mesmo assim, ocupa no máximo N vagas por pesquisa — os mais vistos primeiro."""
    pref = cfg["pesquisa"]["idioma"]
    main, foreign, barred = [], [], []
    for p in props:
        lang = (p.get("language") or "").lower()
        if not lang or lang.startswith(pref["principal"]):
            main.append(p)
            continue
        c = next((x for x in by_id.values() if x["url"] == p["source_url"]), {})
        views = c.get("visualizacoes") or 0
        if views >= pref["estrangeiro_min_visualizacoes"]:
            p["_views"] = views
            foreign.append(p)
        else:
            barred.append(f"{p['topic']} ({lang}, {views:,} visualizações)".replace(",", "."))
    foreign.sort(key=lambda p: -p["_views"])
    for p in foreign[pref["estrangeiro_max_por_pesquisa"]:]:
        barred.append(f"{p['topic']} (estrangeiro além do limite por pesquisa)")
    keep = foreign[:pref["estrangeiro_max_por_pesquisa"]]
    for p in keep:
        p["reason"] = f"[Estrangeiro muito viral: {p.pop('_views'):,} visualizações] ".replace(",", ".") + (p.get("reason") or "")
    return main + keep, barred


def h_verificacao(task):
    o = db.get("opportunities", task["item_id"])
    return {"availability": acquire.check_availability(o["source_url"], o["source_title"])}


def h_aquisicao(task):
    return acquire.acquire(task["item_id"], task["id"])


def h_compressao(task):
    return storage.compress(task["item_id"], task["id"])


def h_transcricao(task):
    return transcribe.transcribe(task["item_id"], task["id"])


def h_analise(task):
    vid = task["item_id"]
    v = db.get("videos", vid)
    o = db.get("opportunities", v["opportunity_id"]) or {}
    meta = work_dir(vid) / "metadados_fonte.json"
    meta.write_text(json.dumps({"titulo": v["title"], "canal": v["channel"], "url": v["url"],
                                "descricao": (o.get("availability") or {}).get("description", "")}, ensure_ascii=False), "utf-8")
    prompt = (f"Analise o vídeo {vid}. Transcrição compacta: {work_dir(vid) / 'transcricao_compacta.txt'} "
              f"(formato [id|início-fim|rótulo_da_voz] texto; '*' após o rótulo = baixa confiança). "
              f"Metadados da fonte: {meta}. Analise TODAS as vozes listadas." + _feedback(task))
    res = llm.call_agent("03-transcritor", prompt, ANALYSIS_SCHEMA, task_id=task["id"], item_id=vid, tools=["Read"])
    (work_dir(vid) / "analise.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    return {"llm": res, "files": [str(work_dir(vid) / "analise.json")]}


def h_selecao(task):
    vid = task["item_id"]
    cfg = config.load()["cortes"]
    for old in db.query("SELECT id FROM cuts WHERE video_id=? AND state='PENDENTE'", (vid,)):
        db.update("cuts", old["id"], {"state": "DESCARTADO"})  # nova tentativa substitui candidatos não revisados
    prompt = (f"Selecione cortes do vídeo {vid}. Transcrição: {work_dir(vid) / 'transcricao_compacta.txt'}; "
              f"análise de contexto: {work_dir(vid) / 'analise.json'}. Duração alvo {cfg['duracao_min']}-{cfg['duracao_max']}s, "
              f"no máximo {cfg['max_por_video']} cortes (menos se não houver bons). Pesos dos critérios: {cfg['pesos']}.\n"
              f"Perfil editorial:\n{recommend.profile_text_for_agents()}" + _feedback(task))
    res = llm.call_agent("04-editor", prompt, cuts.SELECTION_SCHEMA, task_id=task["id"], item_id=vid, tools=["Read"])
    parts = (db.get("transcripts", vid, "video_id") or {}).get("participants") or {}
    ids = cuts.materialize(vid, res, parts)
    fora = []
    for cid in list(ids):  # muito fora da faixa: descartado antes de chegar a você (motivo registrado)
        c = db.get("cuts", cid)
        d = cuts.total(c["scores"]["intervalos"])
        if d > cfg["duracao_max"] * 1.15 or d < cfg["duracao_min"] * 0.75:
            db.update("cuts", cid, {"state": "DESCARTADO", "alerts": c["alerts"] + [
                f"duração {d:.0f}s fora do alvo {cfg['duracao_min']}–{cfg['duracao_max']}s"]})
            ids.remove(cid)
            fora.append(f"{c['title_tmp']} ({d:.0f}s)")
    out = {"llm": res, "cuts": ids, "fora_da_duracao": fora}
    if ids:  # supervisão editorial com IA: chamada independente, um lote por vídeo
        lst = work_dir(vid) / "candidatos_para_revisao.json"
        lst.write_text(json.dumps([{"corte": c["id"], "inicio": c["start"], "fim": c["end"], "titulo": c["title_tmp"],
                                    "transcricao": c["transcript"], "motivo": c["reason"]}
                                   for c in map(lambda i: db.get("cuts", i), ids)], ensure_ascii=False), "utf-8")
        out["revisao_ia"] = llm.call_agent("sup-04-editorial",
            f"Audite os candidatos em {lst} contra a transcrição {work_dir(vid) / 'transcricao_compacta.txt'}.",
            REVIEW_SCHEMA, task_id=task["id"], item_id=vid, tools=["Read"])
    return out


def h_previa(task):
    ids = [c["id"] for c in db.query("SELECT id FROM cuts WHERE video_id=? AND state='PENDENTE'", (task["item_id"],))]
    for cid in ids:
        cuts.make_preview(cid)
    return {"cuts": ids}


def needs_translation(lang: str | None) -> bool:
    t = config.load()["traducao"]
    return bool(t["habilitada"] and lang and not lang.lower().startswith(t["idioma_destino"].lower()))


def h_legendas(task):
    cid = task["item_id"]
    cut = db.get("cuts", cid)
    st = subtitles.style()
    tr = cuts.load_transcript(cut["video_id"])
    lang = tr.get("language")
    translate = needs_translation(lang)
    # na tradução o português costuma ser ~20% mais longo: blocos de origem menores deixam folga
    src_chars = int(st["max_chars"] * 0.8) if translate else st["max_chars"]
    base = subtitles.build_blocks(tr["utterances"], cut["scores"]["intervalos"], src_chars)
    blocks = [dict(b) for b in base]
    out_dir = config.DIRS["intermediarios"] / "final" / cid
    out_dir.mkdir(parents=True, exist_ok=True)
    unc, revised, missing, review = [], False, [], None
    if translate and not config.load()["llm"]["habilitado"]:
        raise llm.LLMError(f"vídeo em '{lang}' exige tradução, mas as chamadas de IA estão desabilitadas")
    if config.load()["llm"]["habilitado"] and base:
        bf = out_dir / "blocos_para_revisao.json"
        bf.write_text(json.dumps([{"i": b["i"], "texto": b["text"], "locutor": b["speaker"],
                                   "baixa_confianca": b["low_conf"]} for b in base], ensure_ascii=False), "utf-8")
        if translate:
            prompt = (f"MODO TRADUÇÃO. Os blocos em {bf} estão em '{lang}'. Traduza cada bloco para português do Brasil, "
                      f"mantendo o mesmo 'i' e o sentido do que é dito naquela janela de tempo. "
                      f"Contexto do corte (em português): {cut['summary']}")
        else:
            prompt = f"MODO REVISÃO. Revise os blocos de legenda em {bf}. Contexto do corte: {cut['summary']}"
        res = llm.call_agent("05-legendas", prompt + _feedback(task), subtitles.REVIEW_SCHEMA,
                             task_id=task["id"], item_id=cid, tools=["Read"])
        fixed = {b["i"]: b for b in res["blocos"]}
        for b in blocks:
            if b["i"] in fixed and fixed[b["i"]]["texto"].strip():
                b["text"], b["destaque"] = fixed[b["i"]]["texto"].strip(), fixed[b["i"]]["destaque"]
            elif translate:
                missing.append(b["i"])  # sem tradução: o supervisor reprova (nunca sai legenda em inglês)
        unc, revised = res["incertezas"], True
        if translate and not missing:
            # auditoria independente da tradução (supervisor de legendas, IA): fidelidade de sentido
            pf = out_dir / "traducao_para_auditoria.json"
            pf.write_text(json.dumps({"incertezas_do_tradutor": unc,
                                      "blocos": [{"i": b["i"], "original": b["text"], "traducao": t["text"]}
                                                 for b, t in zip(base, blocks)]}, ensure_ascii=False), "utf-8")
            review = llm.call_agent("sup-05-legendas",
                                    f"Audite a tradução '{lang}' → português em {pf}. Contexto: {cut['summary']}",
                                    subtitles.TRANSLATION_AUDIT_SCHEMA, task_id=task["id"], item_id=cid, tools=["Read"])
    final = subtitles.fit(blocks, st["max_chars"])
    srt, ass = out_dir / "legenda.srt", out_dir / "legenda.ass"
    f = config.load()["final"]
    w, h = (f["largura"], f["altura"]) if f["formato"] == "vertical" else (1920, 1080)
    subtitles.write_srt(final, srt, st["max_chars"])
    subtitles.write_ass(final, ass, st, w, h)
    if translate:  # legenda no idioma original guardada como referência
        subtitles.write_srt(base, out_dir / "legenda_original.srt", src_chars)
    return {"base": base, "blocks": blocks, "final": final, "srt": str(srt), "ass": str(ass), "incertezas": unc,
            "revisado": revised, "idioma_original": lang, "traduzido": translate, "sem_traducao": missing,
            "revisao_traducao": review}


def h_finalizacao(task):
    out_dir = config.DIRS["intermediarios"] / "final" / task["item_id"]
    return render.render(task["item_id"], out_dir / "legenda.ass")


def h_qc(task):
    cid = task["item_id"]
    cut = db.get("cuts", cid)
    d = config.DIRS["intermediarios"] / "final" / cid
    f = config.load()["final"]
    w, h = (f["largura"], f["altura"]) if f["formato"] == "vertical" else (1920, 1080)
    prev = media.probe(cut["preview_path"])["duration"] if cut.get("preview_path") and Path(cut["preview_path"]).exists() else None
    res = qc.check(d / "video.mp4", d / "legenda.srt", cuts.total(cut["scores"]["intervalos"]), w, h, f["legenda"], prev)
    (d / "qc.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), "utf-8")
    return {"qc": res}


def h_textos(task):
    cid = task["item_id"]
    cut = db.get("cuts", cid)
    v = db.get("videos", cut["video_id"])
    o = db.get("opportunities", v["opportunity_id"]) or {}
    d = config.DIRS["intermediarios"] / "final" / cid
    ctx = d / "contexto_textos.json"
    ctx.write_text(json.dumps({"legenda_final": str(d / "legenda.srt"), "participantes": cut["participants"],
                               "assunto": cut["topic"], "resumo": cut["summary"], "fonte": v["url"], "canal": v["channel"],
                               "autorizacao": (o.get("availability") or {}).get("authorization"),
                               "idioma_original": cuts.load_transcript(cut["video_id"]).get("language"),
                               "legendas_traduzidas_para_portugues": (d / "legenda_original.srt").exists(),
                               "plataforma": config.load()["agenda"]["plataforma"],
                               "titulos_ja_usados": [r["title"] for r in db.query("SELECT title FROM exports WHERE title IS NOT NULL")][-50:]},
                              ensure_ascii=False), "utf-8")
    res = llm.call_agent("08-estrategista", f"Crie os textos de publicação a partir de {ctx} (leia também a legenda final)."
                         + _feedback(task), publish.TEXT_SCHEMA, task_id=task["id"], item_id=cid, tools=["Read"])
    (d / "textos.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    return {"llm": res}


def h_organizacao(task):
    cid = task["item_id"]
    d = config.DIRS["intermediarios"] / "final" / cid
    texts = json.loads((d / "textos.json").read_text("utf-8"))
    qcr = json.loads((d / "qc.json").read_text("utf-8"))
    return publish.organize(cid, d, texts, qcr)


def h_publicacao(task):
    exp = db.get("exports", task["item_id"], "cut_id")
    return publish.publish(exp["id"])


HANDLERS = {"PESQUISA": h_pesquisa, "VERIFICACAO": h_verificacao, "AQUISICAO": h_aquisicao,
            "COMPRESSAO": h_compressao, "TRANSCRICAO": h_transcricao, "ANALISE": h_analise,
            "SELECAO": h_selecao, "PREVIA": h_previa, "LEGENDAS": h_legendas, "FINALIZACAO": h_finalizacao,
            "QC": h_qc, "TEXTOS": h_textos, "ORGANIZACAO": h_organizacao, "PUBLICACAO": h_publicacao,
            "PG_BUSCA": pagina.h_busca, "PG_DOWNLOAD": pagina.h_download, "PG_MONTAGEM": pagina.h_montagem,
            "PG_QC": pagina.h_qc}


# ------------------------------------------------------------------ decisões do gerente
def _set_item_state(task, state):
    table = {"opportunity": "opportunities", "video": "videos", "cut": "cuts", "pagina": "page_videos"}.get(task["item_type"])
    if table:
        db.update(table, task["item_id"], {"state": state})


SUB_TESTS = {"srt_valida", "srt_sincronia", "legenda_na_tela"}  # defeitos que a renderização não resolve


def _cut_still_approved(task) -> bool:
    if task["stage"] not in CUT_STAGES:
        return True
    c = db.get("cuts", task["item_id"])
    return bool(c) and c["state"] == "APROVADO"


def on_success(task, out, rep):
    st, item = task["stage"], task["item_id"]
    if st.startswith("PG_"):
        return pagina.on_success(task, out)
    if st == "PESQUISA":
        known = {r["source_url"] for r in db.query("SELECT source_url FROM opportunities")}
        for p in out["proposals"]:
            if p["source_url"] in known:
                continue  # idempotente ao reaplicar após queda
            db.insert("opportunities", {**p, "created_at": config.iso()})
            enqueue("VERIFICACAO", "opportunity", p["id"])
    elif st == "VERIFICACAO":
        av = out["availability"]
        upd = {"availability": av, "authorization": av.get("authorization"),
               "state": "AGUARDANDO_APROVACAO" if av["ok"] else "DESCARTADO"}
        if av.get("language"):  # informado pela plataforma: mais confiável que a estimativa da pesquisa
            upd.update({"language": av["language"].lower()[:2], "language_source": "informado pela plataforma"})
        db.update("opportunities", item, upd)
    elif st == "AQUISICAO":
        db.update("opportunities", item, {"state": "CONCLUIDO"})
        enqueue("COMPRESSAO", "video", out["video_id"])
    elif st == "TRANSCRICAO":  # o áudio é a prova definitiva do idioma
        v = db.get("videos", item)
        db.update("videos", item, {"language": out["language"]})
        if v and v["opportunity_id"]:
            db.update("opportunities", v["opportunity_id"], {"language": out["language"],
                                                             "language_source": "detectado no áudio"})
    elif st == "COMPRESSAO":
        storage.finalize_compression(item)  # remove o original só depois da auditoria aprovar
    elif st == "ANALISE":
        res = out["llm"]
        merges = {p["rotulo"]: p["mesmo_que"] for p in res["participantes"] if p.get("mesmo_que")}
        transcribe.relabel(item, merges)
        t = db.get("transcripts", item, "video_id")
        parts = {k: v for k, v in t["participants"].items() if k not in merges}
        for p in res["participantes"]:
            if p["rotulo"] in parts:
                parts[p["rotulo"]].update({"name": p["nome"], "role": p["papel"],
                                           "evidence": p["fonte_evidencia"], "attempts": p["tentativas"]})
        db.update("transcripts", item, {"participants": parts, "topics": res["assuntos"], "summary": res["resumo"]},
                  key_col="video_id")
    elif st == "SELECAO":
        mine = set(out["cuts"])  # o veredito da IA só vale para os candidatos desta seleção
        for r in (out.get("revisao_ia") or {}).get("avaliacoes", []):
            c = db.get("cuts", r["corte"]) if r["corte"] in mine else None
            if not c or c["state"] != "PENDENTE":
                continue
            if r["veredito"] == "REPROVADO":
                db.update("cuts", c["id"], {"state": "DESCARTADO", "alerts": c["alerts"] + [f"supervisor editorial: {r['motivo']}"]})
            elif r["veredito"] == "APROVADO_COM_RESSALVAS":
                db.update("cuts", c["id"], {"alerts": c["alerts"] + [f"supervisor editorial: {r['motivo']}"]})
    elif st == "PREVIA":
        for cid in out["cuts"]:
            db.update("cuts", cid, {"state": "AGUARDANDO_APROVACAO"})
        db.update("videos", item, {"state": "AGUARDANDO_CORTES"})
    elif st == "PUBLICACAO":
        db.update("cuts", item, {"state": "CONCLUIDO"})
    if st in NEXT and _cut_still_approved(task):
        enqueue(NEXT[st], "cut" if st in CUT_STAGES else "video", item)


def _block(task, attempts, reason):
    db.update("tasks", task["id"], {"state": "BLOQUEADO", "attempts": attempts, "error": reason, "updated_at": config.iso()})
    _set_item_state(task, "BLOQUEADO")
    log.event(task_id=task["id"], item_id=task["item_id"], agent="gerente-geral",
              result=f"BLOQUEADO após {attempts} tentativas: {reason}", state="BLOQUEADO")


def on_failure(task, rep, exc):
    cfg = config.load()
    attempts = task["attempts"] + 1
    fb = "; ".join(rep["problemas"])[:800]
    if isinstance(exc, llm.LLMQuotaError):  # limite da assinatura: espera 30 min sem gastar tentativa
        db.update("tasks", task["id"], {"state": "PENDENTE", "error": f"limite de uso da IA; nova tentativa em 30 min: {exc}"[:500],
                                        "not_before": config.iso(config.now() + config.timedelta(minutes=30)),
                                        "updated_at": config.iso()})
        return
    if isinstance(exc, cota.LimitReached):  # teto de armazenamento: espera espaço, sem gastar tentativa
        db.update("tasks", task["id"], {"state": "PENDENTE", "not_before": config.iso(config.now() + config.timedelta(minutes=10)),
                                        "error": str(exc)[:500], "updated_at": config.iso()})
        return
    if isinstance(exc, MemoryError):  # falta de recurso momentânea: reagenda sem gastar tentativa
        db.update("tasks", task["id"], {"state": "PENDENTE", "not_before": config.iso(config.now() + config.timedelta(minutes=10)),
                                        "error": str(exc), "updated_at": config.iso()})
        return
    if attempts >= cfg["max_tentativas"]:
        return _block(task, attempts, fb or str(exc))
    if isinstance(exc, storage.NoSpace):
        db.update("tasks", task["id"], {"state": "FALHA", "attempts": attempts, "error": str(exc), "updated_at": config.iso(),
                                        "not_before": config.iso(config.now() + config.timedelta(hours=6))})
        return
    if task["stage"] == "QC":
        # defeito corrigível: volta à etapa responsável (legenda ou render); QC aguarda e reexecuta depois
        failed = set((rep.get("_qc_falhas") or []))
        redo = "LEGENDAS" if failed & SUB_TESTS else "FINALIZACAO"
        with db.tx() as c:
            c.execute("UPDATE tasks SET state='AGUARDANDO_REEXECUCAO', attempts=?, error=?, updated_at=? WHERE id=?",
                      (attempts, fb, config.iso(), task["id"]))
            if redo == "LEGENDAS":
                c.execute("UPDATE tasks SET state='AGUARDANDO_REEXECUCAO' WHERE stage='FINALIZACAO' AND item_id=?",
                          (task["item_id"],))
            c.execute("UPDATE tasks SET state='PENDENTE', payload=?, updated_at=? WHERE stage=? AND item_id=?",
                      (json.dumps({"feedback": fb}), config.iso(), redo, task["item_id"]))
        return
    payload = dict(task.get("payload") or {})
    payload["feedback"] = fb
    db.update("tasks", task["id"], {"state": "PENDENTE", "attempts": attempts, "error": fb, "payload": payload,
                                    "not_before": config.iso(config.now() + config.timedelta(minutes=2 ** attempts)),
                                    "updated_at": config.iso()})


def claim(task_id: str) -> bool:
    """Reserva atômica: só um executor pega a tarefa."""
    with db.tx() as c:
        cur = c.execute("UPDATE tasks SET state='EM_EXECUCAO', updated_at=? WHERE id=? "
                        "AND state IN ('PENDENTE','FALHA','PENDENTE_AUDITORIA')", (config.iso(), task_id))
        return cur.rowcount == 1


def run_task(task: dict) -> dict | None:
    reaudit = task["state"] == "PENDENTE_AUDITORIA"
    if not reaudit and task["stage"] in cota.RESERVA:
        try:
            cota.ensure(cota.RESERVA[task["stage"]], task["stage"].lower())
        except cota.LimitReached as e:  # não cabe abaixo do teto: espera sem gastar tentativa
            db.update("tasks", task["id"], {"not_before": config.iso(config.now() + config.timedelta(minutes=10)),
                                            "error": str(e)[:500], "updated_at": config.iso()})
            return None
    if not claim(task["id"]):
        return None
    agent, sup, _ = supervisors.REGISTRY[task["stage"]]
    if not _cut_still_approved(task):  # corte desfeito/rejeitado enquanto aguardava: nunca segue adiante
        db.update("tasks", task["id"], {"state": "CANCELADO", "error": "corte não está mais aprovado"})
        log.event(task_id=task["id"], item_id=task["item_id"], agent="gerente-geral", result="CANCELADO", state="CANCELADO")
        return None
    out, err, exc = (task.get("output"), None, None) if reaudit else (None, None, None)
    if not reaudit:
        log.event(task_id=task["id"], item_id=task["item_id"], agent=agent, requested=task["stage"], state="EM_EXECUCAO")
        try:
            out = HANDLERS[task["stage"]](task)
        except Exception as e:  # noqa: BLE001
            exc, err = e, f"{type(e).__name__}: {e}"
            log.event(task_id=task["id"], item_id=task["item_id"], agent=agent, error=traceback.format_exc()[-1500:], state="FALHA")
        out = json.loads(json.dumps(out, default=str)) if out else None
    db.update("tasks", task["id"], {"state": "EM_VALIDACAO", "output": out})
    rep = supervisors.audit(task, out, err)
    rdir = config.DIRS["relatorios"] / config.iso()[:10]
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{task['stage']}_{task['id']}.txt").write_text(supervisors.render_text(rep), "utf-8")
    if rep["pode_avancar"]:
        # AUDITADO = aprovado mas efeitos ainda não aplicados; recover() os reaplica (on_success é idempotente)
        db.update("tasks", task["id"], {"state": "AUDITADO", "updated_at": config.iso(), "error": None})
        on_success(task, out, rep)
        db.update("tasks", task["id"], {"state": "CONCLUIDO", "updated_at": config.iso()})
        decision = "AVANCAR"
    else:
        if task["stage"] == "QC" and out:
            rep["_qc_falhas"] = out["qc"].get("falhas", [])
        on_failure(task, rep, exc)
        decision = "REPETIR_OU_BLOQUEAR"
    with db.tx() as c:
        c.execute("UPDATE reports SET manager_decision=? WHERE task_id=? AND id=(SELECT MAX(id) FROM reports WHERE task_id=?)",
                  (decision, task["id"], task["id"]))
    if task["stage"].startswith("PG_"):
        pagina.maybe_release_focus()
    return rep


def recover() -> int:
    """Após queda/reinício (só o dono do lock chama): nada concluído é refeito.
    - AUDITADO: reaplica os efeitos de on_success (idempotentes);
    - EM_VALIDACAO com saída salva: só reaudita, não reexecuta a etapa;
    - EM_EXECUCAO: volta à fila contando tentativa (ou bloqueia no limite)."""
    cfg = config.load()
    stale = db.query("SELECT * FROM tasks WHERE state IN ('EM_EXECUCAO','EM_VALIDACAO','AUDITADO')")
    for t in stale:
        if t["state"] == "AUDITADO":
            rep = (db.query("SELECT body FROM reports WHERE task_id=? ORDER BY id DESC LIMIT 1", (t["id"],)) or [{}])[0].get("body") or {}
            on_success(t, t["output"], rep)
            db.update("tasks", t["id"], {"state": "CONCLUIDO", "updated_at": config.iso()})
        elif t["state"] == "EM_VALIDACAO" and t["output"]:
            db.update("tasks", t["id"], {"state": "PENDENTE_AUDITORIA", "updated_at": config.iso()})
        else:
            # Fechar o .bat é o desligamento normal: interrupção NÃO conta como tentativa falha.
            # Contador próprio só barra ciclos de queda (ex.: falta de memória sempre na mesma etapa).
            payload = dict(t.get("payload") or {})
            payload["interrupcoes"] = payload.get("interrupcoes", 0) + 1
            if payload["interrupcoes"] >= MAX_INTERRUPTIONS:
                db.update("tasks", t["id"], {"payload": payload})
                _block(t, t["attempts"], f"interrompida {payload['interrupcoes']} vezes seguidas na mesma etapa "
                                         "(possível falta de memória)")
            else:
                db.update("tasks", t["id"], {"state": "PENDENTE", "payload": payload, "updated_at": config.iso(),
                                             "error": "interrompida (o .bat foi fechado); retomada automática"})
    return len(stale)


def _texto_do_item(t: dict) -> str:
    """Assunto/título ligado à tarefa, para o foco por tema."""
    it, iid = t["item_type"], t["item_id"]
    if it == "batch":
        p = (t.get("payload") or {}).get("pedido") or {}
        return f"{p.get('tema') or ''} {p.get('categoria') or ''}"
    if it == "cut":
        c = db.get("cuts", iid) or {}
        it, iid = "video", c.get("video_id")
        extra = f"{c.get('title_tmp') or ''} {c.get('topic') or ''}"
    else:
        extra = ""
    if it == "video":
        v = db.get("videos", iid) or {}
        o = db.get("opportunities", v.get("opportunity_id")) or {} if v.get("opportunity_id") else {}
        return f"{extra} {v.get('title') or ''} {o.get('topic') or ''} {o.get('source_title') or ''}"
    if it == "opportunity":
        o = db.get("opportunities", iid) or {}
        return f"{o.get('topic') or ''} {o.get('source_title') or ''} {o.get('summary') or ''}"
    return ""


def tema_ok(t: dict, termos: list[str]) -> bool:
    txt = " ".join(_texto_do_item(t).casefold().split())
    return any(" ".join(x.casefold().split()) in txt for x in termos if x.strip())


def next_task() -> dict | None:
    now = config.iso()
    order = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(ORDER))
    only = ""
    cfg = config.load()
    foco = cfg.get("foco")
    if foco == "pagina":  # foco total no lote de Vídeos de Página
        only = "AND stage LIKE 'PG\\_%' ESCAPE '\\' "
    elif cota.paused():  # limite de armazenamento atingido: só etapas que não gravam arquivos (ou que liberam espaço)
        only = "AND stage IN (" + ",".join(f"'{s}'" for s in sorted(cota.LEVES | cota.REDUZ_USO)) + ") "
    if foco == "tema":  # foco total num assunto (ex.: GTA 6): o resto fica parado na fila, sem ser apagado
        only += "AND stage NOT LIKE 'PG\\_%' ESCAPE '\\' "
        rows = db.query(f"SELECT * FROM tasks WHERE state IN ('PENDENTE','FALHA','PENDENTE_AUDITORIA') {only}"
                        f"AND (not_before IS NULL OR not_before<=?) ORDER BY CASE stage {order} END, created_at LIMIT 500",
                        (now,))
        termos = cfg.get("foco_termos") or []
        return next((t for t in rows if tema_ok(t, termos)), None)
    rows = db.query(f"SELECT * FROM tasks WHERE state IN ('PENDENTE','FALHA','PENDENTE_AUDITORIA') {only}"
                    f"AND (not_before IS NULL OR not_before<=?) ORDER BY CASE stage {order} END, created_at LIMIT 1", (now,))
    return rows[0] if rows else None


def request_research(tema: str = "", categoria: str = "", quantidade: int | None = None,
                     min_visualizacoes: int | None = None) -> str:
    """Pesquisa pedida pelo usuário na Central. Campos opcionais; vai para a fila na hora."""
    pedido = {"tema": (tema or "").strip()[:300], "categoria": (categoria or "").strip()[:80],
              "quantidade": max(1, min(int(quantidade), 30)) if quantidade else None,
              "min_visualizacoes": max(0, int(min_visualizacoes)) if min_visualizacoes else 0}
    item = f"pedido_{config.iso()[:19]}_{db.new_id('p')[-4:]}"  # dois pedidos no mesmo segundo não se fundem
    enqueue("PESQUISA", "batch", item, {"pedido": pedido})
    return item


def maybe_schedule_research() -> None:
    if config.load().get("foco"):
        return  # foco em outro trabalho: não abre pesquisa nova
    cfg = config.load()["pesquisa"]
    last = db.kv_get("ultima_pesquisa")
    if last and (config.now() - config.parse(last)).total_seconds() < cfg["intervalo_h"] * 3600:
        return
    pending = db.query("SELECT COUNT(*) n FROM opportunities WHERE state IN ('AGUARDANDO_APROVACAO','PENDENTE')")[0]["n"]
    if pending >= config.load()["pesquisa"]["max_oportunidades"]:
        return  # fila ainda cheia: não gasta cota pesquisando mais
    enqueue("PESQUISA", "batch", f"pesquisa_{config.iso()[:13]}")
    db.kv_set("ultima_pesquisa", config.iso())


def work_once(research: bool = True) -> dict | None:
    if research:
        maybe_schedule_research()
    t = next_task()
    return run_task(t) if t else None


_LOCK = None


def acquire_lock() -> bool:
    """Garante um único gerente por pasta de produção (evita duas instâncias reabrindo tarefas alheias)."""
    global _LOCK
    if _LOCK:
        return True
    import msvcrt
    f = open(config.PROD / "gerente.lock", "a+")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return False
    _LOCK = f
    return True


def loop(stop_event=None, idle_s: int = 30) -> None:
    if not acquire_lock():
        log.event(agent="gerente-geral", result="outro gerente já está ativo; esta instância não processa tarefas")
        print("Outro gerente já está ativo nesta pasta de produção; esta instância não processará tarefas.")
        return
    media.lower_own_priority()
    recover()
    last_sweep = 0.0
    while not (stop_event and stop_event.is_set()):
        if time.time() - last_sweep > 3600:
            try:
                storage.retention_sweep()
                from . import pos_postagem
                pos_postagem.sweep()  # vídeo final já postado: apagado no dia seguinte
            except Exception as e:  # noqa: BLE001
                log.event(agent="retencao", error=str(e), state="FALHA")
            last_sweep = time.time()
        busy = bool(db.query("SELECT 1 FROM tasks WHERE state IN ('PENDENTE','FALHA','PENDENTE_AUDITORIA') LIMIT 1"))
        media.keep_awake(busy)  # impede a suspensão só enquanto há trabalho (some ao fechar o .bat)
        try:
            rep = work_once()
        except Exception as e:  # noqa: BLE001 — o laço nunca morre por uma tarefa
            log.event(agent="gerente-geral", error=traceback.format_exc()[-1500:], state="FALHA")
            rep = None
        if rep is None:
            (stop_event.wait(idle_s) if stop_event else time.sleep(idle_s))
