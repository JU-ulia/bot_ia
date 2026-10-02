import os
import json
import re
import psycopg2
from groq import Groq
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from werkzeug.security import generate_password_hash, check_password_hash

# IMPORTAÇÕES PARA O FLASK, SESSÃO E E-MAIL
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import threading 
from flask_mail import Mail, Message

from email_validator import validate_email, EmailNotValidError
import smtplib
import traceback
import time
import imaplib
from email import message_from_bytes
from datetime import datetime, timedelta

# Carrega as variáveis de ambiente (.env)
load_dotenv()

# Inicializa a aplicação Flask
app_web = Flask(__name__)
app_web.secret_key = os.getenv("FLASK_SECRET_KEY", "fincash_secret_key_12345")
CORS(app_web)

# CONFIGURAÇÕES DE E-MAIL:
app_web.config['MAIL_SERVER'] = 'smtp.gmail.com'
app_web.config['MAIL_PORT'] = 587
app_web.config['MAIL_USE_TLS'] = True
app_web.config['MAIL_USERNAME'] = os.getenv('EMAIL_USER', 'seu_email@gmail.com')
app_web.config['MAIL_PASSWORD'] = os.getenv('EMAIL_PASS', 'sua_senha_de_app_16_digitos')
app_web.config['MAIL_DEFAULT_SENDER'] = ('Fincash Suporte', app_web.config['MAIL_USERNAME'])

mail = Mail(app_web)

# ==========================================
# E-MAILS: BOAS-VINDAS, AVISO AO SUPORTE E DETECÇÃO DE E-MAIL INVÁLIDO
# ==========================================
SUPORTE_EMAIL = os.getenv('SUPORTE_EMAIL', app_web.config['MAIL_USERNAME'])


def montar_email_boas_vindas(nome, email, telefone):
    msg = Message(
        subject="Seu e-mail foi vinculado ao Fincash!",
        recipients=[email]
    )
    msg.body = f"Olá, {nome}!\n\nSua conta no Fincash foi criada com sucesso.\nPara ativar seus lançamentos pelo Telegram, acesse o bot e compartilhe seu número de telefone registrado ({telefone})."
    msg.html = f"""
    <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
        <h2 style="color: #2563eb; text-align: center;">fincash</h2>
        <hr style="border: 0; border-top: 1px solid #eee;">
        <p>Olá, <strong>{nome}</strong>!</p>
        <p>Seu e-mail (<strong>{email}</strong>) foi vinculado com sucesso à sua conta no <strong>Fincash</strong>.</p>
        <p>Para ativar seus lançamentos no Telegram, abra o bot e envie o seu número de telefone registrado: <strong>{telefone}</strong>.</p>
        <div style="background-color: #f8fafc; padding: 15px; border-left: 4px solid #ef4444; margin: 20px 0; border-radius: 4px;">
            <p style="margin: 0; font-size: 14px; color: #475569;">
                ⚠️ <strong>Caso não tenha sido você:</strong><br>
                Entre em contato com nosso SAC através do e-mail:
                <a href="mailto:123@fincash.com" style="color: #ef4444; font-weight: bold;">123@fincash.com</a>
            </p>
        </div>
    </div>
    """
    return msg


def marcar_email_invalido(email_destino):
    """Marca o usuário dono desse e-mail como 'e-mail inválido' (aparece o lembrete no main.html)."""
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                UPDATE users SET email_invalido = TRUE
                WHERE LOWER(email) = LOWER(%s) AND COALESCE(email_invalido, FALSE) = FALSE;
            """, (email_destino,))
            if cursor.rowcount:
                print(f"⚠ E-mail marcado como inválido: {email_destino}")
        conexao.commit()
    except Exception:
        conexao.rollback()
        traceback.print_exc()
    finally:
        conexao.close()


def notificar_suporte_email_invalido(email_errado):
    """Avisa o suporte quando o SMTP recusa o endereço na hora (nesse caso o Gmail não gera bounce)."""
    try:
        aviso = Message(
            subject="⚠ E-mail inválido no cadastro do Fincash",
            recipients=[SUPORTE_EMAIL],
            body=f"O endereço {email_errado} foi recusado pelo servidor de e-mail (não encontrado)."
        )
        mail.send(aviso)
    except Exception:
        traceback.print_exc()


def enviar_email_async(app, msg):
    """Envia em segundo plano. Se o endereço for recusado, marca o usuário e avisa o suporte."""
    with app.app_context():
        destino = msg.recipients[0]
        try:
            mail.send(msg)
            print(f"✅ E-mail enviado para {destino}")
        except smtplib.SMTPRecipientsRefused:
            print(f"⚠ E-mail recusado pelo servidor: {destino}")
            marcar_email_invalido(destino)
            notificar_suporte_email_invalido(destino)
        except Exception:
            traceback.print_exc()


def enviar_boas_vindas(nome, email, telefone):
    """Dispara UM único e-mail de boas-vindas em segundo plano."""
    msg = montar_email_boas_vindas(nome, email, telefone)
    threading.Thread(target=enviar_email_async, args=(app_web, msg), daemon=True).start()


# --- Detecção de bounce: lê a caixa do suporte (IMAP) e acha e-mails devolvidos ---
MESES_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def extrair_destinatarios_falhos(msg):
    falhos = msg.get('X-Failed-Recipients')
    if falhos:
        return [e.strip().lower() for e in falhos.split(',') if e.strip()]

    achados = set()
    for parte in msg.walk():
        if parte.get_content_type() != 'text/plain':
            continue
        try:
            texto = parte.get_payload(decode=True).decode(parte.get_content_charset() or 'utf-8', errors='ignore')
        except Exception:
            continue
        for e in re.findall(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', texto):
            e = e.lower()
            if 'mailer-daemon' not in e and e != app_web.config['MAIL_USERNAME'].lower():
                achados.add(e)
    return list(achados)


def verificar_bounces():
    imap = None
    try:
        imap = imaplib.IMAP4_SSL('imap.gmail.com')
        imap.login(app_web.config['MAIL_USERNAME'], app_web.config['MAIL_PASSWORD'])
        imap.select('INBOX', readonly=True)  # só lê: não marca nada como lido para o suporte

        d = datetime.now() - timedelta(days=2)
        desde = f"{d.day:02d}-{MESES_IMAP[d.month - 1]}-{d.year}"
        status, ids = imap.search(None, 'FROM', '"mailer-daemon"', 'SINCE', desde)
        if status != 'OK':
            return

        for num in ids[0].split():
            status, data = imap.fetch(num, '(BODY.PEEK[])')
            if status != 'OK' or not data or not data[0]:
                continue
            msg = message_from_bytes(data[0][1])
            for falho in extrair_destinatarios_falhos(msg):
                marcar_email_invalido(falho)  # só afeta quem tem esse e-mail cadastrado
    except Exception:
        traceback.print_exc()
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


def loop_verificar_bounces():
    while True:
        verificar_bounces()
        time.sleep(120)


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
        # 1. Tabela de Usuários (telegram_id opcional no cadastro, telefone único)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT UNIQUE,
            nome TEXT NOT NULL,
            telefone VARCHAR(20) UNIQUE,
            senha TEXT NOT NULL,
            email TEXT UNIQUE,
            data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Garante a coluna telefone em bancos já existentes
        cursor.execute("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS telefone VARCHAR(20) UNIQUE;
        """)

        # 2. Tabela de Transações
        # Na função criar_tabelas():
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT REFERENCES users(telegram_id),
            tipo VARCHAR(10) NOT NULL,
            valor NUMERIC(10, 2) NOT NULL,
            categoria VARCHAR(50),
            descricao TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- Alterado de DATE para TIMESTAMP
        );
        """)

        # 3. Tabela de Categorias
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT REFERENCES users(telegram_id),
            nome TEXT NOT NULL
        );
        """)

        # 4. TABELA DE AUDITORIA E HISTÓRICO DE DADOS MANIPULADOS
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS dados_salvos (
            id SERIAL PRIMARY KEY,
            user_id INT REFERENCES users(id),
            tipo_operacao VARCHAR(50) NOT NULL,
            dados_antigos JSONB,
            dados_novos JSONB,
            data_operacao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 5. BACKUP DE LANÇAMENTOS EXCLUÍDOS (sem FK, para sobreviver a qualquer limpeza)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions_backup (
            id SERIAL PRIMARY KEY,
            id_original INT NOT NULL,
            telegram_id TEXT,
            tipo VARCHAR(10) NOT NULL,
            valor NUMERIC(10, 2) NOT NULL,
            categoria VARCHAR(50),
            descricao TEXT,
            data TIMESTAMP,
            data_exclusao TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dados_salvos_user_id 
        ON dados_salvos(user_id);
        """)

        # Garantia de Migração: Se a tabela 'users' já existia antes sem 'data_cadastro'
        cursor.execute("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS data_cadastro TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
        """)

        # Marca usuários cujo e-mail foi devolvido (digitado errado)
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS email_invalido BOOLEAN DEFAULT FALSE;
        """)

        conexao.commit()
        print("Tabelas e índices sincronizados com sucesso!")

    except Exception as e:
        print(f"Erro ao criar/atualizar tabelas: {e}")
        conexao.rollback()

    finally:
        cursor.close()
        conexao.close()

# Executa a criação das tabelas ao iniciar a aplicação
criar_tabelas()

def deletar_transacao_por_id(telegram_id: str, id_transacao: int) -> bool:
    """Move o lançamento para transactions_backup e o remove de transactions (tudo na mesma transação).
    Retorna True se encontrou e removeu."""
    telegram_id = str(telegram_id).strip()
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                WITH removidos AS (
                    DELETE FROM transactions
                    WHERE id = %s AND TRIM(telegram_id) = %s
                    RETURNING id, telegram_id, tipo, valor, categoria, descricao, data
                )
                INSERT INTO transactions_backup
                    (id_original, telegram_id, tipo, valor, categoria, descricao, data)
                SELECT id, telegram_id, tipo, valor, categoria, descricao, data
                FROM removidos;
            """, (id_transacao, telegram_id))
            movidos = cursor.rowcount

            if movidos == 0:
                # Diagnóstico: o ID existe com outro dono? ou não existe?
                cursor.execute("SELECT telegram_id FROM transactions WHERE id = %s;", (id_transacao,))
                row = cursor.fetchone()
                if row:
                    print(f"⚠️ Exclusão #{id_transacao}: existe, mas pertence a telegram_id={row[0]!r}; "
                          f"quem pediu foi {telegram_id!r}")
                else:
                    print(f"⚠️ Exclusão #{id_transacao}: ID não existe na tabela transactions")

        conexao.commit()
        return movidos > 0
    except Exception as e:
        conexao.rollback()
        print(f"❌ Erro ao excluir lançamento #{id_transacao}: {e}")
        raise
    finally:
        conexao.close()

# ==========================================
# ROTAS DA API E INTERFACE WEB (FLASK)
# ==========================================

# 1. Rota Principal / Dashboard
@app_web.route('/')
def index():
    if 'telegram_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('main.html')

# 2. Rota Login Page
@app_web.route('/login')
def login_page():
    if 'telegram_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')

# 3. API Login
@app_web.route('/api/login', methods=['POST'])
def handle_login():
    dados = request.get_json() or {}
    identifier = str(dados.get('identifier', '')).strip()
    password = str(dados.get('password', '')).strip()

    if not identifier or not password:
        return jsonify({"erro": "Informe seu Telefone, E-mail ou Telegram ID e a senha."}), 400

    # Telefone digitado com máscara "(11) 99999-9999" vira "11999999999" (igual ao cadastro)
    telefone_limpo = identifier.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    conexao = obter_conexao()
    cursor = conexao.cursor()

    # Procura o utilizador por E-mail, Telefone ou Telegram ID
    cursor.execute("""
        SELECT telegram_id, nome, senha, id 
        FROM users 
        WHERE LOWER(email) = LOWER(%s) OR telefone = %s OR telegram_id = %s;
    """, (identifier, telefone_limpo, identifier))
    user = cursor.fetchone()

    cursor.close()
    conexao.close()

    if user:
        db_telegram_id, db_nome, db_senha_hash, db_user_id = user

        if db_senha_hash and check_password_hash(db_senha_hash, password):
            session['telegram_id'] = db_telegram_id
            session['user_id'] = db_user_id
            session['user_name'] = db_nome
            return jsonify({"sucesso": True, "mensagem": "Login efetuado!"})
        else:
            return jsonify({"erro": "Senha incorreta."}), 401
    else:
        return jsonify({"erro": "Usuário não encontrado."}), 404

# 4. API Cadastro
@app_web.route('/api/cadastrar', methods=['POST'])
def handle_register():
    dados = request.get_json() or {}
    nome = str(dados.get('name', '')).strip()
    email = str(dados.get('email', '')).strip()
    telefone = str(dados.get('phone', '')).replace(" ", "").replace("-", "").replace("(", "").replace(")", "").strip()
    password = str(dados.get('password', '')).strip()

    # 1. Validações básicas
    if not nome or not email or not telefone or not password:
        return jsonify({"erro": "Todos os campos são obrigatórios."}), 400

    # E-mail: formato válido + domínio existe (pega 'joao@gmial.con')
    try:
        email = validate_email(email, check_deliverability=True).normalized
    except EmailNotValidError:
        return jsonify({"erro": "E-mail inválido ou domínio inexistente. Confira se digitou corretamente."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()

    try:
        # 2. Verificar se E-mail ou Telefone já existem
        cursor.execute("SELECT 1 FROM users WHERE email = %s OR telefone = %s;", (email, telefone))
        if cursor.fetchone():
            return jsonify({"erro": "Este E-mail ou Telefone já está cadastrado."}), 400

        # 3. Gerar Hash da Senha e inserir (telegram_id fica NULL até o 1º acesso no bot)
        senha_hash = generate_password_hash(password)
        cursor.execute("""
            INSERT INTO users (nome, email, telefone, senha)
            VALUES (%s, %s, %s, %s);
        """, (nome, email, telefone, senha_hash))
        conexao.commit()
    except Exception as e:
        conexao.rollback()
        return jsonify({"erro": f"Erro de banco de dados: {e}"}), 500
    finally:
        cursor.close()
        conexao.close()

    # 4. Um único e-mail de boas-vindas (em segundo plano)
    enviar_boas_vindas(nome, email, telefone)

    return jsonify({"sucesso": True, "mensagem": "Conta criada com sucesso! Agora converse com o Bot no Telegram para ativar."})

# 4.1 API: status do e-mail (o main.html usa para mostrar o lembrete)
@app_web.route('/api/email-status', methods=['GET'])
def email_status():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"erro": "Não autenticado"}), 401

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT email, COALESCE(email_invalido, FALSE) FROM users WHERE id = %s;", (user_id,))
            row = cursor.fetchone()
    finally:
        conexao.close()

    if not row:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    return jsonify({"email": row[0], "email_invalido": bool(row[1])})

# 4.2 API: corrigir o e-mail (guarda o antigo em dados_salvos)
@app_web.route('/api/atualizar-email', methods=['POST'])
def atualizar_email():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"erro": "Não autenticado"}), 401

    dados = request.get_json() or {}
    novo_email = str(dados.get('email', '')).strip()

    try:
        novo_email = validate_email(novo_email, check_deliverability=True).normalized
    except EmailNotValidError:
        return jsonify({"erro": "E-mail inválido ou domínio inexistente. Confira e tente de novo."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()
    try:
        cursor.execute("SELECT nome, telefone, email FROM users WHERE id = %s FOR UPDATE;", (user_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"erro": "Usuário não encontrado."}), 404
        nome, telefone, email_antigo = row

        if email_antigo and email_antigo.lower() == novo_email.lower():
            return jsonify({"erro": "Esse já é o e-mail cadastrado. Digite o e-mail correto."}), 400

        cursor.execute("SELECT 1 FROM users WHERE LOWER(email) = LOWER(%s) AND id <> %s;", (novo_email, user_id))
        if cursor.fetchone():
            return jsonify({"erro": "Este e-mail já está em uso por outra conta."}), 400

        cursor.execute("UPDATE users SET email = %s, email_invalido = FALSE WHERE id = %s;", (novo_email, user_id))

        # Histórico: o e-mail errado continua guardado na tabela de auditoria
        cursor.execute("""
            INSERT INTO dados_salvos (user_id, tipo_operacao, dados_antigos, dados_novos)
            VALUES (%s, %s, %s, %s);
        """, (user_id, 'EDICAO_EMAIL', json.dumps({"email": email_antigo}), json.dumps({"email": novo_email})))

        conexao.commit()
    except Exception as e:
        conexao.rollback()
        print(f"Erro ao atualizar e-mail: {e}")
        return jsonify({"erro": "Não foi possível atualizar o e-mail agora."}), 500
    finally:
        cursor.close()
        conexao.close()

    enviar_boas_vindas(nome, novo_email, telefone)
    return jsonify({"sucesso": True, "email": novo_email})

# 4.3 Página de perfil
@app_web.route('/perfil')
def perfil_page():
    # Sessão antiga (de antes do user_id existir): limpa e pede login de novo
    if 'user_id' not in session:
        session.clear()
        return redirect(url_for('login_page'))
    return render_template('perfil.html')

def limpar_telefone(valor):
    return str(valor).replace(" ", "").replace("-", "").replace("(", "").replace(")", "").strip()

# 4.4 API: dados do perfil
@app_web.route('/api/perfil', methods=['GET'])
def obter_perfil():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"erro": "Não autenticado"}), 401

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT nome, email, telefone, telegram_id,
                       TO_CHAR(data_cadastro, 'DD/MM/YYYY'),
                       COALESCE(email_invalido, FALSE)
                FROM users WHERE id = %s;
            """, (user_id,))
            row = cursor.fetchone()
    finally:
        conexao.close()

    if not row:
        return jsonify({"erro": "Usuário não encontrado"}), 404

    return jsonify({
        "nome": row[0],
        "email": row[1],
        "telefone": row[2],
        "telegram_vinculado": bool(row[3]),
        "data_cadastro": row[4],
        "email_invalido": bool(row[5])
    })

# 4.5 API: editar nome, e-mail e telefone
@app_web.route('/api/perfil', methods=['PUT'])
def atualizar_perfil():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"erro": "Não autenticado"}), 401

    dados = request.get_json() or {}
    novo_nome = str(dados.get('nome', '')).strip()
    novo_email = str(dados.get('email', '')).strip()
    novo_telefone = limpar_telefone(dados.get('telefone', ''))

    if not novo_nome or not novo_email or not novo_telefone:
        return jsonify({"erro": "Preencha nome, e-mail e telefone."}), 400
    if len(novo_nome) > 100:
        return jsonify({"erro": "O nome pode ter no máximo 100 caracteres."}), 400
    if not re.fullmatch(r'\+?\d{10,15}', novo_telefone):
        return jsonify({"erro": "Telefone inválido. Use DDD + número, ex: 11999999999."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()
    try:
        cursor.execute("SELECT nome, email, telefone FROM users WHERE id = %s FOR UPDATE;", (user_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"erro": "Usuário não encontrado."}), 404
        nome_atual, email_atual, telefone_atual = row

        email_mudou = (email_atual or '').lower() != novo_email.lower()
        if email_mudou:
            try:
                novo_email = validate_email(novo_email, check_deliverability=True).normalized
            except EmailNotValidError:
                return jsonify({"erro": "E-mail inválido ou domínio inexistente. Confira e tente de novo."}), 400
            cursor.execute("SELECT 1 FROM users WHERE LOWER(email) = LOWER(%s) AND id <> %s;", (novo_email, user_id))
            if cursor.fetchone():
                return jsonify({"erro": "Este e-mail já está em uso por outra conta."}), 400
        else:
            novo_email = email_atual

        if novo_telefone != telefone_atual:
            cursor.execute("SELECT 1 FROM users WHERE telefone = %s AND id <> %s;", (novo_telefone, user_id))
            if cursor.fetchone():
                return jsonify({"erro": "Este telefone já está em uso por outra conta."}), 400

        # Só o que mudou vai para o histórico (dados_salvos)
        antigos, novos = {}, {}
        if novo_nome != nome_atual:
            antigos['nome'], novos['nome'] = nome_atual, novo_nome
        if email_mudou:
            antigos['email'], novos['email'] = email_atual, novo_email
        if novo_telefone != telefone_atual:
            antigos['telefone'], novos['telefone'] = telefone_atual, novo_telefone

        if not novos:
            return jsonify({"sucesso": True, "alterado": False, "mensagem": "Nenhuma alteração para salvar."})

        cursor.execute("""
            UPDATE users
            SET nome = %s, email = %s, telefone = %s,
                email_invalido = CASE WHEN %s THEN FALSE ELSE COALESCE(email_invalido, FALSE) END
            WHERE id = %s;
        """, (novo_nome, novo_email, novo_telefone, email_mudou, user_id))

        cursor.execute("""
            INSERT INTO dados_salvos (user_id, tipo_operacao, dados_antigos, dados_novos)
            VALUES (%s, %s, %s, %s);
        """, (user_id, 'EDICAO_PERFIL', json.dumps(antigos), json.dumps(novos)))

        conexao.commit()
    except Exception as e:
        conexao.rollback()
        print(f"Erro ao atualizar perfil: {e}")
        return jsonify({"erro": "Não foi possível salvar agora. Tente novamente."}), 500
    finally:
        cursor.close()
        conexao.close()

    session['user_name'] = novo_nome
    if email_mudou:
        enviar_boas_vindas(novo_nome, novo_email, novo_telefone)

    return jsonify({"sucesso": True, "alterado": True, "mensagem": "Dados atualizados com sucesso!"})

# 4.6 API: alterar senha (exige a senha atual)
@app_web.route('/api/perfil/senha', methods=['POST'])
def alterar_senha():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"erro": "Não autenticado"}), 401

    dados = request.get_json() or {}
    senha_atual = str(dados.get('senha_atual', '')).strip()
    nova_senha = str(dados.get('nova_senha', '')).strip()
    confirmar = str(dados.get('confirmar', '')).strip()

    if not senha_atual or not nova_senha or not confirmar:
        return jsonify({"erro": "Preencha todos os campos de senha."}), 400
    if len(nova_senha) < 6:
        return jsonify({"erro": "A nova senha precisa ter pelo menos 6 caracteres."}), 400
    if nova_senha != confirmar:
        return jsonify({"erro": "A confirmação não é igual à nova senha."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()
    try:
        cursor.execute("SELECT senha FROM users WHERE id = %s FOR UPDATE;", (user_id,))
        row = cursor.fetchone()
        if not row or not row[0] or not check_password_hash(row[0], senha_atual):
            return jsonify({"erro": "Senha atual incorreta."}), 400
        if check_password_hash(row[0], nova_senha):
            return jsonify({"erro": "A nova senha precisa ser diferente da atual."}), 400

        cursor.execute("UPDATE users SET senha = %s WHERE id = %s;", (generate_password_hash(nova_senha), user_id))

        # Histórico sem guardar nenhuma senha/hash
        cursor.execute("""
            INSERT INTO dados_salvos (user_id, tipo_operacao, dados_antigos, dados_novos)
            VALUES (%s, %s, %s, %s);
        """, (user_id, 'EDICAO_SENHA', json.dumps({"campo": "senha"}), json.dumps({"campo": "senha", "alterada": True})))

        conexao.commit()
    except Exception as e:
        conexao.rollback()
        print(f"Erro ao alterar senha: {e}")
        return jsonify({"erro": "Não foi possível alterar a senha agora."}), 500
    finally:
        cursor.close()
        conexao.close()

    return jsonify({"sucesso": True, "mensagem": "Senha alterada com sucesso!"})

# 5. Logout
@app_web.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

def obter_telegram_id_atual():
    """Busca o telegram_id ATUAL no banco.
    O valor guardado na sessão é uma foto do momento do login: se a pessoa entrou no site
    antes de vincular o Telegram, ele fica vazio para sempre. Aqui sempre consultamos o banco."""
    user_id = session.get('user_id')
    if user_id:
        conexao = obter_conexao()
        try:
            with conexao.cursor() as cursor:
                cursor.execute("SELECT telegram_id FROM users WHERE id = %s;", (user_id,))
                row = cursor.fetchone()
        finally:
            conexao.close()
        if row and row[0]:
            session['telegram_id'] = row[0]  # mantém a sessão em dia
            return str(row[0]).strip()
        return None

    tid = session.get('telegram_id')
    return str(tid).strip() if tid else None

# 6. API: Usuário Atual
@app_web.route('/api/usuario-atual', methods=['GET'])
def obter_usuario_atual():
    if 'telegram_id' not in session:
        return jsonify({"erro": "Não autenticado"}), 401
    return jsonify({
        "id": session.get('user_id'),
        "telegram_id": obter_telegram_id_atual(),
        "nome": session.get('user_name', 'Usuário')
    })

# 7. API: Data de Cadastro
@app_web.route('/api/usuario-data-cadastro', methods=['GET'])
def obter_data_cadastro():
    if 'telegram_id' not in session:
        return jsonify({"erro": "Não autenticado"}), 401
    
    user_id = session.get('user_id')
    telegram_id = obter_telegram_id_atual()
    conexao = obter_conexao()
    cursor = conexao.cursor()

    if user_id:
        cursor.execute("""
            SELECT TO_CHAR(COALESCE(data_cadastro, CURRENT_TIMESTAMP), 'YYYY-MM-DD') 
            FROM users 
            WHERE id = %s;
        """, (user_id,))
    else:
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

# 8. API: Datas com Transações
@app_web.route('/api/datas-com-transacoes', methods=['GET'])
def datas_com_transacoes():
    if 'telegram_id' not in session:
        return jsonify([]), 401
    
    telegram_id = obter_telegram_id_atual()
    if not telegram_id:
        return jsonify([])
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

# 9. API: Dados do Dashboard
@app_web.route('/api/dados-dashboard/<telegram_id>', methods=['GET'])
def obter_dados_dashboard(telegram_id):
    try:
        # Só quem está logado vê dados (antes, qualquer ID na URL era aceito sem sessão)
        if 'telegram_id' not in session:
            return jsonify({"erro": "Não autenticado"}), 401

        # telegram_id sempre lido do banco: reflete o vínculo feito no Telegram depois do login
        vinculado = obter_telegram_id_atual()
        telegram_id = vinculado or ''   # sem vínculo: nenhum lançamento casa

        mes_filtro = request.args.get('mes', None)

        conexao = obter_conexao()
        cursor = conexao.cursor()

        if mes_filtro:
            condicao_data = "AND TO_CHAR(data, 'YYYY-MM') = %s"
            params_base = (telegram_id, mes_filtro)
        else:
            condicao_data = ""
            params_base = (telegram_id,)

        # Totais
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

        # Poupança
        cursor.execute("""
            SELECT COALESCE(SUM(valor), 0)
            FROM transactions
            WHERE telegram_id = %s AND (categoria ILIKE 'Poupança' OR categoria ILIKE 'Investimentos');
        """, (telegram_id,))
        poupanca_row = cursor.fetchone()
        total_poupanca = float(poupanca_row[0]) if poupanca_row else 0.0

        # Categoria
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

        # Nome
        if session.get('user_id'):
            cursor.execute("SELECT nome FROM users WHERE id = %s;", (session['user_id'],))
        else:
            cursor.execute("SELECT nome FROM users WHERE telegram_id = %s;", (telegram_id,))
        user_row = cursor.fetchone()
        nome_usuario = user_row[0] if user_row else "Usuário"

        # Última Atividade
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
                "ultima_atividade": ultima_atividade,
                "vinculado": bool(vinculado)
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
def interpretar_mensagem_financeira(texto_usuario: str) -> dict:
    """Envia o texto para a Groq (Llama 3) e retorna um dicionário JSON estruturado."""
    prompt = f"""
    Você é um assistente financeiro especialista em extrair lançamentos de texto livre.
    Analise o texto a seguir: "{texto_usuario}"

    CATEGORIAS DE DESPESA:
    - Alimentação, Carro, Internet, Moradia, Lazer, Outros.

    CATEGORIAS DE RENDA:
    - Salário, Freelance, Investimentos, Outras Rendas.

    Retorne APENAS o JSON no formato:
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

def autenticar_e_vincular_telegram(identifier: str, password: str, novo_telegram_id: str) -> tuple[bool, str]:
    """Valida credenciais e vincula o novo Telegram ID."""
    conexao = None
    cursor = None
    try:
        conexao = obter_conexao()
        cursor = conexao.cursor()

        # Busca por e-mail, telefone ou telegram_id
        cursor.execute("""
            SELECT id, nome, email, senha, telegram_id 
            FROM users 
            WHERE LOWER(email) = LOWER(%s) 
               OR telefone = %s 
               OR (telegram_id IS NOT NULL AND telegram_id = %s);
        """, (identifier, identifier, identifier))
        
        user = cursor.fetchone()

        if not user:
            return False, "Usuário não encontrado"

        user_id, db_nome, db_email, db_senha_hash, antigo_telegram_id = user

        if not db_senha_hash or not check_password_hash(db_senha_hash, password):
            return False, "Senha incorreta"

        if antigo_telegram_id == novo_telegram_id:
            return True, db_nome

        # Atualiza no banco de dados
        cursor.execute("UPDATE users SET telegram_id = %s WHERE id = %s;", (novo_telegram_id, user_id))
        conexao.commit()
        
        return True, db_nome

    except Exception as e:
        if conexao:
            conexao.rollback()
        print(f"❌ Erro no banco durante o login: {e}")
        return False, str(e)
    finally:
        if cursor:
            cursor.close()
        if conexao:
            conexao.close()
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
    Você é o Fincash, assistente financeiro pessoal.
    Análise a mensagem: "{texto_usuario}"

    1. Se for sobre finanças/economia: responda de forma prática em português com Markdown.
    2. Se for sobre outro assunto: responda EXATAMENTE:
       "⚠️ Esta mensagem vai contra as diretrizes do nosso sistema. Caso precise de ajuda ou suporte, entre em contato com nosso SAC através do e-mail: aaa@aaa"
    """

    resposta = cliente_groq.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return resposta.choices[0].message.content.strip()

async def processar_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = str(update.effective_user.id)
    texto_usuario = update.message.text.strip() if update.message.text else ""
    texto_lower = texto_usuario.lower()

    # =============================================================
    # 1. PERMITE O COMANDO /LOGIN (MESMO SEM ESTAR VINCULADO)
    # =============================================================
    if texto_lower.startswith("/login"):
        try:
            partes = texto_usuario.split(maxsplit=2)
            if len(partes) < 3:
                await update.message.reply_text(
                    "⚠️ **Formato incorreto!**\nUse: `/login <email_ou_telefone> <senha>`", 
                    parse_mode="Markdown"
                )
                return

            identifier = partes[1].strip()
            password = partes[2].strip()

            sucesso, resultado = autenticar_e_vincular_telegram(identifier, password, user_id)

            if sucesso:
                primeiro_nome = resultado.split()[0]
                await update.message.reply_text(
                    f"Olá, **{primeiro_nome}**! Seja bem-vindo ao **Fincash**! 🎉",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                f"❌ {resultado}"
                )
        except Exception as e:
            print(f"❌ Erro no /login: {e}")

            await update.message.reply_text(
                "❌ Ocorreu um erro ao tentar fazer login."
            )

        return
    
    # =============================================================
    # 2. PROCESSA COMPARTILHAMENTO DE CONTATO (TELEFONE)
    # =============================================================
    if update.message.contact:
        num_telefone = update.message.contact.phone_number.replace("+", "").replace(" ", "").replace("-", "").strip()
        
        conexao = obter_conexao()
        cursor = conexao.cursor()
        
        cursor.execute("""
            SELECT id, nome FROM users 
            WHERE telefone = %s OR telefone = %s OR %s LIKE '%%' || telefone;
        """, (num_telefone, num_telefone[-11:], num_telefone))
        
        user = cursor.fetchone()
        
        if user:
            user_db_id, nome = user
            cursor.execute("UPDATE users SET telegram_id = %s WHERE id = %s;", (user_id, user_db_id))
            conexao.commit()
            cursor.close()
            conexao.close()
            
            primeiro_nome = nome.split()[0]
            await update.message.reply_text(
                f"Olá, **{primeiro_nome}**! Seja bem-vindo ao **Fincash**! 🎉",
                parse_mode="Markdown"
            )
            return
        else:
            cursor.close()
            conexao.close()
            await update.message.reply_text(
                "Parece que não conseguimos acessar sua conta, tente novamente ou entre em contato com o SAC."
            )
            return

    # =============================================================
    # 3. VERIFICA SE O USUÁRIO JÁ ESTÁ VINCULADO NO BANCO
    # =============================================================
    conexao = obter_conexao()
    cursor = conexao.cursor()
    cursor.execute("SELECT telegram_id, nome FROM users WHERE telegram_id = %s;", (user_id,))
    usuario_existente = cursor.fetchone()
    cursor.close()
    conexao.close()

    # Se NÃO estiver vinculado, mostra o aviso e impede a mensagem normal
    if not usuario_existente:
        btn_contato = KeyboardButton("📱 Compartilhar meu Telefone para Ativar", request_contact=True)
        keyboard = ReplyKeyboardMarkup([[btn_contato]], resize_keyboard=True, one_time_keyboard=True)
        
        await update.message.reply_text(
            "⚠️ **Acesso Não Ativado!**\n\n"
            "Seu Telegram ainda não está vinculado ao **Fincash**.\n\n"
            "• Clique no botão abaixo para confirmar seu número de telefone registrado;\n"
            "• Ou vincule manualmente usando: `/login <email_ou_telefone> <senha>`",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return

    # =============================================================
    # 4. AÇÕES PARA USUÁRIOS JÁ AUTENTICADOS (MENSAGENS / GASTOS)
    # =============================================================
    # Interceptador de Exclusão
    padrao_exclusao = r'^(?:apagar|deletar|excluir|remover)\s+(?:id\s*|#\s*)?(\d+)$'
    match_exclusao = re.match(padrao_exclusao, texto_lower)

    if match_exclusao:
        id_deletar = int(match_exclusao.group(1))
        sucesso = deletar_transacao_por_id(user_id, id_deletar)
        if sucesso:
            await update.message.reply_text(f"🗑️ Lançamento **#{id_deletar:02d}** excluído com sucesso!", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Nenhum lançamento encontrado com o ID **#{id_deletar:02d}**.", parse_mode="Markdown")
        return

    # Interceptador de Resumo
    termos_resumo = ["quanto eu gastei", "quanto gastei", "quanto eu tenho", "meu saldo", "resumo de gastos", "quanto já gastei"]
    if any(termo in texto_lower for termo in termos_resumo):
        await comando_resumo(update, context)
        return

    # Lançamentos Financeiros e Consultoria IA
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

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        resposta_consultoria = await responder_consultoria(texto_usuario)
        await update.message.reply_text(resposta_consultoria)
    except Exception as e:
        print(f"Erro no processamento: {e}")
        await update.message.reply_text("❌ Ocorreu um erro ao processar sua solicitação.")

async def comando_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    conexao = obter_conexao()
    cursor = conexao.cursor()

    cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM transactions WHERE telegram_id = %s AND tipo = 'renda';", (user_id,))
    total_renda = cursor.fetchone()[0]

    cursor.execute("SELECT COALESCE(SUM(valor), 0) FROM transactions WHERE telegram_id = %s AND tipo = 'despesa';", (user_id,))
    total_despesas = cursor.fetchone()[0]

    cursor.execute("""
        SELECT categoria, SUM(valor) 
        FROM transactions 
        WHERE telegram_id = %s AND tipo = 'despesa'
        GROUP BY categoria ORDER BY SUM(valor) DESC;
    """, (user_id,))
    categorias = cursor.fetchall()

    cursor.close()
    conexao.close()

    saldo = total_renda - total_despesas
    alerta_saldo = "⚠️ **Atenção:** Seus gastos superaram a sua renda!" if saldo < 0 else "✅ **Saúde Financeira:** Saldo positivo!"

    texto_categorias = "\n".join([f"• **{cat}:** R$ {val:.2f}" for cat, val in categorias]) or "Nenhuma despesa registrada."

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
        await update.message.reply_text(f"❌ Nenhum lançamento encontrado com o ID **#{id_deletar:02d}**.", parse_mode="Markdown")

# ==========================================
# INICIALIZAÇÃO DA APLICAÇÃO (BOT + WEB)
# ==========================================
if __name__ == "__main__":
    criar_tabelas()

    thread_web = threading.Thread(target=rodar_flask)
    thread_web.daemon = True
    thread_web.start()
    print("🌐 Servidor Web rodando em: http://127.0.0.1:5000")

    # Verifica a caixa do suporte a cada 2 min procurando e-mails devolvidos
    threading.Thread(target=loop_verificar_bounces, daemon=True).start()

    token_telegram = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token_telegram:
        print("❌ ERRO: TELEGRAM_BOT_TOKEN não configurado no arquivo .env")
    else:
        app = ApplicationBuilder().token(token_telegram).build()

        app.add_handler(CommandHandler("resumo", comando_resumo))
        app.add_handler(CommandHandler("deletar", comando_deletar))
        app.add_handler(CommandHandler("apagar", comando_deletar))
        app.add_handler(CommandHandler("excluir", comando_deletar))
        app.add_handler(CommandHandler("login", processar_mensagem))
        
        # Registra tratador de texto E compartilhamento de contatos
        app.add_handler(
            MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, processar_mensagem)
        )

        print("🚀 Bot rodando com sucesso! Aguardando mensagens...")
        app.run_polling()