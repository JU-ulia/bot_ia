import os
import json
import psycopg2
from groq import Groq
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from werkzeug.security import generate_password_hash, check_password_hash

# NOVAS IMPORTAÇÕES PARA O FLASK E CONTROLE DE SESSÃO
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import threading

# Carrega as variáveis de ambiente (.env)
load_dotenv()

# Inicializa a aplicação Flask
app_web = Flask(__name__)
app_web.secret_key = os.getenv("FLASK_SECRET_KEY", "fincash_secret_key_12345")
CORS(app_web)

# ==========================================
# CONEXÃO COM O BANCO DE DADOS
# ==========================================
def obter_conexao():
    """Cria e retorna uma nova conexão com o PostgreSQL."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )

# ==========================================
# TABLES (CRIAÇÃO E MIGRAÇÃO DAS TABELAS)
# ==========================================
def criar_tabelas():
    """Garante que as tabelas necessárias existam no banco de dados."""
    conexao = obter_conexao()
    cursor = conexao.cursor()

    try:
        # 1. Tabela de Usuários (com campo data_cadastro)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY,
            nome TEXT,
            senha TEXT,
            email TEXT,
            data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Garantia de Migração: Se a tabela 'users' já existia antes sem a coluna 'data_cadastro'
        cursor.execute("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
        """)

        # 2. Tabela de Transações (Lançamentos Financeiros)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT REFERENCES users(telegram_id),
            tipo VARCHAR(10) NOT NULL,
            valor NUMERIC(10, 2) NOT NULL,
            categoria VARCHAR(50),
            descricao TEXT,
            data DATE DEFAULT CURRENT_DATE
        );
        """)

        # Garantia de Migração: Se a tabela 'transactions' já existia antes sem a coluna 'data'
        cursor.execute("""
            ALTER TABLE transactions 
            ADD COLUMN IF NOT EXISTS data DATE DEFAULT CURRENT_DATE;
        """)

        # 3. Tabela de Categorias
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT REFERENCES users(telegram_id),
            nome TEXT NOT NULL
        );
        """)

        conexao.commit()
        print("Tabelas 'users', 'transactions' e 'categories' sincronizadas com sucesso!")

    except Exception as e:
        print(f"Erro ao criar/atualizar tabelas: {e}")
        conexao.rollback()

    finally:
        cursor.close()
        conexao.close()


# Executa a criação das tabelas ao iniciar a aplicação
criar_tabelas()

def deletar_transacao_por_id(telegram_id: str, id_transacao: int) -> bool:
    """Deleta um lançamento do usuário pelo ID e retorna True se foi encontrado."""
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute("""
                DELETE FROM transactions 
                WHERE id = %s AND telegram_id = %s;
            """, (id_transacao, telegram_id))
            deletados = cursor.rowcount
            conexao.commit()
            return deletados > 0
        
# ==========================================
# ROTAS DA API E INTERFACE WEB (FLASK)
# ==========================================

# 1. Rota Principal / Dashboard
@app_web.route('/')
def index():
    if 'telegram_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('main.html')

# 1. Rota Login
@app_web.route('/login')
def login_page():
    if 'telegram_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')

# 2. Rota da Tela de Login / Cadastro
@app_web.route('/api/login', methods=['POST'])
def handle_login():
    dados = request.get_json() or {}
    identifier = str(dados.get('identifier', '')).strip()
    password = str(dados.get('password', '')).strip()

    if not identifier or not password:
        return jsonify({"erro": "Informe seu Telegram ID ou E-mail e sua senha."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()

    # Procura o utilizador no banco
    cursor.execute("""
        SELECT telegram_id, nome, senha 
        FROM users 
        WHERE telegram_id = %s OR email = %s;
    """, (identifier, identifier))
    user = cursor.fetchone()

    cursor.close()
    conexao.close()

    if user:
        db_telegram_id, db_nome, db_senha_hash = user

        # -------------------------------------------------------------
        # 🔒 PASSO PRINCIPAL: Validar a senha com check_password_hash
        # -------------------------------------------------------------
        if db_senha_hash and check_password_hash(db_senha_hash, password):
            session['telegram_id'] = db_telegram_id
            session['user_name'] = db_nome
            return jsonify({"sucesso": True, "mensagem": "Login efetuado!"})
        else:
            return jsonify({"erro": "Senha incorreta."}), 401
    else:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    
@app_web.route('/api/cadastrar', methods=['POST'])
def handle_register():
    dados = request.get_json() or {}
    nome = str(dados.get('name', '')).strip()
    telegram_id = str(dados.get('telegram_id', '')).strip()
    password = str(dados.get('password', '')).strip()

    if not nome or not telegram_id or not password:
        return jsonify({"erro": "Todos os campos são obrigatórios."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()

    # Verificar se o utilizador já existe
    cursor.execute("SELECT telegram_id FROM users WHERE telegram_id = %s;", (telegram_id,))
    if cursor.fetchone():
        cursor.close()
        conexao.close()
        return jsonify({"erro": "Este Telegram ID já está cadastrado."}), 400

    # -------------------------------------------------------------
    # 🔒 PASSO PRINCIPAL: Gerar o Hash da Senha antes de salvar
    # -------------------------------------------------------------
    senha_hash = generate_password_hash(password)

    # Guarda 'senha_hash' em vez da senha em texto puro
    cursor.execute("""
        INSERT INTO users (telegram_id, nome, senha)
        VALUES (%s, %s, %s);
    """, (telegram_id, nome, senha_hash))

    conexao.commit()
    cursor.close()
    conexao.close()

    return jsonify({"sucesso": True, "mensagem": "Conta criada com sucesso!"})

# 5. Rota de Logout (Sair)
@app_web.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# 6. API: Retorna dados do usuário autenticado na sessão atual
@app_web.route('/api/usuario-atual', methods=['GET'])
def obter_usuario_atual():
    if 'telegram_id' not in session:
        return jsonify({"erro": "Não autenticado"}), 401
    return jsonify({
        "telegram_id": session['telegram_id'],
        "nome": session.get('user_name', 'Usuário')
    })

# 7. API: Retorna a data de cadastro do usuário logado
@app_web.route('/api/usuario-data-cadastro', methods=['GET'])
def obter_data_cadastro():
    if 'telegram_id' not in session:
        return jsonify({"erro": "Não autenticado"}), 401
    
    telegram_id = session['telegram_id']
    conexao = obter_conexao()
    cursor = conexao.cursor()

    cursor.execute("""
        SELECT TO_CHAR(COALESCE(data_cadastro, CURRENT_TIMESTAMP), 'YYYY-MM-DD') 
        FROM users 
        WHERE telegram_id = %s;
    """, (telegram_id,))
    
    row = cursor.fetchone()
    cursor.close()
    conexao.close()

    data_cadastro = row[0] if row else None
    return jsonify({"data_cadastro": data_cadastro})

# 8. API: Retorna as datas que possuem lançamentos cadastrados no banco
@app_web.route('/api/datas-com-transacoes', methods=['GET'])
def datas_com_transacoes():
    if 'telegram_id' not in session:
        return jsonify([]), 401
    
    telegram_id = session['telegram_id']
    conexao = obter_conexao()
    cursor = conexao.cursor()

    cursor.execute("""
        SELECT DISTINCT TO_CHAR(data, 'YYYY-MM-DD') 
        FROM transactions 
        WHERE telegram_id = %s AND data IS NOT NULL;
    """, (telegram_id,))
    
    datas = [row[0] for row in cursor.fetchall()]

    cursor.close()
    conexao.close()

    return jsonify(datas)


# 9. API: Consulta e envia os dados do Dashboard (com filtro por Mês/Ano ?mes=YYYY-MM)
@app_web.route('/api/dados-dashboard/<telegram_id>', methods=['GET'])
def obter_dados_dashboard(telegram_id):
    try:
        if 'telegram_id' in session:
            telegram_id = str(session['telegram_id']).strip()

        mes_filtro = request.args.get('mes', None)

        conexao = obter_conexao()
        cursor = conexao.cursor()

        if mes_filtro:
            condicao_data = "AND TO_CHAR(data, 'YYYY-MM') = %s"
            params_base = (telegram_id, mes_filtro)
        else:
            condicao_data = ""
            params_base = (telegram_id,)

        # 1. Total Renda e Despesa
        query_totais = f"""
            SELECT 
                COALESCE(SUM(CASE WHEN tipo = 'renda' THEN valor ELSE 0 END), 0) AS total_renda,
                COALESCE(SUM(CASE WHEN tipo = 'despesa' THEN valor ELSE 0 END), 0) AS total_despesas
            FROM transactions 
            WHERE telegram_id = %s {condicao_data};
        """
        cursor.execute(query_totais, params_base)
        row_totais = cursor.fetchone()
        total_renda = float(row_totais[0]) if row_totais else 0.0
        total_despesas = float(row_totais[1]) if row_totais else 0.0
        saldo = total_renda - total_despesas

        # 2. Total Poupança / Investimentos
        cursor.execute("""
            SELECT COALESCE(SUM(valor), 0)
            FROM transactions
            WHERE telegram_id = %s AND (categoria ILIKE 'Poupança' OR categoria ILIKE 'Investimentos');
        """, (telegram_id,))
        poupanca_row = cursor.fetchone()
        total_poupanca = float(poupanca_row[0]) if poupanca_row else 0.0

        # 3. Gastos por Categoria
        query_categorias = f"""
            SELECT categoria, SUM(valor) 
            FROM transactions 
            WHERE telegram_id = %s AND tipo = 'despesa' {condicao_data}
            GROUP BY categoria;
        """
        cursor.execute(query_categorias, params_base)
        categorias_rows = cursor.fetchall()
        categorias = [row[0] for row in categorias_rows]
        valores_categorias = [float(row[1]) for row in categorias_rows]

        # 4. Nome do Usuário
        cursor.execute("SELECT nome FROM users WHERE telegram_id = %s;", (telegram_id,))
        user_row = cursor.fetchone()
        nome_usuario = user_row[0] if user_row else "Usuário"

        # 5. BUSCA A DATA DO ÚLTIMO REGISTRO NO MÊS SELECIONADO (FORMATADA)
        query_ultima_atividade = f"""
            SELECT TO_CHAR(MAX(data), 'DD/MM/YYYY HH24:MI') 
            FROM transactions 
            WHERE telegram_id = %s {condicao_data};
        """
        cursor.execute(query_ultima_atividade, params_base)
        last_row = cursor.fetchone()
        ultima_atividade = last_row[0] if last_row and last_row[0] else None

        cursor.close()
        conexao.close()

        return jsonify({
            "usuario": nome_usuario,
            "totais": {
                "renda": total_renda,
                "despesa": total_despesas,
                "saldo": saldo,
                "poupanca": total_poupanca
            },
            "categorias": {
                "labels": categorias,
                "valores": valores_categorias
            },
            "chatbot": {
                "ultima_atividade": ultima_atividade
            }
        })

    except Exception as e:
        print(f"Erro interno no servidor (dados-dashboard): {e}")
        return jsonify({"erro": str(e)}), 500
            
def rodar_flask():
    """Roda o servidor web na porta 5000"""
    app_web.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# Inicializa o cliente da Groq
cliente_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))


# ==========================================
# LÓGICA DE NEGÓCIO E INTEGRAÇÃO GROQ
# ==========================================
def garantir_usuario_existe(telegram_id: str, nome: str):
    """Insere ou atualiza o usuário no banco de dados sem apagar credenciais."""
    conexao = obter_conexao()
    cursor = conexao.cursor()

    cursor.execute("""
        INSERT INTO users (telegram_id, nome)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET nome = EXCLUDED.nome;
    """, (telegram_id, nome))

    conexao.commit()
    cursor.close()
    conexao.close()


def interpretar_mensagem_financeira(texto_usuario: str) -> dict:
    """Envia o texto para a Groq (Llama 3) e retorna um dicionário JSON estruturado."""
    prompt = f"""
    Você é um assistente financeiro especialista em extrair lançamentos de texto livre, mesmo com erros de digitação ou gírias.
    
    Analise o texto a seguir: "{texto_usuario}"

    CATEGORIAS DE DESPESA:
    - Alimentação: Comida, almoço, aumoço, jantar, restaurante, lanche, mercado, iFood.
    - Carro: Combustível, gasolina, mecânico, IPVA, seguro, estacionamento.
    - Internet: Celular, internet residencial, TV.
    - Moradia: Aluguel, luz, água, gás, condomínio.
    - Lazer: Cinema, viagem, passeio, jogos.
    - Outros: Gastos diversos.

    CATEGORIAS DE RENDA:
    - Salário: Salário, pagamento, holerite.
    - Freelance: Bico, serviço, extra.
    - Investimentos: Dividendos, rendimentos.
    - Outras Rendas: Recebi, PIX recebido, presente, reembolso.

    Retorne APENAS o JSON com a estrutura:
    {{
        "descricao": "Nome legível do item",
        "valor": 0.00,
        "categoria": "Nome da Categoria",
        "tipo": "despesa" ou "renda"
    }}
    """

    resposta = cliente_groq.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        response_format={"type": "json_object"}
    )

    conteudo = resposta.choices[0].message.content.strip()
    return json.loads(conteudo)


def salvar_transacao(telegram_id: str, dados: dict) -> int:
    """Salva a transação financeira na tabela 'transactions' e retorna o ID inserido."""
    query = """
        INSERT INTO transactions (telegram_id, tipo, valor, categoria, descricao)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id;
    """
    
    with obter_conexao() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute(query, (
                str(telegram_id),
                dados["tipo"],
                dados["valor"],
                dados["categoria"],
                dados["descricao"]
            ))
            novo_id = cursor.fetchone()[0]
            conexao.commit()
            return novo_id
        
# ==========================================
# HANDLERS DO BOT DO TELEGRAM
# ==========================================
async def responder_consultoria(texto_usuario: str) -> str:
    texto_limpo = texto_usuario.strip().lower()

    saudacoes = ["olá", "ola", "oi", "oii", "oiii", "bom dia", "boa tarde", "boa noite", "tudo bem", "tudo bem?"]
    if texto_limpo in saudacoes:
        return (
            "Olá! 👋 Sou o **Fincash**, o seu assistente financeiro pessoal.\n\n"
            "Estou aqui para te ajudar a registrar gastos, organizar as suas contas e dar dicas de economia.\n\n"
            "Como posso ajudar hoje? 😊"
        )

    prompt = f"""
    Você é o Fincash, um assistente e consultor financeiro pessoal focado EXCLUSIVAMENTE em finanças, economia, investimentos, planejamento orçamentário, controle de gastos e dicas de economia doméstica.

    Sua tarefa é analisar o texto do usuário: "{texto_usuario}"

    REGRA DE SEGURANÇA E ESCOPO:
    1. Se a mensagem for sobre finanças, economia, dinheiro, cortes de gastos, investimentos ou tópicos correlatos:
       - Responda de forma motivadora, clara, prática e amigável em português.
       - Use formatação Markdown (negrito, listas) e emojis.

    2. Se a mensagem for sobre QUALQUER OUTRO ASSUNTO fora do escopo financeiro:
       - Responda EXATAMENTE e APENAS com a seguinte frase:
         "⚠️ Esta mensagem vai contra as diretrizes do nosso sistema. Caso precise de ajuda ou suporte, entre em contato com nosso SAC através do e-mail: aaa@aaa"
    """

    resposta = cliente_groq.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return resposta.choices[0].message.content.strip()

import re

async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    primeiro_nome = update.effective_user.first_name or "Usuário"

    garantir_usuario_existe(user_id, primeiro_nome)
    texto_usuario = update.message.text.strip()
    texto_lower = texto_usuario.lower()

    # 1. INTERCEPTADOR DE DELEÇÃO/EXCLUSÃO (Texto livre)
    padrao_exclusao = r'^(?:apagar|deletar|excluir|remover)\s+(?:id\s*|#\s*)?(\d+)$'
    match_exclusao = re.match(padrao_exclusao, texto_lower)

    if match_exclusao:
        id_deletar = int(match_exclusao.group(1))
        sucesso = deletar_transacao_por_id(user_id, id_deletar)

        if sucesso:
            await update.message.reply_text(
                f"🗑️ Lançamento **#{id_deletar:02d}** excluído com sucesso!", 
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"❌ Nenhum lançamento encontrado com o ID **#{id_deletar:02d}** na sua conta.", 
                parse_mode="Markdown"
            )
        return

    # 2. INTERCEPTADOR DE RESUMO
    termos_resumo = ["quanto eu gastei", "quanto gastei", "quanto eu tenho", "meu saldo", "resumo de gastos", "quanto já gastei"]
    if any(termo in texto_lower for termo in termos_resumo):
        await comando_resumo(update, context)
        return

    # 3. INTERPRETAÇÃO DE LANÇAMENTO FINANCEIRO COM A GROQ
    try:
        dados = interpretar_mensagem_financeira(texto_usuario)

        if dados.get("valor") and float(dados["valor"]) > 0:
            transacao_id = salvar_transacao(user_id, dados)
            id_formatado = f"{transacao_id:02d}"
            tipo_str = "🟢 Renda" if dados["tipo"] == "renda" else "🔴 Despesa"
            
            resposta = (
                f"✅ **Lançamento Registrado!**\n\n"
                f"🆔 **ID:** {id_formatado}\n"
                f"📌 **Descrição:** {dados['descricao']}\n"
                f"💰 **Valor:** R$ {dados['valor']:.2f}\n"
                f"🏷️ **Categoria:** {dados['categoria']}\n"
                f"📊 **Tipo:** {tipo_str}"
            )
            await update.message.reply_text(resposta, parse_mode="Markdown")
            return

    except Exception:
        pass

    # 4. CONSULTORIA FINANCEIRA COM A GROQ
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )
        resposta_consultoria = await responder_consultoria(texto_usuario)
        await update.message.reply_text(resposta_consultoria)

    except Exception as e:
        print(f"Erro no processamento de mensagem: {e}")
        await update.message.reply_text(
            "❌ Ocorreu um erro ao processar sua solicitação."
        )

async def comando_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    conexao = obter_conexao()
    cursor = conexao.cursor()

    cursor.execute("""
        SELECT COALESCE(SUM(valor), 0) 
        FROM transactions 
        WHERE telegram_id = %s AND tipo = 'renda';
    """, (user_id,))
    total_renda = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(SUM(valor), 0) 
        FROM transactions 
        WHERE telegram_id = %s AND tipo = 'despesa';
    """, (user_id,))
    total_despesas = cursor.fetchone()[0]

    cursor.execute("""
        SELECT categoria, SUM(valor) 
        FROM transactions 
        WHERE telegram_id = %s AND tipo = 'despesa'
        GROUP BY categoria
        ORDER BY SUM(valor) DESC;
    """, (user_id,))
    categorias = cursor.fetchall()

    cursor.close()
    conexao.close()

    saldo = total_renda - total_despesas

    if saldo < 0:
        alerta_saldo = "⚠️ **Atenção:** Seus gastos superaram a sua renda!"
    else:
        alerta_saldo = "✅ **Saúde Financeira:** Saldo positivo!"

    texto_categorias = "\n".join(
        [f"• **{cat}:** R$ {val:.2f}" for cat, val in categorias]
    )
    if not texto_categorias:
        texto_categorias = "Nenhuma despesa registrada."

    mensagem = (
        f"📊 **SEU BALANÇO FINANCEIRO**\n\n"
        f"💵 **Total Recebido (Renda):** R$ {total_renda:.2f}\n"
        f"💸 **Total Gasto (Despesas):** R$ {total_despesas:.2f}\n"
        f"⚖️ **Saldo Atual:** R$ {saldo:.2f}\n\n"
        f"{alerta_saldo}\n\n"
        f"📁 **Gastos por Categoria:**\n"
        f"{texto_categorias}"
    )

    await update.message.reply_text(mensagem, parse_mode="Markdown")


# ==========================================
# COMANDOS ADICIONAIS DO BOT
# ==========================================
async def comando_deletar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    
    if not context.args:
        await update.message.reply_text("⚠️ **Uso correto:** `/deletar 01` ou `/deletar 5`", parse_mode="Markdown")
        return

    try:
        id_deletar = int(context.args[0].replace("#", "").strip())
    except ValueError:
        await update.message.reply_text("❌ Informe um número de ID válido.")
        return

    sucesso = deletar_transacao_por_id(user_id, id_deletar)

    if sucesso:
        await update.message.reply_text(f"🗑️ Lançamento **#{id_deletar:02d}** excluído com sucesso!", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Nenhum lançamento encontrado com o ID **#{id_deletar:02d}** na sua conta.", parse_mode="Markdown")


# ==========================================
# INICIALIZAÇÃO DA APLICAÇÃO (BOT + WEB)
# ==========================================
if __name__ == "__main__":
    # 1. Cria/atualiza a estrutura das tabelas na base de dados
    criar_tabelas()

    # 2. Inicia o servidor Web Flask numa thread separada
    thread_web = threading.Thread(target=rodar_flask)
    thread_web.daemon = True
    thread_web.start()
    print("🌐 Servidor Web rodando em: http://127.0.0.1:5000")

    # 3. Configura e inicia o Bot do Telegram
    token_telegram = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token_telegram:
        print("❌ ERRO: TELEGRAM_BOT_TOKEN não configurado no arquivo .env")
    else:
        app = ApplicationBuilder().token(token_telegram).build()

        # Registra os comandos do bot
        app.add_handler(CommandHandler("resumo", comando_resumo))
        app.add_handler(CommandHandler("deletar", comando_deletar))
        app.add_handler(CommandHandler("apagar", comando_deletar))
        app.add_handler(CommandHandler("excluir", comando_deletar))

        # Registra o processador de mensagens de texto comum
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, processar_mensagem)
        )

        print("🚀 Bot rodando com sucesso! Aguardando mensagens...")
        app.run_polling()