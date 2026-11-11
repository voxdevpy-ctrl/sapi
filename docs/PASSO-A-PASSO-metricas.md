# Passo a passo: liberar as métricas do Instagram e do TikTok

Para fazer em casa, **no computador** (os sites de desenvolvedor funcionam mal no celular). Algumas etapas
são no app do celular, e isso está avisado. Tempo total: uns **40 minutos**. Custo: **zero**.

> **Regra de ouro:** em nenhum momento me mande senha, token, "App Secret" ou "Client Secret" pelo chat.
> Você só vai **preparar** tudo hoje. Quando eu implementar o Agente 10, a Central vai ganhar um botão
> **Conectar** e você autoriza por lá. Nada precisa ser copiado nem anotado hoje.

---

## Parte 1: Instagram (cerca de 25 min)

### 1.1 Passar a conta para profissional (Criador): no **celular**, app do Instagram
1. Abra seu **perfil**, toque no menu **☰** (canto superior direito) e entre em **Configurações e atividade**.
2. Role até **Tipo de conta e ferramentas** (pode aparecer como "Para profissionais").
3. Toque em **Mudar para conta profissional**, depois em **Continuar** nas telas de apresentação.
4. Escolha uma **categoria**. Sugestão: "Criador de conteúdo digital". Marque "Exibir no perfil" só se quiser.
5. Quando perguntar o tipo, escolha **Criador** (não "Empresa").
6. Se oferecer **conectar a uma Página do Facebook**, toque em **Pular**: não precisamos disso.
7. Pronto. Nada muda para quem te segue. A conta pode continuar pública ou privada, mas o ideal é pública.

**Deu certo se:** no perfil aparece o botão **Painel profissional**.

### 1.2 Criar a conta de desenvolvedor da Meta: no **computador**
1. Acesse **developers.facebook.com** e clique em **Começar** (ou "Get Started"), no canto superior direito.
2. Entre com sua **conta do Facebook**. Se não tiver uma, crie em facebook.com antes: é obrigatório para usar o site de desenvolvedores, mesmo que você não use o Facebook.
3. Aceite os termos da plataforma. Depois a Meta pede para **verificar a conta** por SMS (celular) e para **confirmar o e-mail**: faça as duas coisas.
4. Na pergunta sobre o que você faz, escolha **Desenvolvedor** (ou "Outro"). Isso não muda nada.

**Deu certo se:** você cai no painel **Meus apps** (My Apps).

### 1.3 Criar o app (gratuito)
1. Em **Meus apps**, clique em **Criar app**.
2. **Detalhes do app:**
   - Nome: **Central de Cortes Metricas**. ⚠️ A Meta **recusa** nomes com "Instagram", "Insta", "IG", "Facebook" ou "Meta".
   - E-mail de contato: o seu.
   - Clique em **Avançar**.
3. **Casos de uso:** marque **"Gerenciar mensagens e conteúdo no Instagram"** ("Manage messaging & content on Instagram"). Clique em **Avançar**.
4. **Empresa:** escolha **"Ainda não quero conectar um portfólio empresarial"**. Clique em **Avançar**.
5. Nas telas de **Requisitos** e **Visão geral**, só clique em **Avançar** e depois em **Criar app** (ou "Ir para o painel"). Pode pedir sua senha do Facebook de novo: quem digita é você.
6. **Não** clique em "Publicar" e **não** coloque o app em modo "Ao vivo" (Live). Ele fica em **Desenvolvimento**, e é assim que precisa ficar: só você usa.

### 1.4 Ativar a permissão de métricas
1. No menu da esquerda do app, abra **Casos de uso**. No cartão do Instagram, clique em **Personalizar**.
2. Abra **Permissões** (ou "Permissions and features").
3. Confira se estas duas estão **adicionadas** e, se ao lado de alguma aparecer **Adicionar**, clique:
   - `instagram_business_basic`
   - `instagram_business_manage_insights` ← **é esta que dá as métricas**
4. As outras que já vierem (mensagens, comentários, publicação) podem ficar: o sistema não vai usá-las.
   Na hora de conectar, você poderá desmarcar o que não for métrica.

### 1.5 Adicionar sua conta do Instagram como testadora
1. No menu da esquerda, abra **Funções do app** e depois **Funções** ("App roles" → "Roles").
2. Clique em **Adicionar pessoas** e escolha **Testador do Instagram** ("Instagram Tester").
3. Digite seu **@ do Instagram** (sem o @) e clique em **Adicionar**.
4. Agora no **celular**, no app do Instagram: **☰** → **Configurações e atividade** → **Apps e sites**
   (ou "Permissões do site") → **Convites de testador** → **Aceitar** o convite do "Central de Cortes Metricas".
   - Se o convite não aparecer, espere 5 minutos e reabra o app.
   - Se ainda não aparecer, aceite pelo navegador: entre em **instagram.com** logado, vá a
     Configurações → Apps e sites → **Convites de testador**.

**Deu certo se:** em "Funções" no site da Meta, seu @ aparece como Testador do Instagram, **sem** "Pendente".

### 1.6 Parar aqui ✋
**Não** clique em "Gerar tokens de acesso" ("Generate access tokens") hoje. Esse é o último passo, e vamos
fazê-lo juntos quando o botão **Conectar Instagram** existir na Central. Leva 1 minuto e você mesmo autoriza.

---

## Parte 2: TikTok (cerca de 15 min, no computador)

### 2.1 Conta de desenvolvedor
1. Acesse **developers.tiktok.com** e clique em **Log in** (canto superior direito).
2. Entre com a **mesma conta do TikTok em que você posta**, pela opção "Continue with TikTok" se aparecer.
   Se o site pedir para criar uma conta de desenvolvedor com e-mail, crie: ela é separada do TikTok e é gratuita.
3. Aceite os termos de desenvolvedor.

### 2.2 Criar o app
1. Clique no seu avatar e depois em **Manage apps** (Gerenciar apps), e então em **Connect an app** ou **Create app**.
2. Se perguntar o dono do app, escolha **Individual** (pessoa física). Se só existir a opção "organização",
   crie uma com seu nome.
3. Nome do app: **Central de Cortes Metricas** (não use "TikTok" no nome).

### 2.3 Criar o Sandbox (modo de teste, sem auditoria)
1. Dentro do app, no topo da página, procure a chave **Production / Sandbox** e mude para **Sandbox**.
2. Clique em **Create Sandbox**, dê o nome **teste** e confirme.
3. **Todo o resto da Parte 2 é feito dentro do Sandbox.** Confira se no topo está escrito "Sandbox".

### 2.4 Preencher o básico
1. Em **Basic information**:
   - **App icon:** envie o ícone do SAPI. O arquivo está em
     `<pasta do SAPI>\cortes\ui\icone.png`. Se o site reclamar do tamanho, me avise depois
     que eu gero um maior.
   - **Category:** "Entertainment" ou "Utilities".
   - **Description:** `Ferramenta pessoal que lê as estatísticas dos meus próprios vídeos.`
   - Se exigir **Terms of Service URL** e **Privacy Policy URL** para salvar, deixe em branco e tente
     salvar mesmo assim (no Sandbox costumam ser opcionais). Se travar, anote a mensagem e pare aqui.
2. Em **Platforms**, marque **Desktop** (só ela).

### 2.5 Adicionar os produtos e a permissão de vídeos
1. Em **Products** clique em **Add products** e adicione **Login Kit**. Se aparecer **Display API**,
   adicione também.
2. Em **Scopes** (permissões), garanta estas duas:
   - `user.info.basic` (já vem)
   - `video.list` ← **é esta que dá views, curtidas, comentários e compartilhamentos**
3. Em **Login Kit** → **Desktop** → **Redirect URI**, cole **exatamente**:
   ```
   http://127.0.0.1:8770/tiktok/callback/
   ```
   (é o endereço da própria Central no seu PC; nada sai para a internet por ele)
4. Clique em **Save** / **Apply changes**.

### 2.6 Liberar a sua conta no Sandbox
1. Abra **Sandbox settings** e depois **Target users** (usuários-alvo).
2. Clique em **Add account**. Vai abrir o login do TikTok: entre com a **conta em que você posta** e autorize.
3. Sua conta deve aparecer na lista de target users.

### 2.7 Parar aqui ✋
Na página do app existem **Client key** e **Client secret**. **Não copie, não fotografe, não mande pra mim.**
Quando a Central tiver o botão **Conectar TikTok**, você cola os dois lá (ficam guardados protegidos no
Windows) e autoriza. A primeira coisa que eu vou fazer é **testar se o Sandbox devolve os números reais**
dos seus vídeos e te mostrar o resultado.

---

## Checklist final (marque e me mande só os ✔/✘, nada de códigos)

- [ ] Instagram em **conta profissional (Criador)**
- [ ] Conta de desenvolvedor Meta criada e verificada
- [ ] App **Central de Cortes Metricas** criado, em modo **Desenvolvimento**
- [ ] Permissão `instagram_business_manage_insights` adicionada
- [ ] Convite de **Testador do Instagram** aceito (sem "Pendente")
- [ ] Conta de desenvolvedor TikTok criada
- [ ] App TikTok com **Sandbox** criado
- [ ] Plataforma **Desktop**, Login Kit, escopo `video.list` e Redirect URI salvos
- [ ] Sua conta do TikTok em **Target users**

## Se travar em algum ponto

| O que aconteceu | O que fazer |
|---|---|
| O botão ou menu tem nome um pouco diferente | Normal: Meta e TikTok mudam os textos. Procure a palavra-chave (ex.: "Roles", "Tester", "Scopes", "Sandbox"). |
| O site está em inglês | Tudo bem: os nomes em inglês estão entre parênteses neste guia. |
| A Meta recusou o nome do app | Tire qualquer referência a Instagram/Facebook/Meta. Ex.: "Cortes Painel". |
| O convite de testador não aparece | Espere 5 min, reabra o app, tente pelo navegador (passo 1.5.4). |
| A Meta pede "verificação de empresa" ou "App Review" | **Não faça.** Não é necessário para usar só na sua conta. Pule ou feche essa tela. |
| O TikTok exige URL de privacidade/termos para salvar | Pare na etapa, anote a mensagem exata e me conte. Eu crio uma página simples para isso. |
| Qualquer tela pede **pagamento ou cartão** | **Pare.** Nada disso tem custo; se pedir cartão, algo está errado. |
| Alguma tela pede para você mandar um token ou segredo para alguém | Não mande. Só se cola isso na Central, e só depois que eu avisar. |

Se travar, não precisa resolver: pare na etapa, anote **o número do passo e a mensagem exata da tela**
(um print sem códigos visíveis também serve) e seguimos daí.
