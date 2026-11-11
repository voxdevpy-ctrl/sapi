# Segurança

## Como relatar uma falha

Não abra issue pública para falhas de segurança. Use o **Private vulnerability reporting** do GitHub, na aba
**Security → Report a vulnerability** deste repositório. Descreva o problema, como reproduzir e o impacto.
**Não envie senhas, tokens ou dados pessoais reais**, nem os seus nem os de outras pessoas.

## Como o SAPI se protege

- **Roda localmente.** O servidor escuta só em `127.0.0.1`. O acesso remoto vem desligado e, quando ligado, só
  aceita a rede privada do Tailscale (ou o Wi-Fi de casa, se você permitir).
- **PIN:**
  - guardado como hash PBKDF2-SHA256 com sal;
  - limite de tentativas;
  - sessões só na memória, que terminam quando o `.bat` fecha.
- **Requisições:** os POSTs exigem um cabeçalho próprio e Origin/Host conferidos, contra CSRF e DNS rebinding.
  Os arquivos servidos ficam presos às pastas permitidas, contra path traversal.
- **Sem chave de API.** Os agentes usam o Claude Code logado na conta de cada pessoa. O SAPI remove
  `ANTHROPIC_API_KEY` do ambiente dos agentes e nunca lê as credenciais do Claude Code.
- **Agentes de IA com poucas permissões.** Os agentes de produção só podem ler arquivos e devolver JSON validado
  por schema; eles não executam comandos. Títulos e legendas de vídeos de terceiros são tratados como dados,
  nunca como instruções.
- **Comandos externos** (FFmpeg, yt-dlp) recebem listas de argumentos, nunca texto montado num shell.
- **Interface:** todo texto vindo do servidor é escapado antes de ir para a tela. Não há script de CDN.
- **Repositório sem dados do usuário.** O `.gitignore` bloqueia banco, vídeos, logs, modelos, `.env`, cookies
  e configuração pessoal. Cada commit passa pelo [gitleaks](https://github.com/gitleaks/gitleaks).

Nenhum sistema é 100% seguro. Se encontrar algo, relate pelo canal privado acima.

## Versões suportadas

Só a versão mais recente da branch `main` recebe correções.
