# SAPI — Sistema Autônomo de Produção para a Internet

O SAPI transforma vídeos longos (lives, podcasts, entrevistas) em **cortes curtos legendados** para TikTok,
Instagram e Shorts. Ele roda **no seu computador**, mesmo num PC simples, **sem placa de vídeo** e **sem API paga**.
Você só entra em dois momentos: aprovar os assuntos e aprovar cada corte.

> **Use só com conteúdo seu ou que você tem direito de usar.** O SAPI é uma ferramenta. Respeitar direitos
> autorais e os termos de cada plataforma é responsabilidade de quem usa. Veja [Uso responsável](#uso-responsável).

## O que ele faz

- **Pesquisa assuntos** e verifica as fontes antes de pedir a sua aprovação.
- **Baixa, comprime e transcreve** localmente, com faster-whisper na CPU. Também identifica quem fala.
- **Escolhe os cortes** com contexto, tira os silêncios inúteis e mantém as pausas dramáticas. Depois legenda em
  português (traduz quando o vídeo é estrangeiro) e finaliza em 1080×1920.
- **Supervisiona tudo:** cada etapa é auditada por um supervisor, e os relatórios ficam na Central.
- **Prontos para postar:** você baixa o vídeo, copia o título e a descrição, e marca TikTok e Instagram
  separadamente. O corte só sai dos pendentes quando as duas redes estão marcadas.
- **Vídeos de Página:** vídeos curtos de humor montados no formato "post de página", com crédito ao criador.
- **Limite de armazenamento:** quando chega no limite, a produção pausa e só volta quando há espaço.
- **Celular:** você acessa a Central pelo celular, pela sua rede privada (Tailscale) e com PIN.

## Requisitos

| Programa | Para quê | Como instalar |
|---|---|---|
| Windows 10/11 | sistema testado | — |
| Python 3.12+ | o SAPI em si | [python.org](https://www.python.org/downloads/) (marque "Add to PATH") |
| FFmpeg | vídeo e áudio | `winget install Gyan.FFmpeg` |
| Node.js | o yt-dlp usa para o YouTube | `winget install OpenJS.NodeJS.LTS` |
| Claude Code (opcional, recomendado) | os agentes de IA | [claude.com/product/claude-code](https://claude.com/product/claude-code), com login na **sua** conta |
| Tailscale (opcional) | acesso pelo celular | [tailscale.com/download](https://tailscale.com/download), no PC e no celular |

Testado só no **Windows 11** (i3-1005G1, 8 GB de RAM, sem GPU). Em outros sistemas o código pode funcionar,
mas os scripts `.bat` e o instalador são para Windows.

## Instalação

1. Baixe o projeto (botão **Code → Download ZIP**, ou `git clone`) e extraia numa pasta sua.
2. Dê dois cliques em **`instalar.bat`**. Ele:
   - confere os programas da tabela;
   - cria o ambiente Python (`.venv`) e instala as dependências;
   - oferece baixar os modelos de transcrição (~500 MB, uma vez só).
3. Dê dois cliques em **`iniciar.bat`**. O navegador abre em `http://127.0.0.1:8770`.

**Nada roda fora do `iniciar.bat`.** Fechar a janela dele encerra tudo, e o trabalho retoma na próxima abertura.

## Configuração

- **Pasta de dados:** banco, vídeos, logs e configuração ficam na pasta `PRODUCAO` dentro do SAPI. Para usar outra
  pasta, copie `.env.example` para `.env` e defina `SAPI_DADOS`. Essa pasta **nunca** vai para o repositório.
- **Ajustes:** ficam no **Painel**, em "Configurações". Lá você define o limite de armazenamento, as consultas
  de pesquisa, o ritmo dos cortes e o nome e o @ da página, entre outros.
- **Manual completo:** [LEIA-ME.md](LEIA-ME.md) explica cada etapa, a retenção de 14 dias, o perfil editorial,
  a nuvem opcional (rclone) e os limites conhecidos.

## Conectar o Claude Code

Os agentes de IA rodam pelo **Claude Code instalado e logado no seu próprio computador**, com a **sua** assinatura.
O SAPI não usa chave de API e remove `ANTHROPIC_API_KEY` do ambiente dos agentes, para não haver cobrança por API.

1. Instale o Claude Code e rode `claude` num terminal para entrar com a sua conta.
2. Na Central, abra **Painel → Conectar Claude Code**.

O botão mostra se o Claude Code está instalado, a versão e se há login salvo. Ele nunca lê nem envia
credenciais. Cada pessoa usa a própria conta; o SAPI não compartilha a assinatura de ninguém.

## Acesso pelo celular

Cada instalação liga o próprio celular ao próprio computador:

1. Instale o **Tailscale** no PC e no celular e entre com a **sua** conta nos dois.
2. Na Central, abra **Celular**, defina um PIN de 6 a 12 dígitos e ligue o acesso.
3. Reabra o `iniciar.bat`. No celular, abra o endereço mostrado na tela e digite o PIN.

O acesso fica desligado até você ligar. Ele só aceita a rede privada do Tailscale (ou o Wi-Fi de casa, se você
permitir). O PIN é guardado só como hash, e as sessões morrem quando o `.bat` fecha. Não abra portas no roteador.

## Uso responsável

- Use material **seu**, com **autorização por escrito** ou com licença que permita reuso.
- Os Termos do YouTube restringem baixar vídeos fora dos recursos oficiais. Para eliminar esse risco, trabalhe com
  arquivos colocados em `PRODUCAO\ENTRADA_LOCAL` e desligue `aceitar_creative_commons`.
- Dê crédito ao criador original. Os Vídeos de Página incluem o crédito na descrição automaticamente.
- Transcrições e vozes de pessoas são dados pessoais (LGPD). Elas ficam só na sua pasta de dados.

## Desenvolvimento

```bash
.venv\Scripts\python -m unittest tests.test_producao tests.test_pagina tests.test_remoto
```

Rode sempre com a lista de módulos (`tests.test_*`), nunca com `discover`. Os testes usam uma pasta temporária
e nunca tocam nos seus dados. Veja [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) e
[CLAUDE.md](CLAUDE.md).

## Licença

[MIT](LICENSE). As dependências têm as próprias licenças: faster-whisper (MIT), sherpa-onnx (Apache-2.0),
yt-dlp (Unlicense). O FFmpeg é instalado à parte, com a licença dele.
