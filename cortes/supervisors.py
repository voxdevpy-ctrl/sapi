"""Nível 3 — supervisores individuais. Somente leitura: auditam evidências reais (arquivos, banco,
logs) e produzem relatório curto para o gerente-geral. Não refazem o trabalho do agente."""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from datetime import timedelta
from pathlib import Path

from . import config, cuts, db, log, media, qc, subtitles

AP, AR, RP = "APROVADO", "APROVADO_COM_RESSALVAS", "REPROVADO"


class Audit:
    def __init__(self, task: dict, deveria: str):
        self.task, self.deveria = task, deveria
        self.evid: list[str] = []
        self.prob: list[str] = []
        self.warn: list[str] = []
        self.fix: list[str] = []

    def ok(self, cond: bool, evidence: str, problem: str, fix: str = "", soft: bool = False) -> bool:
        if cond:
            self.evid.append(evidence)
        else:
            (self.warn if soft else self.prob).append(problem)
            if fix:
                self.fix.append(fix)
        return cond

    def report(self, fez: str) -> dict:
        status = RP if self.prob else AR if self.warn else AP
        return {"tarefa": self.task["id"], "etapa": self.task["stage"], "item": self.task["item_id"],
                "deveria": self.deveria, "fez": fez, "correto": not self.prob, "evidencias": self.evid,
                "problemas": self.prob, "ressalvas": self.warn, "correcoes": self.fix,
                "verificacao": "repetir a etapa e reauditar os mesmos critérios" if self.prob else "",
                "status": status, "pode_avancar": status != RP}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _file(a: Audit, p, what: str) -> bool:
    return a.ok(bool(p) and Path(p).exists() and Path(p).stat().st_size > 0,
                f"{what} existe ({Path(p).stat().st_size if p and Path(p).exists() else 0} bytes)",
                f"{what} ausente ou vazio: {p}", f"regerar {what}")


def _errors(a: Audit, error: str | None) -> bool:
    if error:
        a.prob.append(f"o agente terminou com erro: {error[:300]}")
        a.fix.append("corrigir a causa indicada e repetir")
        return True
    return False


# ---------------- 01 pesquisa ----------------
def pesquisa(task, out, error):
    a = Audit(task, "Coletar candidatos reais e propor oportunidades verificáveis, sem inventar fontes.")
    if _errors(a, error):
        return a.report("falhou")
    col = json.loads(Path(out["collect_file"]).read_text("utf-8"))
    ids = {c["candidato_id"] for c in col["candidatos"]}
    picked = out["llm"]["oportunidades"]
    props = out["proposals"]
    a.ok(all(o["source_url"] in {c["url"] for c in col["candidatos"]} for o in props),
         f"{len(props)} oportunidades propostas, todas com URL coletada", "oportunidade com URL não coletada")
    a.ok(all(o["candidato_id"] in ids for o in picked), f"{len(picked)} oportunidades referenciam candidatos coletados",
         "oportunidade com candidato_id inexistente (possível fonte inventada)", "descartar itens sem candidato real")
    a.ok(len({o["candidato_id"] for o in picked}) == len(picked), "sem duplicatas", "oportunidades duplicadas")
    a.ok(all(o["evidencias"] for o in picked), "todas têm evidências", "oportunidade sem evidência de interesse",
         "exigir evidência ou descartar")
    chans = [next((c["canal"] for c in col["candidatos"] if c["candidato_id"] == o["candidato_id"]), None) for o in picked]
    a.ok(len(picked) < 3 or len(set(chans)) > 1, "fontes diversificadas", "todas as oportunidades vêm de um único canal",
         "ampliar consultas", soft=True)
    a.ok(bool(col["candidatos"]), f"{len(col['candidatos'])} candidatos coletados",
         "nenhum candidato coletado: " + "; ".join(col["erros"][:3]), "verificar consultas e conexão", soft=True)
    return a.report(f"coletou {len(col['candidatos'])} candidatos; propôs {len(props)}")


# ---------------- 02 aquisição ----------------
def aquisicao(task, out, error):
    st = task["stage"]
    if st == "VERIFICACAO":
        a = Audit(task, "Confirmar disponibilidade real e base de autorização antes da aprovação humana.")
        if _errors(a, error):
            return a.report("falhou")
        av = out["availability"]
        a.ok(bool(av.get("checked_at") and av.get("method") is not None or av["problems"]),
             f"verificação registrada em {av.get('checked_at')} via {av.get('method')}", "verificação sem evidência")
        if av["ok"]:
            a.ok(bool(av.get("authorization")), f"base de autorização: {av.get('authorization')}",
                 "liberada sem base de autorização")
            a.ok((av.get("duration") or 0) >= 60, f"duração {av.get('duration')}s", "duração inválida")
        else:
            a.evid.append("fonte corretamente retida: " + "; ".join(av["problems"]))
        return a.report("disponível" if av["ok"] else "retida: " + "; ".join(av["problems"]))
    if st == "AQUISICAO":
        a = Audit(task, "Adquirir a fonte aprovada integralmente.")
        if _errors(a, error):
            return a.report("falhou")
        v = db.get("videos", out["video_id"])
        _file(a, v["original_path"], "original")
        a.ok(abs(v["duration"] - (out["availability"].get("duration") or v["duration"])) <= 2,
             f"duração {v['duration']:.0f}s confere com a fonte", "arquivo incompleto")
        return a.report(f"adquiriu {v['original_path']}")
    a = Audit(task, "Comprimir, validar integridade e registrar retenção de 14 dias.")
    if _errors(a, error):
        return a.report("falhou; original preservado")
    v = db.get("videos", task["item_id"])
    if _file(a, v["compressed_path"], "cópia de trabalho"):
        a.ok(media.sha256(v["compressed_path"]) == v["compressed_sha256"], "hash SHA-256 confere",
             "hash divergente da cópia", "recomprimir")
    exp = config.parse(v["acquired_at"]) + timedelta(days=config.load()["retencao_dias"])
    a.ok(v["retention_until"] == config.iso(exp), f"exclusão prevista para {v['retention_until']}",
         "data de retenção incorreta", "recalcular")
    return a.report(v["compression_note"] or "")


# ---------------- 03 transcrição ----------------
def transcricao(task, out, error):
    if task["stage"] == "ANALISE":
        return analise(task, out, error)
    a = Audit(task, "Transcrever com timestamps e separar as vozes de todos os participantes.")
    if _errors(a, error):
        return a.report("falhou")
    t = json.loads(Path(out["files"][0]).read_text("utf-8"))
    u = t["utterances"]
    a.ok(bool(u), f"{len(u)} falas", "transcrição vazia", "verificar áudio")
    a.ok(all(x["start"] <= x["end"] for x in u) and all(p["start"] <= q["start"] for p, q in zip(u, u[1:])),
         "timestamps em ordem", "timestamps fora de ordem")
    a.ok(all(x["end"] <= t["duration"] + 1 for x in u), "timestamps dentro da duração", "timestamp além do vídeo")
    a.ok(t.get("diarization_turns", 0) > 0, f"separação de vozes executada ({t['diarization_turns']} turnos)",
         "separação de vozes não executada — etapa obrigatória", "repetir diarização")
    unk = sum(1 for x in u if x["speaker"] == "?")
    a.ok(unk <= max(1, len(u) * 0.05), "falas com locutor atribuído", f"{unk} falas sem locutor", soft=True)
    low = sum(x["low_conf"] for x in u)
    a.ok(low <= len(u) * 0.2, f"{low} falas de baixa confiança marcadas", f"{low} de {len(u)} falas com baixa confiança",
         "considerar modelo maior", soft=True)
    lp = t.get("language_prob")
    a.ok(lp is None or lp >= 0.6, f"idioma {t.get('language')} detectado (confiança {lp})",
         f"idioma {t.get('language')} detectado com baixa confiança ({lp})", "conferir o idioma falado", soft=True)
    return a.report(f"{len(u)} falas em '{t.get('language')}', vozes {list(t['speakers'])}, {low} trechos duvidosos")


def analise(task, out, error):
    a = Audit(task, "Identificar participantes com evidências, mapear assuntos e momentos.")
    if _errors(a, error):
        return a.report("falhou")
    res = out["llm"]
    tr = cuts.load_transcript(task["item_id"])
    by_id = {u["id"]: u["text"] for u in tr["utterances"]}
    v = db.get("videos", task["item_id"])
    meta = _norm(f"{v['title']} {v['channel']} {(db.get('opportunities', v['opportunity_id']) or {}).get('summary', '')}")
    labels = set(tr["speakers"]) - {"?"}
    got = {p["rotulo"] for p in res["participantes"]}
    a.ok(labels <= got, "todas as vozes analisadas", f"vozes sem análise: {sorted(labels - got)}",
         "analisar todas as vozes")
    merges = {p["rotulo"]: p.get("mesmo_que") for p in res["participantes"] if p.get("mesmo_que")}
    for p in res["participantes"]:
        target = merges.get(p["rotulo"])
        if target:  # separação de vozes dividiu a pessoa: valida a unificação em vez da evidência nominal
            a.ok(target in labels and target != p["rotulo"] and target not in merges,
                 f"{p['rotulo']} unificado com {target} (separação de vozes dividiu a pessoa)",
                 f"unificação inválida de {p['rotulo']} com {target}", "unificar só com um rótulo principal existente")
            a.ok(bool(p["fonte_evidencia"]) and bool(p["evidencia_ids"]), "", f"unificação de {p['rotulo']} sem justificativa",
                 "explicar e citar as falas")
            a.warn.append(f"vozes {p['rotulo']}→{target} unificadas; conferir na prévia")
            continue
        if p["nome"]:
            first = _norm(p["nome"]).split()[0]
            cited = [by_id.get(e) for e in p["evidencia_ids"]]
            word = re.compile(r"\b" + re.escape(first) + r"\b")
            in_tr = any(c and word.search(_norm(c)) for c in cited)
            a.ok(in_tr or bool(word.search(meta)), f"{p['rotulo']}={p['nome']} sustentado por evidência",
                 f"nome {p['nome']} para {p['rotulo']} sem evidência verificável",
                 "manter como rótulo genérico ou citar a fala/metadado que comprova")
        else:
            a.ok(bool(p["tentativas"]), f"{p['rotulo']} não identificado, tentativas documentadas",
                 f"{p['rotulo']} sem nome e sem tentativas documentadas", "documentar tentativas")
    a.ok(bool(res["assuntos"]), f"{len(res['assuntos'])} assuntos mapeados", "sem mapa de assuntos")
    return a.report(f"{len(res['participantes'])} participantes, {len(res['assuntos'])} assuntos, "
                    f"{len(res['momentos'])} momentos")


# ---------------- 04 editorial ----------------
def editorial(task, out, error):
    cfg = config.load()["cortes"]
    if task["stage"] == "PREVIA":
        a = Audit(task, "Gerar prévias leves e fiéis aos pontos propostos, sem finalização.")
        if _errors(a, error):
            return a.report("falhou")
        for cid in out["cuts"]:
            c = db.get("cuts", cid)
            if _file(a, c["preview_path"], f"prévia {cid}"):
                d = media.probe(c["preview_path"])["duration"]
                a.ok(abs(d - cuts.total(c["scores"]["intervalos"])) <= 0.35, f"{cid}: {d:.1f}s confere",
                     f"{cid}: duração da prévia diverge", "regerar prévia")
        return a.report(f"{len(out['cuts'])} prévias")
    a = Audit(task, "Selecionar cortes independentes, com começo, desenvolvimento e fim, sem repetição.")
    if _errors(a, error):
        return a.report("falhou")
    sel = out["llm"]["cortes"]
    created = [db.get("cuts", c) for c in out["cuts"]]
    fora = out.get("fora_da_duracao", [])
    a.ok(len(created) + len(fora) == len(sel), f"{len(created)} candidatos materializados",
         f"{len(sel) - len(created) - len(fora)} candidatos com ids de fala inválidos", "corrigir ids")
    if fora:
        a.warn.append("descartados por duração fora do alvo: " + "; ".join(fora))
    a.ok(len(created) <= cfg["max_por_video"], "quantidade dentro do limite", "excesso de cortes", soft=True)
    for c in created:
        d = cuts.total(c["scores"]["intervalos"])
        a.ok(cfg["duracao_min"] <= d <= cfg["duracao_max"], f"{c['id']}: {d:.0f}s",
             f"{c['id']}: duração {d:.0f}s fora de {cfg['duracao_min']}-{cfg['duracao_max']}s", soft=True)
    for x, y in [(x, y) for i, x in enumerate(created) for y in created[i + 1:]]:
        ov = min(x["end"], y["end"]) - max(x["start"], y["start"])
        a.ok(ov <= 0.3 * min(x["end"] - x["start"], y["end"] - y["start"]), "sem sobreposição relevante",
             f"{x['id']} e {y['id']} praticamente iguais", "manter apenas o melhor", soft=True)
    rev = out.get("revisao_ia")
    if rev:
        for r in rev["avaliacoes"]:
            a.ok(r["veredito"] != RP, f"revisão editorial: {r['corte']} ok",
                 f"revisão editorial reprovou {r['corte']}: {r['motivo']}", "descartar ou ajustar limites", soft=True)
    return a.report(f"{len(created)} candidatos; {out['llm'].get('observacoes', '')[:200]}")


# ---------------- 05 legendas ----------------
def legendas(task, out, error):
    tr = out and out.get("traduzido")
    a = Audit(task, "Legendas fiéis à fala" + (f", traduzidas de '{out['idioma_original']}' para português" if tr else "")
              + ", sincronizadas, legíveis e sem sobreposição.")
    if _errors(a, error):
        return a.report("falhou")
    base, edited, final = out["base"], out["blocks"], out["final"]
    a.ok(len(edited) == len(base) and all(abs(b["start"] - e["start"]) < 1e-6 and abs(b["end"] - e["end"]) < 1e-6
                                          for b, e in zip(base, edited)),
         "revisão/tradução manteve blocos e tempos", "revisão/tradução alterou blocos ou tempos")
    # a divisão de blocos longos preserva o início e o fim de cada bloco sincronizado
    for b in base:
        parts = [x for x in final if x["src_i"] == b["i"]]
        a.ok(bool(parts) and abs(parts[0]["start"] - b["start"]) < 0.002 and abs(parts[-1]["end"] - b["end"]) < 0.002,
             "", f"bloco {b['i']}: divisão alterou a sincronia", "regerar legendas")
    try:  # confere o ARQUIVO gerado contra o que foi planejado
        written = subtitles.parse_srt(Path(out["srt"]))
        a.ok(len(written) == len(final) and all(abs(f["start"] - w["start"]) < 0.002 and abs(f["end"] - w["end"]) < 0.002
                                                for f, w in zip(final, written)),
             f"{len(written)} blocos no SRT, tempos conferem", "SRT diverge dos blocos sincronizados")
        a.ok(all(x["end"] <= y["start"] for x, y in zip(written, written[1:])), "sem sobreposição", "legendas sobrepostas")
        a.ok(all(len(x["lines"]) <= 2 for x in written), "no máximo 2 linhas por bloco", "bloco com mais de 2 linhas")
    except ValueError as e:
        a.prob.append(f"SRT inválido: {e}")
    if tr:
        a.ok(not out["sem_traducao"], "todos os blocos traduzidos",
             f"blocos sem tradução: {out['sem_traducao']}", "traduzir todos os blocos")
        for b, e in zip(base, edited):
            ratio = len(e["text"]) / max(1, len(b["text"]))
            a.ok(0.3 <= ratio <= 2.6, "", f"bloco {b['i']}: tamanho da tradução suspeito ({ratio:.1f}× o original) — "
                 "possível omissão ou acréscimo", "traduzir só o que é dito no bloco")
            if len(b["text"].split()) >= 4:
                a.ok(_norm(b["text"]) != _norm(e["text"]), "", f"bloco {b['i']}: parece não traduzido", "traduzir", soft=True)
        rev = out.get("revisao_traducao")
        a.ok(bool(rev), "auditoria de tradução executada", "tradução sem auditoria de sentido")
        for r in (rev or {}).get("avaliacoes", []):
            a.ok(r["veredito"] == "OK", "", f"tradução do bloco {r['i']}: {r['motivo']}", "corrigir a tradução desse bloco")
    else:
        for b, e in zip(base, edited):
            sim = difflib.SequenceMatcher(None, _norm(b["text"]), _norm(e["text"])).ratio()
            a.ok(sim >= 0.6, "", f"bloco {b['i']}: texto mudou demais ({sim:.2f}) — risco de invenção",
                 "restaurar o texto reconhecido")
    for e in edited:
        if e.get("destaque"):
            a.ok(_norm(e["destaque"]) in _norm(e["text"]), "", f"bloco {e['i']}: destaque não está no texto", soft=True)
    if out.get("incertezas"):
        a.warn.append("incertezas registradas: " + "; ".join(out["incertezas"][:5]))
    return a.report(f"{len(final)} legendas" + (f", traduzidas de '{out['idioma_original']}'" if tr else "")
                    + f"; revisão IA: {'sim' if out.get('revisado') else 'não'}")


# ---------------- 06 audiovisual ----------------
def audiovisual(task, out, error):
    a = Audit(task, "Renderizar o corte aprovado no formato configurado, com legendas e áudio tratado.")
    if _errors(a, error):
        return a.report("falhou")
    if _file(a, out["video"], "vídeo final"):
        p = media.probe(out["video"])
        a.ok(abs(p["duration"] - out["expected_duration"]) <= 0.35, f"duração {p['duration']:.2f}s confere",
             "duração diverge do corte aprovado", "renderizar novamente")
        a.ok((p["video"]["width"], p["video"]["height"]) == (out["width"], out["height"]),
             f"{out['width']}x{out['height']}", "resolução incorreta")
        a.ok(bool(p["audio"]), "áudio presente", "sem áudio")
    return a.report(f"renderizou {out['video']}")


# ---------------- 07 conformidade ----------------
def conformidade(task, out, error):
    a = Audit(task, "Auditar o arquivo real com testes técnicos completos.")
    if _errors(a, error):
        return a.report("falhou")
    q = out["qc"]
    missing = [t for t in qc.REQUIRED_TESTS if t not in q["testes"]]
    a.ok(not missing, "todos os testes obrigatórios executados", f"testes não executados: {missing}",
         "executar testes faltantes")
    grave = [k for k, v in q["testes"].items() if not v["ok"] and v["grave"]]
    a.ok((q["status"] == RP) == bool(grave), f"veredito {q['status']} coerente com os testes",
         "veredito incoerente com os resultados")
    if q["status"] == RP:
        a.prob.append("controle de qualidade reprovou: " + ", ".join(f"{k} ({q['testes'][k]['detalhe']})" for k in grave))
        a.fix.append("reenviar à finalização")
    for k in q.get("ressalvas", []):
        a.warn.append(f"{k}: {q['testes'][k]['detalhe']}")
    return a.report(f"QC {q['status']}")


# ---------------- 08 textual ----------------
def textual(task, out, error):
    a = Audit(task, "Título, descrição e hashtags fiéis ao vídeo final, sem clickbait enganoso.")
    if _errors(a, error):
        return a.report("falhou")
    t = out["llm"]
    cut = db.get("cuts", task["item_id"])
    srt = config.DIRS["intermediarios"] / "final" / cut["id"] / "legenda.srt"
    legenda = srt.read_text("utf-8") if srt.exists() else ""  # inclui a tradução, se houver
    corpus = _norm(cut["transcript"] + " " + legenda + " " + json.dumps(cut["participants"], ensure_ascii=False))
    a.ok(0 < len(t["titulo"]) <= 100, f"título com {len(t['titulo'])} caracteres", "título vazio ou longo demais")
    dup = db.query("SELECT cut_id FROM exports WHERE lower(title)=lower(?) AND cut_id<>?", (t["titulo"], cut["id"]))
    a.ok(not dup, "título inédito", "título repetido de outro corte", "gerar alternativa")
    for num in re.findall(r"\d+", t["titulo"]):
        a.ok(num in corpus, "", f"número {num} do título não aparece no conteúdo", "remover número não confirmado")
    for name in re.findall(r"(?<!^)(?<![.!?] )\b[A-ZÁÉÍÓÚÂÊÔÃÕ][a-záéíóúâêôãõç]{2,}", t["titulo"]):
        a.ok(_norm(name) in corpus, "", f"nome próprio '{name}' não aparece no conteúdo", "remover nome",
             soft=True)
    a.ok(3 <= len(t["hashtags"]) <= 8 and all(h.startswith("#") for h in t["hashtags"]),
         f"{len(t['hashtags'])} hashtags", "hashtags fora do padrão (3–8, com #)", soft=True)
    v = db.get("videos", cut["video_id"])
    o = db.get("opportunities", v["opportunity_id"]) or {}
    if "creative_commons" in str((o.get("availability") or {}).get("authorization")):
        a.ok(_norm(v["channel"] or "")[:6] in _norm(t["descricao"]), "atribuição Creative Commons presente",
             "descrição sem atribuição obrigatória da licença CC", "incluir crédito ao autor e link")
    return a.report(f"título: {t['titulo']}")


# ---------------- 09 publicação ----------------
def publicacao(task, out, error):
    a = Audit(task, "Organizar arquivos completos e publicar só por integração oficial, sem duplicar.")
    if _errors(a, error):
        return a.report("falhou")
    if task["stage"] == "ORGANIZACAO":
        folder = Path(out["folder"])
        for f in ("video.mp4", "legenda.srt", "titulo.txt", "descricao.txt", "metadata.json"):
            _file(a, folder / f, f)
        m = json.loads((folder / "metadata.json").read_text("utf-8"))
        a.ok(all(k in m for k in ("data_producao", "data_exportacao", "publicacao_programada")),
             "datas de produção, exportação e publicação distintas no metadado", "metadado sem datas separadas")
        return a.report(f"organizado em {folder}")
    pub = db.get("publications", out["publication_id"])
    if pub["state"] == "PUBLICADO":
        a.ok(bool(pub["external_id"] or pub["url"]), f"publicado: {pub['url'] or pub['external_id']}",
             "marcado como publicado sem identificador da plataforma", "não considerar publicado")
    else:
        a.evid.append(f"estado {pub['state']}: {pub['error'] or 'arquivo pronto; nenhuma publicação alegada'}")
    n = db.query("SELECT COUNT(*) n FROM publications WHERE export_id=? AND platform=? AND state='PUBLICADO'",
                 (pub["export_id"], pub["platform"]))[0]["n"]
    a.ok(n <= 1, "sem publicação duplicada", "publicação duplicada detectada")
    return a.report(f"publicação: {pub['state']}")


# ---------------- 11/12 Vídeos de Página ----------------
def pg_busca(task, out, error):
    from . import pagina
    a = Audit(task, "Achar vídeos curtos de humor reais, virais e inéditos na página, com 3 piadas cada.")
    if _errors(a, error):
        return pagina._supervise(a, task, out, error, "falhou")
    esc = out["escolhidos"]
    a.ok(bool(esc), f"{len(esc)} escolhidos de {out['candidatos']} candidatos reais", "nenhum vídeo escolhido",
         "rodar a busca de novo com outras consultas")
    a.ok(len(esc) >= out["quantidade"], "quantidade pedida atingida",
         f"pediu {out['quantidade']}, vieram {len(esc)}", soft=True)
    a.ok(not out["fora_da_lista"], "todas as URLs vieram da coleta real",
         f"agente citou URLs fora da lista (ignoradas): {out['fora_da_lista']}", soft=True)
    c = config.load()["pagina"]
    for x in esc:
        d = x.get("duracao_s")
        a.ok(d is None or c["duracao_min_s"] <= d <= c["duracao_max_s"], f"{x['url']}: {d if d is not None else 'duração conferida no download'}",
             f"{x['url']}: duração fora da faixa")
        a.ok((x.get("visualizacoes") or 0) >= c["min_visualizacoes"], f"{x['url']}: {x.get('visualizacoes')} visualizações",
             f"{x['url']}: poucas visualizações")
        a.ok(len([p for p in x["piadas"] if p.strip()]) >= 2, f"{x['url']}: {len(x['piadas'])} piadas",
             f"{x['url']}: menos de 2 piadas", soft=True)
        a.ok(not x["riscos"], f"{x['url']}: sem riscos apontados", f"{x['url']}: riscos {x['riscos']}", soft=True)
        flag = x.get("parece_ia") or x.get("repost") == "muito" or pagina.canal_restrito(x, c)
        a.ok(not pagina.barrado_ia_repost(x, x.get("visualizacoes"), c, x.get("engajamento_por_mil"), out.get("engaj_topo")),
             f"{x['url']}: " + ("IA/muito repostado, liberado: muito viral e no topo do engajamento" if flag else
                                f"original/pouco repostado ({x.get('repost') or '?'}), sem IA"),
             f"{x['url']}: IA/muito repostado sem viralização suficiente")
    a.ok(True, f"{len(out.get('barrados_ia_repost') or [])} barrados pelas regras IA/repost/gringo", "")
    nt = out.get("notas") or {}
    if nt:
        a.ok(True, f"notas do lote: mediana {nt.get('mediana_por_mil')}/mil, topo {nt.get('topo_por_mil')}/mil, "
                   f"gringo só com {nt.get('gringo_min_por_mil')}/mil", "")
        for x in esc:
            if x.get("engajamento_por_mil") is not None:
                a.ok(not pagina.eh_gringo(x) or x["engajamento_por_mil"] >= nt["gringo_min_por_mil"],
                     f"{x['url']}: " + ("gringo bem acima da mediana" if pagina.eh_gringo(x) else
                                        f"origem {x.get('origem')}"), f"{x['url']}: gringo sem engajamento suficiente")
        br = sum(not pagina.eh_gringo(x) for x in esc)
        a.ok(br >= (len(esc) + 1) // 2, f"{br} de {len(esc)} escolhidos brasileiros/incertos",
             f"só {br} de {len(esc)} escolhidos são brasileiros", soft=True)
    sem = [x["url"] for x in esc if x.get("engajamento_por_mil") is None]
    a.ok(not sem, "todos os escolhidos têm comentários/curtidas medidos",
         f"escolhidos sem dados de engajamento: {sem}", soft=True)
    if c.get("min_comentarios"):
        for x in esc:
            if x.get("comentarios") is not None:
                a.ok(x["comentarios"] >= c["min_comentarios"], f"{x['url']}: {x['comentarios']} comentários",
                     f"{x['url']}: abaixo do mínimo de comentários")
    return pagina._supervise(a, task, out, error, f"escolheu {len(esc)} vídeos")


def pg_download(task, out, error):
    from . import pagina
    a = Audit(task, "Baixar o vídeo inteiro, com imagem e áudio, sem repetir um já usado.")
    if _errors(a, error):
        return pagina._supervise(a, task, out, error, "falhou")
    if _file(a, out["arquivo"], "vídeo-fonte"):
        a.ok(bool(out["probe"]["video"]), "trilha de vídeo presente", "sem vídeo")
        a.ok(bool(out["probe"]["audio"]), "trilha de áudio presente", "sem áudio (vídeo de humor sem som)", soft=True)
        a.ok(out["bytes"] < 150 * (1 << 20), f"{out['bytes'] / (1 << 20):.1f} MB", "arquivo grande demais")
        dup = db.query("SELECT id FROM page_videos WHERE id<>? AND meta LIKE ?", (task["item_id"], f"%{out['sha256']}%"))
        a.ok(not dup, "conteúdo inédito na página (impressão digital)", f"mesmo arquivo já usado em {dup}")
        pv = db.get("page_videos", task["item_id"])
        a.ok(abs(out["duracao"] - (pv["duration"] or out["duracao"])) <= 2, f"duração {out['duracao']:.1f}s confere com a busca",
             "duração diferente da anunciada na busca", soft=True)
        a.ok(not out.get("rejeitado"), "duração dentro da faixa da página", f"descartado: {out.get('rejeitado')}", soft=True)
    return pagina._supervise(a, task, out, error, f"baixou {out and out.get('arquivo')}")


def pg_montagem(task, out, error):
    from . import pagina
    a = Audit(task, "Escrever a frase e montar o vídeo no modelo de página, com crédito ao criador.")
    if _errors(a, error):
        return pagina._supervise(a, task, out, error, "falhou")
    r = out["llm"]
    a.ok(len(out.get("quadros") or []) >= 3, f"{len(out.get('quadros') or [])} quadros analisados pelo montador",
         "montador viu poucos quadros do vídeo", soft=True)
    if not r["seguro"]:
        a.ok(False, "", f"montador recusou o vídeo: {r['motivo_inseguro']}", soft=True)
        return pagina._supervise(a, task, out, error, "recusou o vídeo (descartado)")
    leg = r["legenda_tela"]
    a.ok(0 < len(leg) <= 110, f"piada com {len(leg)} caracteres", "piada vazia ou longa demais")
    a.ok(pagina.clean(leg) == leg.strip(), "piada sem emoji (a fonte do modelo não tem)", "piada tinha emoji (removido)", soft=True)
    if _file(a, out["video"], "vídeo montado"):
        p = media.probe(out["video"])
        a.ok((p["video"]["width"], p["video"]["height"]) == (1080, 1920), "1080x1920", "resolução errada")
    a.ok(out["creditos"] in out["descricao"], "crédito ao criador na descrição", "sem crédito ao criador")
    a.ok(3 <= len(r["hashtags"]) <= 8, f"{len(r['hashtags'])} hashtags", "quantidade de hashtags fora de 3–8", soft=True)
    a.ok(len(out["render"]["linhas"]) <= 3, f"texto em {len(out['render']['linhas'])} linha(s), fonte {out['render']['fonte_px']}px",
         "texto passou de 3 linhas")
    return pagina._supervise(a, task, out, error, f"montou {out.get('video')}")


def pg_qc(task, out, error):
    from . import pagina
    a = Audit(task, "Conferir o arquivo final: resolução, duração, áudio, decodificação e capa.")
    if _errors(a, error):
        return pagina._supervise(a, task, out, error, "falhou")
    for k, v in out["testes"].items():
        a.ok(v, f"{k}: ok", f"{k}: falhou", "montar de novo")
    return pagina._supervise(a, task, out, error, f"testou {len(out['testes'])} itens")


REGISTRY = {  # etapa → (agente operacional, supervisor, função de auditoria)
    "PG_BUSCA": ("11-cacador", "sup-11-cacador", pg_busca),
    "PG_DOWNLOAD": ("11-cacador", "sup-11-cacador", pg_download),
    "PG_MONTAGEM": ("12-montador", "sup-12-montagem", pg_montagem),
    "PG_QC": ("12-montador", "sup-12-montagem", pg_qc),
    "PESQUISA": ("01-pesquisador", "sup-01-pesquisa", pesquisa),
    "VERIFICACAO": ("02-localizador", "sup-02-aquisicao", aquisicao),
    "AQUISICAO": ("02-localizador", "sup-02-aquisicao", aquisicao),
    "COMPRESSAO": ("02-localizador", "sup-02-aquisicao", aquisicao),
    "TRANSCRICAO": ("03-transcritor", "sup-03-transcricao", transcricao),
    "ANALISE": ("03-transcritor", "sup-03-transcricao", transcricao),
    "SELECAO": ("04-editor", "sup-04-editorial", editorial),
    "PREVIA": ("04-editor", "sup-04-editorial", editorial),
    "LEGENDAS": ("05-legendas", "sup-05-legendas", legendas),
    "FINALIZACAO": ("06-finalizador", "sup-06-audiovisual", audiovisual),
    "QC": ("07-qualidade", "sup-07-conformidade", conformidade),
    "TEXTOS": ("08-estrategista", "sup-08-textual", textual),
    "ORGANIZACAO": ("09-distribuidor", "sup-09-publicacao", publicacao),
    "PUBLICACAO": ("09-distribuidor", "sup-09-publicacao", publicacao),
}


def audit(task: dict, out: dict | None, error: str | None) -> dict:
    agent, sup, fn = REGISTRY[task["stage"]]
    try:
        rep = fn(task, out, error)
    except Exception as e:  # noqa: BLE001 — falha na própria auditoria nunca libera a etapa
        rep = {"tarefa": task["id"], "etapa": task["stage"], "item": task["item_id"], "deveria": "",
               "fez": "", "correto": False, "evidencias": [], "problemas": [f"auditoria não concluída: {e}"],
               "ressalvas": [], "correcoes": ["investigar saída do agente"], "status": RP, "pode_avancar": False}
    rep.update({"agente": agent, "supervisor": sup})
    db.insert("reports", {"task_id": task["id"], "stage": task["stage"], "agent": agent, "supervisor": sup,
                          "ts": config.iso(), "status": rep["status"], "body": rep})
    log.event(task_id=task["id"], item_id=task["item_id"], agent=agent, supervisor=sup,
              requested="auditoria", result=rep["status"], state=rep["status"])
    return rep


def render_text(rep: dict) -> str:
    """Formato do relatório para leitura humana / gerente-geral."""
    lines = [f"AGENTE: {rep['agente']}", f"SUPERVISOR: {rep['supervisor']}",
             f"TAREFA: {rep['etapa']} ({rep['item']}) — {rep['deveria']}", f"RESULTADO: {rep['fez']}"]
    if rep["evidencias"]:
        lines.append("EVIDÊNCIA: " + "; ".join(e for e in rep["evidencias"] if e)[:600])
    if rep["problemas"]:
        lines.append("PROBLEMA: " + "; ".join(rep["problemas"]))
    if rep["ressalvas"]:
        lines.append("RESSALVAS: " + "; ".join(rep["ressalvas"]))
    if rep["correcoes"]:
        lines.append("RECOMENDAÇÃO: " + "; ".join(dict.fromkeys(rep["correcoes"])))
    lines.append(f"STATUS: {rep['status'].replace('_', ' ')}. Pode avançar: {'sim' if rep['pode_avancar'] else 'não'}.")
    return "\n".join(lines)
