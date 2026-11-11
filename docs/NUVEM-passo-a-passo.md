# Cópia na nuvem antes de apagar os vídeos postados (opcional)

Sem a nuvem, a Central **apaga o vídeo final no dia seguinte** às marcações **Postado TikTok** e
**Postado Instagram**. Título, descrição, hashtags, legendas e metadados **nunca** são apagados.
Com a nuvem ligada, ela **primeiro envia o vídeo e confere o tamanho**, e só depois apaga. Se o envio
falhar, o vídeo fica no PC e a Central tenta de novo na hora seguinte.

O programa que faz o envio é o **rclone** (gratuito, código aberto), que já instalei.

## Qual nuvem usar (grátis, sem ser o Google Drive)

| Serviço | Espaço grátis | Observação |
|---|---|---|
| **MEGA** (mega.io) | **20 GB** | Recomendado: cabem cerca de 400 cortes. Se você usar verificação em duas etapas no MEGA e o rclone recusar o login, use o Koofr. |
| **Koofr** (koofr.eu) | 10 GB | Alternativa: usa uma "senha de app" própria, separada da sua senha principal. |

Nenhum dos dois pede cartão.

## Passo a passo (cerca de 10 min, no PC)

1. **Crie a conta** em **mega.io**: clique em "Criar conta", escolha o plano **Grátis** e confirme o e-mail.
2. **Abra um terminal novo.** Aperte Windows, digite `PowerShell` e dê Enter. Precisa ser novo para o
   comando `rclone` ser reconhecido.
3. **Cadastre a nuvem no rclone:**
   ```bash
   rclone config
   ```
   Responda assim:
   - `n` (novo remoto) → nome: **`nuvem`**
   - Storage: digite **`mega`** e Enter
   - `user`: seu e-mail do MEGA
   - `pass`: `y` e digite **sua senha do MEGA** duas vezes. Ela não aparece na tela, e eu nunca a vejo.
   - Nas outras perguntas, só Enter (padrão). Em "Edit advanced config?" responda `n`.
   - Confirme com `y`.
4. **Ainda no menu do rclone, proteja o arquivo com senha:** escolha `s` (Set configuration password)
   → `a` (Add password) → crie uma **senha nova** (só para isso) e repita. Depois `q` para sair.
5. **Entregue essa senha à Central de forma protegida.** Ela fica criptografada pelo Windows, e só a sua
   conta neste PC consegue abrir:
   ```bash
   cd <pasta do SAPI>; .venv\Scripts\python.exe -m cortes nuvem-senha
   ```
   Digite a senha do passo 4.
6. **Ligue na Central:** abra a aba **Painel**. Na configuração, troque o trecho `"nuvem"` para:
   ```json
   "nuvem": { "habilitada": true, "rclone_remote": "nuvem", "pasta": "CentralDeCortes" }
   ```
   Depois clique em **Salvar**.
7. **Teste:**
   ```bash
   cd <pasta do SAPI>; .venv\Scripts\python.exe -m cortes nuvem-testar
   ```
   Se aparecer `ok`, está pronto. Os vídeos vão para a pasta **CentralDeCortes** no seu MEGA,
   separados por data e horário.

## Regras que valem sempre
- Só é apagado o **vídeo final (.mp4)** dentro de `PRODUCAO\PRODUZIDOS`. Nada fora dessa pasta.
- Por padrão, o vídeo só é apagado depois que **as duas** marcações existirem. Para apagar com só
  uma, mude `"pos_postagem": {"exigir": "qualquer"}` no Painel.
- Desmarcar um "Postado" antes do dia seguinte cancela a exclusão.
- Para nunca apagar: `"pos_postagem": {"excluir_video": false}`.
