"""Teste ponta a ponta REAL (usa IA via assinatura do Claude Code e modelos locais) em ambiente isolado.

Uso: python tests/e2e_local.py <video_de_teste.mp4> <pasta_producao_temporaria>
As decisões humanas são simuladas chamando a mesma função usada pelos cinco botões da interface.
Nada é publicado (adaptador 'manual').
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

video, prod = Path(sys.argv[1]), Path(sys.argv[2])
if prod.exists():
    shutil.rmtree(prod)
os.environ["CORTES_PRODUCAO"] = str(prod)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cortes import config, db, decisions, orchestrator, supervisors  # noqa: E402

config.ensure_dirs()
cfg = config.load()
cfg["janela_desfazer_s"] = 0
cfg["pesquisa"].update({"tendencias": False, "consultas": [], "max_oportunidades": 3, "duracao_min_fonte_s": 60})
cfg["cortes"].update({"duracao_min": 15, "max_por_video": 3})
(config.DIRS["config"] / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
shutil.copy(video, config.DIRS["entrada"] / video.name)


def drain(label):
    t0 = time.time()
    while True:
        rep = orchestrator.work_once(research=False)
        if rep is None:
            waiting = db.query("SELECT 1 FROM tasks WHERE state IN ('PENDENTE','FALHA','PENDENTE_AUDITORIA') "
                               "AND not_before IS NOT NULL")
            if waiting and time.time() - t0 < 1800:  # nova tentativa agendada pelo gerente: aguarda como o real
                time.sleep(10)
                continue
            break
        print(f"[{time.time() - t0:6.1f}s] {rep['etapa']:<12} {rep['status']:<24} {rep['fez'][:90]}")
        if rep["problemas"]:
            print("            problemas:", rep["problemas"][:3])
    print(f"--- {label} concluído em {time.time() - t0:.0f}s")


orchestrator.enqueue("PESQUISA", "batch", "pesquisa_e2e")
drain("pesquisa + verificação")
opps = db.query("SELECT * FROM opportunities WHERE state='AGUARDANDO_APROVACAO'")
print("Oportunidades na fila de aprovação:", [(o["id"], o["topic"]) for o in opps])
assert opps, "nenhuma oportunidade verificada"
decisions.decide("opportunity", opps[0]["id"], 100, None)  # botão MUITO APROVADO
drain("aquisição → prévias")

cuts = db.query("SELECT * FROM cuts WHERE state='AGUARDANDO_APROVACAO' ORDER BY score_total DESC")
print("Cortes na fila:", [(c["id"], c["title_tmp"], c["score_total"], c["alerts"]) for c in cuts])
assert cuts, "nenhum corte proposto"
plan = [100, 0, -50]
for c, s in zip(cuts, plan):
    decisions.decide("cut", c["id"], s, "Sem motivo informado" if s else "Corte repetitivo")
drain("legendas → publicação")

tr = db.get("transcripts", db.query("SELECT id FROM videos")[0]["id"], "video_id")
print("Participantes:", json.dumps(tr["participants"], ensure_ascii=False))
for e in db.query("SELECT * FROM exports"):
    print("Exportado:", e["folder"], "|", e["title"], "| QC", e["qc_status"])
    print("  arquivos:", sorted(p.name for p in Path(e["folder"]).iterdir()))
    print("  descrição:", Path(e["folder"], "descricao.txt").read_text("utf-8")[:400])
    print("  legenda final:\n" + Path(e["folder"], "legenda.srt").read_text("utf-8")[:1500])
print("Idioma detectado:", db.query("SELECT language FROM transcripts"))
print("Publicações:", db.query("SELECT state, error FROM publications"))
print("Estados dos cortes:", db.query("SELECT id, state FROM cuts"))
print("Tarefas não concluídas:", db.query("SELECT stage, state, error FROM tasks WHERE state NOT IN ('CONCLUIDO')"))
print("Vídeo:", {k: v for k, v in db.query("SELECT * FROM videos")[0].items() if k in
                 ("original_size", "compressed_size", "compression_note", "retention_until", "retention_state")})
