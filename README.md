# 💰 Fincash — Gestão Financeira Inteligente com IA

O **Fincash** é uma plataforma de gestão financeira pessoal que combina Inteligência Artificial, automação e integração com o Telegram para facilitar o controle das finanças e o acompanhamento de investimentos.

A aplicação permite registrar receitas e despesas por meio de mensagens em linguagem natural, acompanhar o orçamento pessoal, organizar economias e obter informações sobre o mercado financeiro, tudo em uma experiência simples e intuitiva.

O projeto foi desenvolvido em Python, utilizando Flask, PostgreSQL, Telegram Bot API e inteligência artificial por meio da API da Groq.

---

## 🌐 Acesse o Projeto

🚀 **Aplicação online:** [Acessar Fincash](COLE_AQUI_O_LINK_DA_APLICACAO)

Conheça a plataforma Fincash e explore suas funcionalidades de gestão financeira, inteligência artificial e acompanhamento de investimentos.

---

## 🎯 Objetivo do Projeto

O objetivo do Fincash é tornar a gestão financeira mais acessível, prática e inteligente, ajudando os usuários a desenvolverem hábitos financeiros saudáveis.

A plataforma centraliza o controle financeiro em um ambiente integrado, permitindo acompanhar movimentações, visualizar indicadores e receber informações personalizadas sobre investimentos.

## ✨ Funcionalidades

### 📊 Gestão Financeira
- Cadastro e gerenciamento de receitas e despesas.
- Registro automático de movimentações por meio de mensagens em linguagem natural.
- Classificação inteligente de transações com auxílio de IA.
- Gerenciamento de categorias personalizadas.
- Dashboard com resumo financeiro.
- Histórico de transações.
- Gráficos de evolução financeira.
- Filtros por período.

### 💵 Poupança e Planejamento
- Registro de depósitos e saques.
- Acompanhamento do saldo acumulado.
- Definição de metas de economia.
- Alertas relacionados à poupança.
- Previsão e acompanhamento financeiro.

### 📈 Investimentos
- Notícias financeiras atualizadas conforme o perfil do investidor.
- Indicadores econômicos, como dólar, euro, Bitcoin, Ethereum, Selic e IPCA.
- Análises de mercado geradas por Inteligência Artificial.
- Quiz para identificação do perfil de investidor.
- Perfis conservador, moderado, arrojado e cripto.
- Alertas opcionais sobre notícias e movimentações do mercado.
- Assistente de IA para dúvidas sobre investimentos.

### 🤖 Integração com Telegram
- Registro de receitas e despesas por mensagens.
- Interpretação automática de valores e categorias.
- Consulta de resumo financeiro.
- Gerenciamento de poupança e investimentos.
- Acesso a informações financeiras.
- Alertas e notificações automatizadas.

### 🔐 Segurança e Usuários
- Cadastro e autenticação de usuários.
- Senhas armazenadas com hash.
- Recuperação de senha por e-mail.
- Vinculação de conta com o Telegram.
- Controle de sessão.
- Proteção do endpoint de webhook.
- Histórico de alterações em dados financeiros.

---

## 🛠️ Tecnologias Utilizadas

| Tecnologia | Aplicação |
|---|---|
| Python | Linguagem principal |
| Flask | Desenvolvimento da aplicação web |
| PostgreSQL | Banco de dados |
| Groq API | Inteligência Artificial |
| Telegram Bot API | Integração com chatbot |
| python-telegram-bot | Gerenciamento do bot |
| HTML, CSS e JavaScript | Interface web |
| Flask-Mail | Envio de e-mails |
| Gunicorn | Servidor de aplicação |
| python-dotenv | Gerenciamento de variáveis de ambiente |

### APIs e serviços externos
- **Groq:** processamento de linguagem natural e análises com IA.
- **Google Notícias RSS:** obtenção de notícias financeiras.
- **AwesomeAPI:** indicadores de moedas e criptomoedas.
- **Banco Central do Brasil:** indicadores econômicos.

---

## 📁 Estrutura do Projeto

```text
bot_ia/
│
├── static/                 # Arquivos estáticos da aplicação
│
├── templates/              # Páginas e templates HTML
│
├── fincash.py              # Aplicação principal e regras de negócio
│
├── investimentos.py        # Módulo de investimentos e análises
│
├── telegram_webhook.py     # Integração com o Telegram via webhook
│
├── utils.py                # Funções utilitárias
│
├── gunicorn.conf.py        # Configurações do Gunicorn
│
├── requirements.txt        # Dependências do projeto
│
└── .gitignore              # Arquivos ignorados pelo Git
```

---

## ⚙️ Instalação e Configuração

### 1. Clone o repositório

```bash
git clone https://github.com/JU-ulia/bot_ia.git
```

### 2. Entre na pasta do projeto

```bash
cd bot_ia
```

### 3. Crie um ambiente virtual

```bash
python -m venv venv
```

Ative o ambiente virtual:

**Windows:**
```bash
venv\Scripts\activate
```

**Linux / macOS:**
```bash
source venv/bin/activate
```

### 4. Instale as dependências

```bash
pip install -r requirements.txt
```

### 5. Configure as variáveis de ambiente

Crie um arquivo `.env` na raiz do projeto:

```env
# Inteligência Artificial
GROQ_API_KEY=sua_chave_groq

# Telegram
TELEGRAM_BOT_TOKEN=seu_token_telegram
TELEGRAM_WEBHOOK_SECRET=seu_segredo_webhook

# Banco de dados PostgreSQL
DATABASE_URL=sua_url_postgresql

# Segurança
FLASK_SECRET_KEY=sua_chave_secreta

# E-mail
EMAIL_USER=seu_email@gmail.com
EMAIL_PASS=sua_senha_de_aplicativo
SUPORTE_EMAIL=seu_email_de_suporte
SAC_EMAIL=seu_email_de_atendimento

# Aplicação
APP_BASE_URL=http://localhost:5000
WEBHOOK_BASE_URL=https://seu-dominio.com

# Cookies
COOKIE_SECURE=0
```

**Importante:**
- Obtenha a chave da IA na plataforma da [Groq](https://console.groq.com/).
- Crie seu bot e obtenha o token pelo [BotFather](https://t.me/BotFather).
- Configure um banco de dados PostgreSQL.
- Para utilizar o webhook do Telegram em produção, configure uma URL pública HTTPS e um segredo para o webhook.
- Nunca compartilhe o arquivo `.env` ou publique suas credenciais.

Caso utilize um banco PostgreSQL local em vez de `DATABASE_URL`, configure:

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=fincash
DB_USER=seu_usuario
DB_PASSWORD=sua_senha
```

---

## ▶️ Executando o Projeto

Com o ambiente virtual ativado e as variáveis de ambiente configuradas, execute:

```bash
python fincash.py
```

A aplicação web ficará disponível em:

```text
http://localhost:5000
```

O bot do Telegram também será inicializado, utilizando o token configurado no arquivo `.env`.

**Observação:** para executar o projeto corretamente, é necessário configurar o banco PostgreSQL, a chave da Groq e o token do Telegram.

### Execução em produção

O projeto também possui configuração para execução com Gunicorn:

```bash
gunicorn -c gunicorn.conf.py fincash:app_web
```

Em produção, configure as variáveis de ambiente e o webhook do Telegram de acordo com o endereço público da aplicação.

---

## 🧠 Inteligência Artificial

O Fincash utiliza Inteligência Artificial para interpretar mensagens financeiras escritas em linguagem natural.

Por exemplo, ao enviar:

> Gastei R$ 45,00 no supermercado hoje.

O sistema interpreta a mensagem, identifica o valor, classifica a movimentação e registra a despesa na categoria correspondente.

Na área de investimentos, a IA também analisa notícias financeiras e produz conteúdos educativos adaptados ao perfil do investidor.

---

## ⚠️ Aviso Importante

As informações e análises sobre investimentos disponibilizadas pelo Fincash possuem finalidade exclusivamente educativa e informativa.

O conteúdo gerado por Inteligência Artificial não constitui recomendação de compra ou venda de ativos financeiros, nem garantia de rentabilidade.

---

## 🚀 Melhorias Futuras

- Ampliação dos recursos de análise financeira.
- Novos relatórios e indicadores personalizados.
- Melhorias na experiência do usuário.
- Expansão das funcionalidades do assistente inteligente.
- Novas integrações com serviços financeiros.

---

## 📈 Projeto

**Fincash — Projeto TGI**

Repositório: [github.com/JU-ulia/bot_ia](https://github.com/JU-ulia/bot_ia)

Desenvolvido com Python, Inteligência Artificial e foco em inovação na gestão financeira pessoal.

---

## 👩‍💻 Desenvolvedoras

O projeto Fincash foi desenvolvido por:

- **Bianca Alves** — [LinkedIn](https://www.linkedin.com/in/biancasantosalves/)
- **Julia Garrido** — [LinkedIn](https://www.linkedin.com/in/julia-garrido-4a0265217/)

---

*Fincash — Mais inteligência para cuidar do seu dinheiro. 💚*
