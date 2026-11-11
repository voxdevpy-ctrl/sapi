"""Os cinco botões: registro imediato da decisão, efeitos de estado e desfazer."""
from __future__ import annotations

from . import config, db, log, recommend

BUTTONS = {100: "MUITO APROVADO", 50: "APROVADO", 0: "IRRELEVANTE / ADIAR",
           -50: "DESAPROVADO", -100: "MUITO DESAPROVADO"}
REASONS = ["Falta de contexto", "Começo ruim", "Encerramento ruim", "Trecho sem interesse",
           "Conversa desconexa", "Corte repetitivo", "Duração inadequada", "Pausas ou ritmo ruins",
           "Áudio ou imagem ruins", "Conteúdo inadequado ao perfil", "Assunto interessante, mas execução ruim",
           "Fonte pouco interessante", "Outro motivo", "Sem motivo informado"]
NEXT_STAGE = {"opportunity": "AQUISICAO", "cut": "LEGENDAS"}
TABLE = {"opportunity": "opportunities", "cut": "cuts"}


def state_for(score: int) -> str:
    return "APROVADO" if score > 0 else "ADIADO" if score == 0 else "REJEITADO"


def decide(kind: str, item_id: str, score: int, reason: str | None = None) -> dict:
    if score not in BUTTONS:
        raise ValueError("pontuação inválida")
    item = db.get(TABLE[kind], item_id)
    if not item:
        raise KeyError(item_id)
    if item["state"] not in ("AGUARDANDO_APROVACAO", "ADIADO"):
        raise PermissionError(f"item em estado {item['state']} não está aguardando decisão")
    if kind == "opportunity" and score > 0 and not (item.get("availability") or {}).get("ok"):
        raise PermissionError("aquisição bloqueada: disponibilidade não confirmada")
    cfg = config.load()
    feats = recommend.opportunity_features(item) if kind == "opportunity" else (item.get("features") or {})
    before = recommend.weights(kind)
    snapshot = {k: item.get(k) for k in ("topic", "summary", "source_url", "channel", "niche", "start", "end",
                                         "transcript", "context_before", "context_after", "reason", "participants",
                                         "score_total", "video_id") if k in item}
    if kind == "cut" and score > 0:
        from . import storage
        try:
            storage.cut_source(item, db.get("videos", item["video_id"]) or {})
        except FileNotFoundError as e:
            raise PermissionError(f"não é possível aprovar: {e}") from e
    with db.tx() as c:
        # reconfere o estado dentro da transação (dois cliques/abas simultâneos não geram estados conflitantes)
        upd = c.execute(f"UPDATE {TABLE[kind]} SET state=? WHERE id=? AND state IN ('AGUARDANDO_APROVACAO','ADIADO')",
                        (state_for(score), item_id))
        if upd.rowcount != 1:
            raise PermissionError("o item já foi decidido em outra janela")
        cur = c.execute("INSERT INTO decisions(kind,item_id,score,label,reason,features,snapshot,decided_at,"
                        "criteria_version,is_test) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (kind, item_id, score, BUTTONS[score], reason or "Sem motivo informado",
                         db._enc("features", feats), db._enc("snapshot", snapshot), config.iso(),
                         cfg["cortes"]["versao_criterios"], item.get("is_test", 0)))
        dec_id = cur.lastrowid
        if score > 0:
            nb = config.iso(config.now() + config.timedelta(seconds=cfg["janela_desfazer_s"]))
            c.execute("INSERT OR IGNORE INTO tasks(id,stage,item_type,item_id,state,not_before,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?)", (db.new_id("tsk"), NEXT_STAGE[kind], kind, item_id, "PENDENTE",
                                                   nb, config.iso(), config.iso()))
    if not item.get("is_test"):
        recommend.record_profile_changes(before, recommend.weights(kind))
    log.event(item_id=item_id, agent="usuario", requested=f"decisão {kind}",
              result=f"{BUTTONS[score]} ({score:+d}) — {reason or 'sem motivo'}", state=state_for(score))
    return {"decision_id": dec_id, "state": state_for(score), "label": BUTTONS[score]}


def undo(decision_id: int) -> dict:
    d = db.get("decisions", decision_id)
    if not d or d["undone"]:
        raise KeyError("decisão inexistente ou já desfeita")
    stage = NEXT_STAGE[d["kind"]]
    with db.tx() as c:
        # apaga a tarefa só se ainda estiver pendente — atômico contra o gerente pegá-la ao mesmo tempo
        c.execute("DELETE FROM tasks WHERE stage=? AND item_id=? AND state='PENDENTE'", (stage, d["item_id"]))
        if c.execute("SELECT 1 FROM tasks WHERE stage=? AND item_id=?", (stage, d["item_id"])).fetchone():
            raise PermissionError("a produção já começou para este item; não é possível desfazer")
        c.execute("UPDATE decisions SET undone=1 WHERE id=?", (decision_id,))
        c.execute(f"UPDATE {TABLE[d['kind']]} SET state='AGUARDANDO_APROVACAO' WHERE id=?", (d["item_id"],))
    log.event(item_id=d["item_id"], agent="usuario", requested="desfazer decisão", result=f"decisão {decision_id} desfeita")
    return {"item_id": d["item_id"], "kind": d["kind"]}


def stats(kind: str, since: str | None = None) -> dict:
    rows = db.query("SELECT score, decided_at FROM decisions WHERE kind=? AND undone=0", (kind,))
    if since:  # comparação por instante (com fuso), não por texto
        t0 = config.parse(since)
        t0 = t0 if t0.tzinfo else t0.astimezone()
        rows = [r for r in rows if config.parse(r["decided_at"]) >= t0]
    by: dict[int, int] = {}
    for r in rows:
        by[r["score"]] = by.get(r["score"], 0) + 1
    pend = db.query(f"SELECT COUNT(*) n FROM {TABLE[kind]} WHERE state='AGUARDANDO_APROVACAO'")[0]["n"]
    return {"avaliados": sum(by.values()), "por_pontuacao": {BUTTONS[k]: by.get(k, 0) for k in BUTTONS},
            "aprovados": by.get(100, 0) + by.get(50, 0), "adiados": by.get(0, 0),
            "descartados": by.get(-50, 0) + by.get(-100, 0), "pendentes": pend}
