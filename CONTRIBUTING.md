# Como contribuir

Obrigado pelo interesse! O SAPI é escrito e documentado em **português do Brasil**.

## Antes de começar
- Abra uma issue descrevendo o problema ou a ideia antes de um PR grande.
- O projeto precisa continuar leve: ele tem que rodar num PC sem GPU, com 8 GB de RAM. Não traga dependência
  pesada sem um motivo forte, e prefira a biblioteca padrão do Python.
- Use só ferramentas gratuitas e de código aberto. Nada de API paga.

## Preparando o ambiente
1. Rode `instalar.bat`, ou crie o `.venv` e rode `pip install -r requirements.txt`.
2. Rode os testes com a lista de módulos (nunca `discover`):
   ```bash
   .venv\Scripts\python -m unittest tests.test_ajustes tests.test_armazenamento tests.test_cota tests.test_decisoes tests.test_pagina tests.test_postagem tests.test_producao tests.test_recomendacao tests.test_remoto tests.test_revisao tests.test_ritmo tests.test_traducao
   ```
   Os testes criam uma pasta de dados temporária e nunca tocam nos seus dados.

## Regras do código
- Mudança de comportamento vem com teste.
- Banco: só migrações aditivas e idempotentes (em `cortes/db.py`). Nunca apague coluna nem dado do usuário.
- A interface (`cortes/ui/`) não usa build, bibliotecas nem CDN. Anime só `transform` e `opacity`, respeite
  `prefers-reduced-motion` e passe todo texto do servidor por `esc()`.
- Nunca registre senha, token, cookie ou credencial em log.
- Nunca inclua vídeos, bancos, logs, nomes de canais reais ou dados de terceiros em testes, exemplos ou prints.

## Pull requests
- Um assunto por PR, com descrição do que mudou e de como você testou.
- Antes de enviar, rode os testes e o `gitleaks git . --redact`.
