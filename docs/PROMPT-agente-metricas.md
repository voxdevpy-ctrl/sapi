# PROMPT — Agente 10: Analista de Desempenho das Postagens (rascunho para discussão)

> Destinatário: a IA que vai implementar este agente dentro da Central de Cortes já existente
> (`<pasta do SAPI>`). Leia o `LEIA-ME.md` e o código antes de qualquer mudança.

---

## 1. Sua função

Você vai construir e operar o **Agente 10 — Analista de Desempenho**, com seu supervisor individual
(**Supervisor 10 — Auditor de Métricas**), integrado à linha de produção existente (agentes 01–09,
gerente-geral, SQLite em `PRODUCAO\cortes.db`).

Objetivo: acompanhar os cortes que o usuário postou, medir o desempenho, explicar **o que está dando certo
e errado e por quê**, e transformar isso em **sinais de preferência** para o Agente 01 (pesquisa) e o
Agente 04 (seleção de cortes). Esses sinais **orientam, não mandam**: a busca continua explorando conteúdo
novo e dando segunda chance.

## 2. Regras inegociáveis (herdadas do projeto)

- **Custo zero.** Nada de API paga, assinatura ou crédito. Só a assinatura do Claude Code já existente.
- **Nada roda fora do `iniciar.bat`.** Sem Agendador do Windows e sem serviço em segundo plano. A coleta e
  o relatório diário acontecem enquanto a Central estiver aberta. Se ela ficou fechada, a primeira abertura
  do dia põe tudo em dia.
- **Hardware fraco** (i3, 8 GB): coleta leve, sequencial, sem baixar vídeos para medir.
- **Evidência acima de opinião.** Todo número diz de onde veio e quando foi coletado. Nunca invente
  métricas, nunca estime visualizações, nunca "complete" dado faltante.
- **Não contorne plataformas.** Só métodos permitidos. Tokens guardados protegidos e nunca em log.
- **Correlação não é causa.** O "por quê" é sempre uma hipótese com a evidência que a sustenta e o
  tamanho da amostra.
- **Português do Brasil** em tudo que o usuário lê.

## 3. Situação atual que você precisa respeitar

- A postagem é **manual**: o usuário baixa o corte na aba **Prontos** e posta ele mesmo. O sistema só sabe
  que algo foi postado quando ele clica em **Marcar como postado** e informa o link (tabela
  `publications`, estado `POSTADO_PELO_USUARIO`, campo `url`).
- Cada corte já tem as características que vão ser cruzadas com o desempenho (tabelas `cuts`, `exports`,
  `videos`, `opportunities`): assunto, nicho, canal de origem, idioma original e se foi traduzido,
  participantes, duração, tipo de abertura, estilo (história, humor, debate…), notas do editor, título,
  hashtags, horário programado e real da postagem, e a nota que o usuário deu nos 5 botões.

## 4. De onde vêm as métricas — plataformas do usuário: **Instagram (Reels) e TikTok**

Pesquisa feita em 09/11/2026. Antes de implementar, **confirme cada ponto na documentação oficial atual**
(Meta for Developers e TikTok for Developers) — regras de acesso mudam.

### 4.1 Instagram — caminho principal, oficial e gratuito
- **API do Instagram com login do Instagram** (`graph.instagram.com`), permissões `instagram_business_basic`
  + `instagram_business_manage_insights`.
- **Sem App Review**: um app que só atende contas que o próprio usuário possui e adicionou ao app usa
  *Standard Access*, que não exige revisão da Meta.
- **Requisitos (o usuário faz, uma vez):** conta do Instagram como **profissional (Criador)** — troca gratuita
  nas configurações; criar um app gratuito em developers.facebook.com, adicionar o produto Instagram e a
  própria conta como testadora; autorizar e gerar o token. Token de longa duração (~60 dias) **renovado
  automaticamente** pelo sistema antes de vencer; guardado protegido, nunca em log.
- **Métricas por Reel** (`GET /{media-id}/insights`): `views`, `reach`, `likes`, `comments`, `saved`,
  `shares`, `total_interactions`, `follows`, `profile_visits`, `ig_reels_avg_watch_time` (ms) e
  `ig_reels_video_view_total_time`. Algumas aparecem como "em desenvolvimento" na Meta: trate ausência
  como "sem dado". Se `shares` falhar, teste pelo endpoint de métricas e registre o resultado.
- Vínculo post ↔ corte: pelo link (permalink) que o usuário informa, ou listando as mídias da conta
  (`/me/media`) e casando por data/legenda — com confirmação do usuário na primeira vez.

### 4.2 TikTok — caminho oficial a TESTAR primeiro, com alternativas
- **Display API** (`/v2/video/query/` e `/v2/video/list/`, escopo `video.list`) devolve por vídeo
  `view_count`, `like_count`, `comment_count`, `share_count`. **Não** devolve tempo assistido, retenção,
  alcance ou seguidores ganhos.
- **Acesso:** o modo *Sandbox* permite até 10 contas do próprio desenvolvedor sem auditoria, mas **não está
  documentado se `video.list` retorna dados reais no Sandbox**, e o guia oficial cita aprovação do Login Kit.
  → **Primeira tarefa da implementação: testar** com a conta do usuário e relatar o resultado exato.
- **Se o oficial não liberar dados**, oferecer ao usuário, nesta ordem:
  1. **Registro manual rápido** na aba Prontos/Desempenho: views, curtidas, comentários, compartilhamentos,
     salvamentos e **% assistido / tempo médio** (que só o TikTok Studio mostra). 30 segundos por vídeo,
     só nos marcos 24 h e 72 h; o sistema avisa quando é hora.
  2. **Leitura da página pública do vídeo** (yt-dlp, só metadados públicos: views, curtidas, comentários,
     compartilhamentos). Zona cinzenta nos termos de uso do TikTok → **só ativar com autorização explícita
     do usuário**, com intervalo longo entre leituras.

### 4.3 Regras gerais
- Mostrar na interface qual fonte está ativa para cada plataforma.
- Nunca afirmar que coletou de uma fonte não configurada. Sem dado = "sem dado", nunca zero.
- Nenhuma senha digitada pelo agente: login e autorizações são feitos pelo usuário.

## 4.4 O que é "rendeu" (definição usada em todo o agente)

Sem dinheiro direto, "rendeu" = **desempenho relativo ao próprio perfil**, por plataforma:
- **Alcance**: views na mesma idade (24 h / 72 h / 7 d) ÷ mediana dos posts do mesmo perfil na mesma idade.
- **Retenção** (quando houver): tempo médio assistido ÷ duração do corte. É o melhor sinal de conteúdo bom.
- **Engajamento forte**: (compartilhamentos + salvamentos) ÷ views — pesa mais que curtida, porque mostra
  que a pessoa quis guardar ou mandar para alguém.
- **Engajamento leve**: (curtidas + comentários) ÷ views.
- **Crescimento**: seguidores ganhos / visitas ao perfil por post (Instagram).
- Classificação final por post: **rendeu muito / rendeu / na média / abaixo / fraco**, com a métrica que
  decidiu e a confiança (cai quando faltam dados, ex.: TikTok sem retenção).
- Pesos dessa composição configuráveis; a explicação sempre diz quais métricas estavam disponíveis.

## 5. Coleta (determinística, Python — sem IA)

- Para cada postagem (Instagram e TikTok) com link: coletar em marcos de idade **2 h, 24 h, 72 h e 7 dias**
  (o que estiver vencido e ainda não coletado). Guardar série temporal numa tabela nova
  `metrics(publication_id, coletado_em, idade_h, fonte, views, likes, comments, shares, retencao_media,
  pct_assistido, inscritos, bruto_json)`.
- Idempotente: rodar duas vezes não duplica marco. Falha de rede = tentar depois, sem bloquear a produção.
- Sem link informado: lembrar o usuário na aba Prontos ("informe o link para acompanhar").

## 6. Análise diária (IA, uma chamada por dia)

Uma vez por dia (no horário configurado, ou na primeira abertura do dia seguinte):

1. **Comparação justa.** Compare cortes **na mesma idade** (ex.: views em 24 h) e **na mesma plataforma**.
   Use mediana do próprio histórico como referência, não números absolutos. Normalize por horário/dia de
   postagem quando houver amostra.
2. **Desempenho por corte**: acima / na média / abaixo, com o tamanho da diferença.
3. **Padrões por característica**: para cada característica (nicho, assunto, idioma, traduzido ou não,
   duração, abertura, estilo, participante, canal de origem, estilo de título, horário), calcule o efeito
   médio **com intervalo de incerteza** e o número de postagens por trás. Só chame de padrão se houver
   amostra mínima configurável (padrão: ≥ 3 postagens em cada lado da comparação).
4. **O porquê** (IA): para os melhores e piores do dia, assistir não é possível; usar transcrição, título,
   primeiros segundos da legenda, duração e métricas de retenção (se houver) para propor hipóteses
   ("abertura com pergunta reteve mais nos 3 primeiros segundos"), cada uma marcada como
   **hipótese**, com evidência e confiança.
5. **Discordância com o usuário**: destacar quando um corte que ele deu +100 foi mal (ou vice-versa).
   Isso não muda a nota dele; serve para os dois aprenderem.

## 7. Saídas

### 7.1 Relatório do dia (para o usuário — aba nova "Desempenho", legível no celular)
- Resumo em 3 linhas: o que foi melhor, o que foi pior, uma recomendação.
- Top 3 e bottom 3 do período, com os números e a fonte de cada um.
- Padrões confirmados (amostra suficiente) separados de **sinais fracos** (amostra pequena).
- Hipóteses do porquê, rotuladas como hipóteses.
- O que mudou na preferência enviada à busca hoje e por quê.
- Itens sem dado (link faltando, fonte não configurada).

### 7.2 Sinais para a busca e a seleção (estruturado, JSON validado por schema)
Para cada característica: `peso_desempenho` (−1 a +1), `confianca` (0–1), `amostra`, `explicacao`.
Regras de uso pelo Agente 01/04 e pelo recomendador (`recommend.py`):
- O desempenho entra como **um componente a mais**, nunca substitui as notas do usuário. Teto de
  influência configurável (padrão: **até 30%** da pontuação de recomendação).
- Peso escalado pela confiança; sinais com amostra pequena quase não pesam.
- Decaimento com o tempo (meia-vida configurável, padrão 21 dias): tendência de ontem não manda para
  sempre.
- **Exploração preservada**: a cota de conteúdo novo e de segunda chance do recomendador não pode cair por
  causa do desempenho. Desempenho ruim reduz prioridade, nunca bloqueia um tema.
- Preferência PT-BR e regras de autorização continuam valendo acima de tudo.

## 8. Supervisor 10 — Auditor de Métricas (independente, somente leitura)
Verifica antes de o gerente aceitar o relatório:
- Todo número tem fonte e horário de coleta e bate com a tabela `metrics`.
- Comparações feitas na mesma idade e plataforma.
- Nenhum padrão afirmado abaixo da amostra mínima; hipóteses rotuladas como hipóteses.
- Pesos enviados à busca respeitam teto, confiança e decaimento.
- Nenhuma métrica inventada ou "zero" no lugar de "sem dado".
Relatório no formato padrão do projeto (deveria / fez / evidências / problemas / correções / pode avançar).

## 9. Interface
- Aba **Desempenho**: relatório do dia, gráfico simples por corte (views por idade), tabela de padrões.
- Na aba **Prontos**: campo para o link ao marcar como postado (já existe) e, opcional, campos de métrica
  manual (camada A).
- Faixa de status: "Coletando métricas…" quando estiver coletando.

## 10. Testes obrigatórios
- Marcos de idade idempotentes; fonte indisponível não trava nada.
- Comparação só na mesma idade/plataforma.
- Amostra pequena não vira padrão; teto de 30% respeitado; exploração mantida (cota inalterada).
- Desempenho ruim nunca zera prioridade de um tema.
- Dados de teste nunca misturados com os reais.

## 11. Entrega
Antes de implementar, apresente: fontes de métrica escolhidas e o que cada uma exige do usuário,
estrutura das tabelas, formato do relatório e dos sinais, e os riscos. Só implemente após aprovação.
