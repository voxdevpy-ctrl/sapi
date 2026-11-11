# SAPI — manual completo (antes "Central de Cortes")

Sistema local que pesquisa assuntos, verifica fontes, adquire, comprime, transcreve, identifica participantes,
seleciona cortes, legenda, finaliza, audita, escreve títulos e organiza para publicação.
Você participa em **dois pontos**: aprovar assuntos/fontes e aprovar cada corte (cinco botões).

## Como usar

1. Dê dois cliques em `iniciar.bat` (abre `http://127.0.0.1:8770` e liga o gerente da produção).
   **Nada roda fora disso**: sem tarefas agendadas e sem processo em segundo plano. Fechar a janela do
   `.bat` encerra tudo, inclusive FFmpeg e chamadas de IA em andamento (que retomam na próxima abertura).
2. Aba **Assuntos**: avalie cada fonte já verificada. Aba **Cortes**: assista à prévia e avalie.
   Teclas: `1` muito aprovado · `2` aprovado · `3` adiar · `4` desaprovado · `5` muito desaprovado ·
   `U` desfazer a última · `P` pausar. O motivo (chips acima dos botões) é opcional.
3. Aba **Prontos**: cortes finalizados para assistir, **baixar**, copiar título e descrição, compartilhar e
   marcar como postado (com o link, se quiser).
4. Aba **Painel**: produção, agentes e supervisores, relatórios, falhas, armazenamento, exclusões
   programadas, perfil editorial aprendido, adiados, rejeitados, histórico e configurações.

Uma decisão positiva só vira trabalho depois de 90 s (janela para desfazer). Adiar nunca autoriza nada.

Comandos (na pasta do projeto): `.venv\Scripts\python -m cortes status | relatorios 20 | retencao |
trabalhar --uma | auditar-recomendacao | modelos`.

## De onde vêm as fontes (e por que algumas nunca aparecem)

Só entram na fila fontes com **base de autorização** verificada antes da sua aprovação:
- `ENTRADA_LOCAL\` — vídeos que você coloca lá (seus ou que você tem direito de usar);
- canais próprios / canais com autorização registrada (`autorizacao.canais_proprios` e
  `autorizacao.canais_autorizados` = `{"ID_DO_CANAL": "como a autorização foi obtida"}`);
- vídeos com licença **Creative Commons** declarada pela plataforma (atribuição incluída na descrição).

Atenção: os Termos do YouTube restringem baixar vídeos fora dos recursos oficiais, mesmo com licença CC.
Para eliminar esse risco, use `"aceitar_creative_commons": false` e trabalhe só com material próprio,
autorizado por escrito, ou colocado em `ENTRADA_LOCAL`.

## Arquitetura (resumo)

| Nível | Quem | Como executa |
|---|---|---|
| 1 | Gerente-geral | `cortes/orchestrator.py` (regras determinísticas, uma tarefa por vez) + agente `gerente-geral` para julgamentos |
| 2 | 9 agentes operacionais | Python determinístico + `claude -p --agent 0X-...` onde há interpretação (01, 03, 04, 05, 08) |
| 3 | 9 supervisores individuais | `cortes/supervisors.py` (checagens de evidência, só leitura) + agente `sup-04-editorial` convocado por vídeo |

O Claude Code **não permite subagentes aninhados** e não mantém agentes rodando permanentemente.
Por isso o orquestrador convoca o supervisor **depois** de cada etapa, de forma independente do agente
auditado (o agente não pode alterar a auditoria). Todo relatório responde às 7 perguntas e fica em
`PRODUCAO\RELATORIOS\AAAA-MM-DD\` e no banco (`PRODUCAO\cortes.db`).

Fluxo de estados: `PESQUISA → VERIFICACAO → [você] → AQUISICAO → COMPRESSAO → TRANSCRICAO → ANALISE →
SELECAO → PREVIA → [você] → LEGENDAS → FINALIZACAO → QC → TEXTOS → ORGANIZACAO → PUBLICACAO`.
Falhou? O supervisor explica, o gerente repete com o retorno do supervisor (até 3 vezes) e então bloqueia.
Queda/reinício: tarefas interrompidas voltam à fila; publicação nunca é reenviada às cegas.

## Armazenamento e retenção

- Original → cópia H.264 (CRF 24, até 1080p). Só apaga o original após validar sondagem, decodificação
  completa, duração e SHA-256. Se a recompressão economizar < 15 %, mantém o original como cópia de trabalho.
- 14 dias corridos após a aquisição a cópia é excluída automaticamente (`python -m cortes retencao`,
  executado ao abrir o `.bat` e de hora em hora enquanto ele está aberto; se o PC ficou dias sem abrir,
  a primeira varredura põe tudo em dia).
- Se ainda houver etapa usando o vídeo, vira EXCEÇÃO e é limpo depois. Cortes pendentes/adiados ficam
  só com o **trecho** necessário. Nada fora de `PRODUCAO\ORIGINAIS\` é excluído.

## Aprendizado editorial (sem "treinar IA")

Cada decisão alimenta pesos por característica (nicho, canal, formato, tema; estilo, abertura, duração,
participantes) com meia-vida de 30 dias e suavização (uma avaliação isolada vale no máximo ±0,25).
A fila mistura **preferências conhecidas**, **exploração** de temas nunca avaliados e **segunda chance**
para temas rejeitados há tempo (rejeição forte reduz, nunca zera), com penalidade de repetição de canal/nicho.
Cortes rejeitados nunca voltam; a segunda chance vale para novas fontes do tema.

## Custos e privacidade

Tudo local e gratuito: FFmpeg, faster-whisper (modelo `small`), sherpa-onnx, yt-dlp, SQLite, Python.
A IA usa sua assinatura do Claude Code (`claude -p`); se `ANTHROPIC_API_KEY` existir no ambiente, ela é
removida da chamada para impedir cobrança por API. O servidor só escuta em 127.0.0.1.

## Segurança e robustez (após revisão técnica independente)

- **Um único gerente** por pasta de produção (trava de arquivo); tarefas são reservadas de forma atômica.
- **Retomada sem refazer**: etapa já executada só é reauditada; efeitos aprovados são reaplicados de forma
  idempotente; a aquisição e a compressão reaproveitam o que já existe; o original só é apagado
  **depois** que o supervisor aprova a cópia.
- **Cortes**: desfazer/rejeitar enquanto o gerente trabalha cancela a etapa; nada segue sem estado APROVADO.
- **IA sem acesso à web**: títulos e descrições de terceiros podem conter injeção de prompt; os agentes
  só leem arquivos e devolvem JSON validado por schema (`StructuredOutput`).
- **Servidor**: aceita só Host local (anti DNS rebinding), POST exige cabeçalho próprio, Range validado,
  configuração validada por tipo e gravada de forma atômica.

## Limitações conhecidas (honestas)

- **Publicação automática não está ligada**: o adaptador é `manual` (deixa tudo pronto em
  `PRODUZIDOS\AAAA-MM-DD\HH-MM\` e informa). YouTube Data API é gratuita (cota ~6 envios/dia), mas exige
  projeto no Google Cloud + OAuth e, sem auditoria do Google, vídeos enviados por API ficam **privados**.
  TikTok/Instagram exigem aprovação de app. Nenhuma integração foi criada sem sua autorização.
- **Arquivos da ENTRADA_LOCAL são movidos** para ORIGINAIS, recomprimidos e excluídos após 14 dias:
  guarde sua própria cópia se ela for a única.
- Separação de vozes testada com 2 vozes sintéticas; em conversas reais com sobreposição e eco pode
  errar — o supervisor marca e a identificação nominal exige evidência citável.
- Risadas/reações não são detectadas automaticamente (o Whisper às vezes transcreve; não é garantido).
- Erro de rede momentâneo na verificação descarta a fonte (não é reavaliada sozinha).
- Uma queda entre copiar o vídeo final e registrá-lo pode deixar uma cópia órfã em PRODUZIDOS (nunca apagada).
- No i3, comprimir+transcrever+separar vozes leva ~0,8× a duração do vídeo (medido: 158 s de vídeo →
  43 s compressão + 35 s transcrição + 42 s diarização), ou seja, ~45–60 min para 1 h de material.

## Idiomas e tradução

- O idioma de cada assunto aparece já na fila, com a origem: **estimado pela pesquisa** → **informado pela
  plataforma** → **detectado no áudio** (definitivo, pelo Whisper).
- Vídeos em outro idioma recebem legendas **traduzidas para português** (agente 05 em modo tradução) e
  uma auditoria independente de sentido por bloco (supervisor 05). Erros evidentes de reconhecimento
  (ex.: "Nazi" ouvido no lugar de "nurse") são corrigidos pelo contexto e registrados como incerteza.
- A legenda no idioma original fica guardada como `legenda_original.srt` na pasta do corte.
- Títulos, descrições, resumos e análises saem sempre em português.
- Se a separação de vozes dividir uma pessoa em dois rótulos, o analista unifica com justificativa
  (o supervisor confere) e a transcrição é remarcada.

## Celular (julgar e receber cortes de qualquer lugar)

Funciona **somente enquanto o `.bat` estiver aberto** no PC (é ele quem processa e serve os vídeos).

1. Instale o **Tailscale** (gratuito para uso pessoal) no PC e no celular e entre na mesma conta nos dois.
   Ele cria uma rede privada: a Central nunca fica exposta na internet.
2. No PC, aba **Painel → Acesso pelo celular**: defina um PIN (6–12 dígitos) e clique em
   **Ligar acesso remoto**. Feche e abra o `.bat` de novo. Se o Windows perguntar sobre o firewall,
   permita só em **Redes privadas**.
3. No celular, abra o endereço mostrado no painel (`http://100.x.x.x:8770`) e digite o PIN.
   Dica: "Adicionar à tela inicial" para abrir como um aplicativo.

Segurança: só aceita conexões da rede Tailscale (a rede Wi-Fi de casa é opcional); qualquer acesso que não
seja do próprio PC exige o PIN, guardado apenas como hash PBKDF2; 5 erros bloqueiam por 10 minutos;
configurações e PIN só podem ser mudados no próprio PC. O botão **Compartilhar** (enviar o vídeo direto
para o app da rede social) exige HTTPS — com HTTP, use **Baixar vídeo**.

## Acompanhar a produção

- A faixa no topo (todas as abas, inclusive no celular) mostra o que o sistema está fazendo agora, há quanto tempo e uma estimativa.
- A aba **Produção** mostra a etapa em andamento com barra de progresso estimada, as próximas etapas, cada vídeo e cada corte etapa por etapa (✓ feito · ● agora · ◌ na fila · ↻ repetindo · ⏸ interrompido · ✕ bloqueado) e a atividade recente. Atualiza sozinha a cada 5 s.
- Fechar o .bat no meio de uma etapa não conta como falha: ela recomeça ao abrir de novo (só 8 quedas seguidas na mesma etapa bloqueiam).


## Pedir assuntos pela Central
Na aba Assuntos, use **＋ Pedir assuntos novos**. Tema (nomes separados por vírgula), categoria, quantidade e
visualizações mínimas são todos opcionais. Com nomes de criadores, a pesquisa também lê as lives e os vídeos
do canal deles.

## Autorização das fontes
Por padrão, só entram fontes com base de autorização (veja acima). A opção `autorizacao.autorizar_qualquer_canal`
existe para quem tem autorização dos criadores: ligada, cada vídeo registra a base "autorizado_pelo_usuario".
**Ligue só se você realmente tiver essa autorização.**

## Ritmo dos cortes
O silêncio inútil entre as falas é removido, e fica só a pausa que o contexto pede. Os valores abaixo são quanto
de pausa **fica**, e dá para ajustar em `"ritmo"` no Painel:
- meio de frase (hesitação): 0,15 s
- fim de frase (`.`): 0,4 s
- **pausa dramática** (depois de `?`, `!`, `…` ou `:`): 0,85 s
- troca de locutor: 0,25 s; resposta a uma pergunta: 0,5 s

O silêncio é medido no próprio áudio, e cada emenda tem um fade de 12 ms, para não dar estalo. A legenda acompanha
os novos tempos, e nenhuma palavra some.

## Limite de armazenamento
Tudo o que está dentro da pasta de dados entra na conta: originais, intermediários, produzidos, banco e relatórios.
- **Ao chegar no limite (padrão 10 GB), a produção pausa.** Nada novo é baixado, renderizado ou finalizado.
  Pesquisa, análise e textos continuam, porque não gravam vídeo. A compressão também continua, porque libera espaço.
- **Tolerância (padrão 1 GB), só para terminar o que já estava rodando.** Cada etapa estima antes quanto vai gravar,
  e o download e a compressão são vigiados enquanto gravam. Se não couber, a etapa espera sem gastar tentativa.
- **Volta sozinha** quando o uso cai abaixo do limite.
- Ajuste em `"armazenamento": {"limite_gb": 10, "tolerancia_gb": 1}` no Painel.
- Lives acima de `pesquisa.duracao_max_fonte_s` (padrão 4 h) são ignoradas, para não pesar no PC.

## Postado TikTok / Postado Instagram
Na aba Prontos, cada rede tem o próprio botão. No dia seguinte às duas marcações, o vídeo final é apagado do PC;
títulos, descrições e legendas ficam. Baixar um vídeo marca "Vídeo Baixado", com a data e o número de vezes.
Nuvem opcional (rclone + MEGA/Koofr grátis): veja `docs/NUVEM-passo-a-passo.md`.

## Vídeos de Página (opcional)
Monta vídeos curtos no formato "post de página": avatar, nome e @ da sua página, uma frase em cima e o vídeo
embaixo. **Use só vídeos seus ou com permissão do autor**; a descrição sempre leva o crédito ao criador.
- Só roda quando você clica em **Buscar vídeos** na seção; nada é buscado sozinho.
- **Agente 11 (curadoria)** + supervisor: avalia candidatos curtos, ordena por engajamento
  ((curtidas + 5 × comentários) por mil visualizações), prioriza vídeos originais e sugere 3 frases para cada.
- **Agente 12 (montagem)** + supervisor: olha quadros reais do vídeo, escreve a frase final, decide entre recortar e
  encaixar, escreve a descrição com crédito e monta com FFmpeg. Depois vem o QC técnico.
- **Duas aprovações suas**, com os 5 botões: o vídeo com a frase e o resultado final.
- **Regras automáticas** (ajustáveis em `"pagina"` no Painel): vídeo feito por IA, muito repostado ou de canal em
  `canais_regra_ia` só entra se for muito viral e estiver no topo do engajamento; estrangeiro só com engajamento
  bem acima da mediana; nenhum vídeo abaixo de `engajamento_min_por_mil`.
- Cada lote grava um log detalhado em `LOGS/pagina/<lote>.jsonl`.
- A seção separa **Cortes criados**, **Aprovados** e **Descartados**.
