"""Acompanhamento da produção em tempo real: o que está rodando, o que vem a seguir e o andamento de cada item."""
from __future__ import annotations

from . import config, db, orchestrator, supervisors

VIDEO_STEPS = ["AQUISICAO", "COMPRESSAO", "TRANSCRICAO", "ANALISE", "SELECAO", "PREVIA"]
CUT_STEPS = ["LEGENDAS", "FINALIZACAO", "QC", "TEXTOS", "ORGANIZACAO", "PUBLICACAO"]
LABEL = {"PESQUISA": "Pesquisando assuntos", "VERIFICACAO": "Verificando disponibilidade", "AQUISICAO": "Baixando",
         "COMPRESSAO": "Comprimindo", "TRANSCRICAO": "Transcrevendo e separando vozes", "ANALISE": "Identificando participantes",
         "SELECAO": "Escolhendo cortes", "PREVIA": "Gerando prévias", "LEGENDAS": "Legendando",
         "FINALIZACAO": "Renderizando", "QC": "Controle de qualidade", "TEXTOS": "Escrevendo título e descrição",
         "ORGANIZACAO": "Organizando arquivos", "PUBLICACAO": "Preparando publicação",
         "PG_BUSCA": "Caçando vídeos virais", "PG_DOWNLOAD": "Baixando vídeo de página",
         "PG_MONTAGEM": "Montando vídeo de página", "PG_QC": "Conferindo vídeo de página"}
# estimativas medidas neste PC (fração da duração do vídeo-fonte); etapas de IA ~1–2 min fixos
RATIO = {"COMPRESSAO": 0.3, "TRANSCRICAO": 0.55}
FIXED_S = {"PG_BUSCA": 150, "PG_DOWNLOAD": 30, "PG_MONTAGEM": 150, "PG_QC": 20, "ANALISE": 90, "SELECAO": 120, "LEGENDAS": 60, "FINALIZACAO": 60, "TEXTOS": 30, "PESQUISA": 60}


def _title(task: dict) -> str:
    t, i = task["item_type"], task["item_id"]
    if t == "pagina_lote":
        return "lote de Vídeos de Página"
    row = db.get({"opportunity": "opportunities", "video": "videos", "cut": "cuts", "pagina": "page_videos"}.get(t, "kv"), i) if t != "batch" else None
    if not row:
        return "lote de pesquisa" if t == "batch" else i
    return row.get("topic") or row.get("title") or row.get("title_tmp") or i


def _source_seconds(task: dict) -> float | None:
    if task["item_type"] == "video":
        v = db.get("videos", task["item_id"])
        return v and v["duration"]
    if task["item_type"] == "opportunity":
        o = db.get("opportunities", task["item_id"])
        return o and (o.get("availability") or {}).get("duration")
    return None


def _estimate(task: dict) -> int | None:
    if task["stage"] in RATIO:
        d = _source_seconds(task)
        return int(d * RATIO[task["stage"]]) if d else None
    return FIXED_S.get(task["stage"])


def _elapsed(ts: str | None) -> int:
    return max(0, int((config.now() - config.parse(ts)).total_seconds())) if ts else 0


def _steps(item_ids: list[str], steps: list[str]) -> list[dict]:
    rows = {r["stage"]: r for r in db.query(
        f"SELECT stage, state, attempts, error FROM tasks WHERE item_id IN ({','.join('?' * len(item_ids))})", item_ids)}
    out = []
    for s in steps:
        r = rows.get(s)
        st = "feito" if r and r["state"] == "CONCLUIDO" else "agora" if r and r["state"] in ("EM_EXECUCAO", "EM_VALIDACAO") \
            else "erro" if r and r["state"] in ("BLOQUEADO",) else "repetindo" if r and r["attempts"] else \
            "na fila" if r else "aguardando"
        if st == "agora" and orchestrator._LOCK is None:
            st = "interrompido"  # o .bat foi fechado no meio; retoma ao abrir
        out.append({"etapa": s, "nome": LABEL[s], "estado": st, "erro": (r or {}).get("error") if st in ("erro", "repetindo") else None})
    return out


AUDIT = {"APROVADO": "aprovou", "APROVADO_COM_RESSALVAS": "aprovou com ressalvas", "REPROVADO": "reprovou"}


def _feed(e: dict) -> dict | None:
    """Converte o log técnico em frase legível; omite varreduras de retenção sem efeito."""
    if e["agent"] == "retencao":
        if "'excluidos': 0" in (e["result"] or "") and "'trechos_removidos': 0" in (e["result"] or ""):
            return None
        return {"ts": e["ts"], "texto": f"Limpeza de originais: {e['result']}"}
    item = _title(e) if e.get("item_id") else ""
    if e["requested"] == "auditoria" and e["stage"]:
        txt = f"Supervisor {AUDIT.get(e['result'], e['result'])} — {LABEL.get(e['stage'], e['stage'])}: {item}"
    elif e["state"] in ("FALHA", "BLOQUEADO") and not e["result"]:
        txt = f"Falha em {LABEL.get(e['stage'], e['stage'] or '')}: {item}"
    else:
        txt = f"{e['agent']}: {e['result']}" + (f" ({item})" if item else "")
    return {"ts": e["ts"], "texto": txt}


def snapshot() -> dict:
    running = db.query("SELECT * FROM tasks WHERE state IN ('EM_EXECUCAO','EM_VALIDACAO') ORDER BY updated_at")         if orchestrator._LOCK is not None else []
    now = [{"etapa": t["stage"], "nome": LABEL.get(t["stage"], t["stage"]), "item": _title(t),
            "agente": supervisors.REGISTRY[t["stage"]][0], "supervisor": supervisors.REGISTRY[t["stage"]][1], "desde": t["updated_at"], "decorrido_s": _elapsed(t["updated_at"]),
            "estimativa_s": _estimate(t), "auditando": t["state"] == "EM_VALIDACAO"} for t in running]
    order = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(orchestrator.ORDER))
    queued = db.query(f"SELECT * FROM tasks WHERE state IN ('PENDENTE','FALHA','PENDENTE_AUDITORIA') "
                      f"ORDER BY CASE stage {order} END, created_at LIMIT 12")
    from . import cota
    arm = cota.status()
    fila = [{"etapa": t["stage"], "nome": LABEL.get(t["stage"], t["stage"]), "item": _title(t),
             "espera_ate": t["not_before"] if t["not_before"] and t["not_before"] > config.iso() else None,
             "tentativa": t["attempts"] + 1} for t in queued]

    videos = []
    for o in db.query("SELECT * FROM opportunities WHERE state IN ('APROVADO','CONCLUIDO','BLOQUEADO') "
                      "ORDER BY created_at DESC LIMIT 20"):
        v = (db.query("SELECT * FROM videos WHERE opportunity_id=?", (o["id"],)) or [None])[0]
        ids = [o["id"]] + ([v["id"]] if v else [])
        steps = _steps(ids, VIDEO_STEPS)
        cuts = db.query("SELECT * FROM cuts WHERE video_id=? AND state IN ('AGUARDANDO_APROVACAO','APROVADO','CONCLUIDO','BLOQUEADO')",
                        (v["id"],)) if v else []
        cut_rows = [{"titulo": c["title_tmp"], "estado": c["state"],
                     "etapas": _steps([c["id"]], CUT_STEPS) if c["state"] != "AGUARDANDO_APROVACAO" else []} for c in cuts]
        all_done = all(s["estado"] == "feito" for s in steps) and cut_rows and \
            all(c["estado"] == "CONCLUIDO" or c["estado"] == "AGUARDANDO_APROVACAO" for c in cut_rows)
        videos.append({"assunto": o["topic"], "idioma": (v or {}).get("language") or o.get("language"),
                       "duracao_s": (v or {}).get("duration") or (o.get("availability") or {}).get("duration"),
                       "etapas": steps, "cortes": cut_rows, "concluido": bool(all_done)})

    waiting_cuts = db.query("SELECT COUNT(*) n FROM cuts WHERE state='AGUARDANDO_APROVACAO'")[0]["n"]
    waiting_opps = db.query("SELECT COUNT(*) n FROM opportunities WHERE state='AGUARDANDO_APROVACAO'")[0]["n"]
    if now:
        resumo = f"{now[0]['nome']}: {now[0]['item']}"
        estado = "trabalhando"
    elif any(not f["espera_ate"] for f in fila):
        resumo, estado = "Começando a próxima etapa…", "trabalhando"
    elif fila:
        resumo, estado = f"Nova tentativa agendada ({fila[0]['nome']})", "aguardando"
    elif waiting_cuts or waiting_opps:
        resumo, estado = f"Esperando sua avaliação: {waiting_opps} assunto(s), {waiting_cuts} corte(s)", "aguardando_voce"
    else:
        resumo, estado = "Nada para fazer agora", "ocioso"
    if arm["pausado"] and not now:
        resumo = (f"Pausado: armazenamento cheio ({arm['usado'] / (1 << 30):.1f} de {arm['limite'] / (1 << 30):.0f} GB). "
                  "Volta sozinho quando liberar espaço")
        estado = "aguardando"
    gerente = orchestrator._LOCK is not None
    if not gerente:
        resumo, estado = "Gerente parado (abra o iniciar.bat para produzir)", "parado"
    recent = [r for r in (_feed(e) for e in db.query(
        "SELECT e.ts, e.agent, e.supervisor, e.requested, e.result, e.state, t.stage, t.item_type, t.item_id "
        "FROM events e LEFT JOIN tasks t ON t.id=e.task_id WHERE e.agent<>'usuario' "
        "AND (e.result IS NOT NULL OR e.state IN ('FALHA','BLOQUEADO')) ORDER BY e.id DESC LIMIT 60")) if r][:15]
    return {"estado": estado, "resumo": resumo, "agora": now, "fila": fila, "videos": videos,
            "recentes": recent, "gerente_ativo": gerente, "armazenamento": arm}
