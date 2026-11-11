"""Uso: python -m cortes <comando>

  iniciar [--porta 8770]   interface + gerente trabalhando em segundo plano
  trabalhar [--uma]        só o gerente (sem interface); --uma executa uma tarefa e sai
  retencao                 varredura de retenção de 14 dias (idempotente)
  status                   resumo das filas, tarefas e armazenamento
  relatorios [N]           últimos N relatórios dos supervisores
  modelos                  baixa (uma vez) os modelos locais de transcrição/diarização
  auditar-recomendacao     verifica bolha/concentração do recomendador
  nuvem-senha              guarda a senha do rclone.conf protegida pelo Windows
  nuvem-testar             confere se a nuvem configurada responde
"""
from __future__ import annotations

import json
import sys

from . import config, db, orchestrator, storage, supervisors


def status() -> None:
    def grp(sql):
        return {r["k"]: r["n"] for r in db.query(sql)}
    print("Oportunidades:", grp("SELECT state k, COUNT(*) n FROM opportunities GROUP BY state"))
    print("Cortes:       ", grp("SELECT state k, COUNT(*) n FROM cuts GROUP BY state"))
    print("Tarefas:      ", grp("SELECT stage||':'||state k, COUNT(*) n FROM tasks GROUP BY k"))
    s = storage.storage_overview()
    print(f"Originais ocupando {s['ocupado_originais'] >> 20} MB; economia {s['economia_compressao'] >> 20} MB; "
          f"disco livre {s['disco_livre_mb'] / 1024:.1f} GB")


def main(argv: list[str]) -> int:
    config.ensure_dirs()
    cmd = argv[0] if argv else "status"
    if cmd in ("iniciar", "trabalhar"):
        from . import media
        media.kill_children_on_exit()  # fechar a janela encerra tudo: nada roda fora do .bat
    if cmd == "iniciar":
        from . import web
        port = int(argv[argv.index("--porta") + 1]) if "--porta" in argv else 8770
        web.serve(port)
    elif cmd == "trabalhar":
        if "--uma" in argv:
            if not orchestrator.acquire_lock():
                print("Outro gerente já está ativo; nada foi executado.")
                return 1
            orchestrator.recover()
            rep = orchestrator.work_once()
            print(supervisors.render_text(rep) if rep else "Nenhuma tarefa pronta.")
        else:
            orchestrator.loop()
    elif cmd == "retencao":
        print(json.dumps(storage.retention_sweep(), ensure_ascii=False))
    elif cmd == "status":
        status()
    elif cmd == "relatorios":
        n = int(argv[1]) if len(argv) > 1 else 10
        for r in db.query("SELECT body FROM reports ORDER BY id DESC LIMIT ?", (n,)):
            print(supervisors.render_text(r["body"]), "\n")
    elif cmd == "modelos":
        from . import transcribe
        print(transcribe.ensure_models())
        from faster_whisper import WhisperModel
        WhisperModel(config.load()["transcricao"]["modelo"], device="cpu", compute_type="int8",
                     download_root=str(transcribe.MODELS / "whisper"))
        print("modelo whisper pronto")
    elif cmd == "nuvem-senha":  # guarda a senha do rclone.conf protegida pelo Windows (DPAPI)
        import getpass
        from . import nuvem
        nuvem.save_config_password(getpass.getpass("Senha da configuração do rclone (não aparece na tela): "))
        print("Senha guardada com a proteção do Windows.")
    elif cmd == "nuvem-testar":
        from . import nuvem
        print(nuvem.test())
    elif cmd == "auditar-recomendacao":
        from . import audit_rec
        print(json.dumps(audit_rec.run(), ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
