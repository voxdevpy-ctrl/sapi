# CLAUDE.md — instruções do projeto SAPI

**Regra absoluta:** escreva tudo em **português do Brasil**: respostas, código, comentários, mensagens de commit,
textos da interface e documentação.

## O que é
SAPI (Sistema Autônomo de Produção para a Internet) é uma linha de produção local de cortes de vídeo.
- **Etapas:** pesquisa → verificação → aquisição → compressão → transcrição → análise → seleção → prévia →
  legendas → finalização → QC → textos → organização/publicação.
- **Aprovações humanas:** duas, os assuntos e os cortes, cada uma com 5 botões. Cada etapa tem um supervisor.

## Estrutura
- `cortes/` — pacote Python (stdlib + faster-whisper, sherpa-onnx, yt-dlp).
  - `orchestrator.py` tem a máquina de tarefas (tabela `tasks`, `ORDER`, `HANDLERS`, `on_success`/`on_failure`).
  - `supervisors.py` tem as auditorias determinísticas (`REGISTRY`).
  - `llm.py` chama os agentes com `claude -p --agent X --json-schema`.
  - `web.py` é o servidor HTTP da Central (127.0.0.1:8770).
  - `db.py` cuida do SQLite WAL e das migrações; `config.py` dos caminhos e configurações padrão.
  - `cota.py` é o limite de armazenamento; `pagina.py`, os Vídeos de Página; `remote.py`, o celular/PIN.
- `cortes/ui/` — a interface estática: `index.html`, `app.js`, `style.css` e `gate.js` (a entrada SAPI).
- `.claude/agents/` — os agentes de produção e os supervisores.
- `tests/` — unittest. A pasta de dados é temporária.
- Dados do usuário: `PRODUCAO/` ou `SAPI_DADOS`. **Nunca** vão para o git.

## Comandos
- Instalar: `instalar.bat`. Iniciar: `iniciar.bat`.
- Testes (sempre com a lista de módulos; `discover` quebra o isolamento):
  `.venv\Scripts\python -m unittest tests.test_ajustes tests.test_armazenamento tests.test_cota tests.test_decisoes tests.test_pagina tests.test_postagem tests.test_producao tests.test_recomendacao tests.test_remoto tests.test_revisao tests.test_ritmo tests.test_traducao`
- Utilitários: `.venv\Scripts\python -m cortes status | relatorios 20 | retencao | modelos`.

## Regras
- **Segurança:** nunca leia, imprima ou registre credenciais (as do Claude Code, o PIN, cookies, rclone).
  Nunca crie chave de API nem cobrança. Nunca use `--dangerously-skip-permissions`.
- **Dados:**
  - nunca apague cortes aprovados, vídeos finais, metadados, relatórios ou histórico, exceto pelas regras já
    implementadas (retenção de 14 dias dos originais; vídeo final no dia seguinte ao marcado nas duas redes);
  - exclusões pedidas na hora vão para a Lixeira e ficam registradas em `retention_log`.
- **Banco:** só migrações aditivas e idempotentes. Teste numa cópia antes de mexer num banco real.
- **Subprocessos:** sempre com lista de argumentos, sem shell.
- **Interface:**
  - sem build, sem bibliotecas, sem CDN;
  - textos sempre passando por `esc()`;
  - só um vídeo tocando por vez (objeto `Players`);
  - animações só com `transform`/`opacity`, paradas quando não há produção, e respeitando
    `prefers-reduced-motion`;
  - a entrada SAPI é só visual: o login de verdade é o PIN (`#login`).
- **Hardware alvo:** i3, 8 GB de RAM, sem GPU. Nada pesado.
- **Destrutivo:** nada de `git push --force`, reescrever histórico ou apagar arquivos de dados sem confirmação.

## Contribuindo com o Claude Code
Habilidades privadas do dono (por exemplo, `llm-council`) são opcionais e não fazem parte do repositório.
Se você não as tiver, siga este arquivo e o CONTRIBUTING.md.
