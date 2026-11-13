import os
import json
import re
import psycopg2
import asyncio
from groq import Groq
from dotenv import load_dotenv
from telegram_webhook import TelegramWebhook
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from werkzeug.security import generate_password_hash, check_password_hash

# IMPORTAÇÕES PARA O FLASK, SESSÃO E E-MAIL
from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for
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
import calendar
import math
import csv
import io
import secrets
import hashlib
from urllib.parse import urlparse

import investimentos
from html import escape as escapar_html
from utils import fmt_brl, limpar_resposta_ia, limpar_telefone, normalizar


# Carrega as variáveis de ambiente (.env)
load_dotenv()

# Inicializa a aplicação Flask
app_web = Flask(__name__)
_secret = os.getenv("FLASK_SECRET_KEY")
if not _secret:
    # Sem chave no .env: usa uma aleatória (as sessões caem a cada reinício) em vez de uma chave pública fixa.
    print("⚠ FLASK_SECRET_KEY não definida no .env: usando chave temporária. Defina uma chave fixa em produção.")
    _secret = secrets.token_hex(32)
app_web.secret_key = _secret
app_web.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',                                   # navegador não envia o cookie em POST de outros sites
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",    # defina COOKIE_SECURE=1 quando usar HTTPS
)

# O site é servido pelo próprio Flask (mesma origem): CORS só é ligado se você listar origens no .env
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    CORS(app_web, origins=_cors_origins)


@app_web.before_request
def bloquear_origem_estranha():
    """Proteção extra contra CSRF: requisições que alteram dados só valem se vierem do próprio site."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origem = request.headers.get('Origin')
        if origem and urlparse(origem).netloc != request.host and origem not in _cors_origins:
            return jsonify({"erro": "Origem não permitida."}), 403


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
SAC_EMAIL = os.getenv('SAC_EMAIL') or SUPORTE_EMAIL   # e-mail exibido aos usuários (defina SAC_EMAIL no .env)


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
                <a href="mailto:{SAC_EMAIL}" style="color: #ef4444; font-weight: bold;">{SAC_EMAIL}</a>
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
    """Conecta ao Neon ou ao PostgreSQL local."""

    database_url = os.getenv("DATABASE_URL")

    if database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
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
            valor NUMERIC(12, 2) NOT NULL,
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

        # Categorias personalizadas: cada uma é de 'despesa' ou de 'renda'
        cursor.execute("""
            ALTER TABLE categories
            ADD COLUMN IF NOT EXISTS tipo VARCHAR(10) NOT NULL DEFAULT 'despesa';
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
            valor NUMERIC(12, 2) NOT NULL,
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

        # Poupança: cada depósito/saque é um movimento (o total guardado é a soma)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS savings_movements (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT NOT NULL,
            tipo VARCHAR(10) NOT NULL CHECK (tipo IN ('deposito', 'saque')),
            valor NUMERIC(12, 2) NOT NULL CHECK (valor > 0),
            descricao TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_savings_movements_tg
        ON savings_movements(telegram_id, data);
        """)

        # Orçamento mensal por categoria (um limite por categoria e usuário)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT NOT NULL,
            categoria VARCHAR(50) NOT NULL,
            limite NUMERIC(12, 2) NOT NULL CHECK (limite > 0),
            UNIQUE (telegram_id, categoria)
        );
        """)

        # Meta de poupança (uma por usuário)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            telegram_id TEXT PRIMARY KEY,
            valor_meta NUMERIC(12, 2) NOT NULL CHECK (valor_meta > 0),
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Investimentos: cada aporte ('entrada') / resgate ('saida') é um movimento
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS investments (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT NOT NULL,
            tipo VARCHAR(10) NOT NULL CHECK (tipo IN ('entrada', 'saida')),
            valor NUMERIC(12, 2) NOT NULL CHECK (valor > 0),
            descricao TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_investments_tg
        ON investments(telegram_id, data);
        """)

        # Bancos antigos: alarga o valor monetário para NUMERIC(12,2) (igual às demais tabelas)
        cursor.execute("ALTER TABLE transactions ALTER COLUMN valor TYPE NUMERIC(12, 2);")
        cursor.execute("ALTER TABLE transactions_backup ALTER COLUMN valor TYPE NUMERIC(12, 2);")

        # Alertas de teto de gastos (teto = renda do mês)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_settings (
            telegram_id TEXT PRIMARY KEY,
            ativo BOOLEAN NOT NULL DEFAULT TRUE,
            limiar INT NOT NULL DEFAULT 80 CHECK (limiar BETWEEN 50 AND 99)
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT NOT NULL,
            mes CHAR(7) NOT NULL,
            nivel VARCHAR(10) NOT NULL,
            enviado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (telegram_id, mes, nivel)
        );
        """)

        # Tokens de recuperação de senha (guarda só o HASH do token, nunca o token em si)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash CHAR(64) NOT NULL UNIQUE,
            expira_em TIMESTAMP NOT NULL,
            usado BOOLEAN NOT NULL DEFAULT FALSE,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Alertas de poupança (opt-in, no máximo 1 por semana)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS poupanca_alertas (
            telegram_id TEXT PRIMARY KEY,
            ativo BOOLEAN NOT NULL DEFAULT FALSE,
            ultimo_envio TIMESTAMP
        );
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
        return render_template('landing.html')   # antes: redirect para o login
    return render_template('main.html')

@app_web.route('/sobre')
def sobre_page():
    return render_template('landing.html')       # acessível mesmo logado

# 2. Rota Login Page
# Página de Transações (histórico de entradas e saídas)
@app_web.route('/transacoes')
def transacoes_page():
    if 'telegram_id' not in session and 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('transacoes.html')

# API: lista de transações do usuário logado
@app_web.route('/api/transacoes', methods=['GET'])
def api_transacoes():
    if 'telegram_id' not in session and 'user_id' not in session:
        return jsonify({"erro": "Não autenticado"}), 401

    telegram_id = obter_telegram_id_atual()
    if not telegram_id:
        return jsonify({"vinculado": False, "transacoes": []})

    mes = request.args.get('mes')  # formato YYYY-MM (opcional)
    if mes and not re.fullmatch(r'\d{4}-\d{2}', mes):
        return jsonify({"erro": "Mês inválido"}), 400

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            query = """
                SELECT id, tipo, valor, categoria, descricao,
                       TO_CHAR(data, 'DD/MM/YYYY HH24:MI'), data
                FROM transactions
                WHERE TRIM(telegram_id) = %s
            """
            params = [telegram_id]
            if mes:
                query += " AND TO_CHAR(data, 'YYYY-MM') = %s"
                params.append(mes)
            query += " ORDER BY data DESC NULLS LAST, id DESC LIMIT 1000;"
            cursor.execute(query, params)
            linhas = cursor.fetchall()

            # Movimentos da poupança entram no extrato (mesmo filtro de mês)
            sql_p = """
                SELECT id, tipo, valor, descricao, TO_CHAR(data, 'DD/MM/YYYY HH24:MI'), data
                FROM savings_movements
                WHERE TRIM(telegram_id) = %s
            """
            params_p = [telegram_id]
            if mes:
                sql_p += " AND TO_CHAR(data, 'YYYY-MM') = %s"
                params_p.append(mes)
            sql_p += " ORDER BY data DESC, id DESC LIMIT 1000;"
            cursor.execute(sql_p, params_p)
            movs = cursor.fetchall()

            disponivel = saldo_disponivel(cursor, telegram_id)
            guardado = saldo_poupanca(cursor, telegram_id)

            sql_mov = """
                SELECT COALESCE(SUM(CASE WHEN tipo = 'deposito' THEN valor ELSE -valor END), 0)
                FROM savings_movements WHERE TRIM(telegram_id) = %s
            """
            params_mov = [telegram_id]
            if mes:
                sql_mov += " AND TO_CHAR(data, 'YYYY-MM') = %s"
                params_mov.append(mes)
            cursor.execute(sql_mov, params_mov)
            poupanca_periodo = float(cursor.fetchone()[0])
    finally:
        conexao.close()

    itens = []
    for r in linhas:
        itens.append((r[6], 1, r[0], {
            "id": r[0], "tipo": r[1], "valor": float(r[2]), "categoria": r[3],
            "descricao": r[4], "data_fmt": r[5] or ""}))
    for r in movs:
        guardou = r[1] == 'deposito'
        itens.append((r[5], 0, r[0], {
            "id": r[0], "tipo": "poupanca", "sentido": r[1], "valor": float(r[2]),
            "categoria": "Poupança",
            "descricao": r[3] or ("Guardado na poupança" if guardou else "Retirado da poupança"),
            "data_fmt": r[4] or ""}))
    itens.sort(key=lambda x: (x[0] or datetime.min, x[1], x[2]), reverse=True)

    return jsonify({
        "vinculado": True,
        "saldo_disponivel": round(disponivel, 2),
        "poupanca": round(guardado, 2),
        "poupanca_periodo": round(poupanca_periodo, 2),
        "transacoes": [i[3] for i in itens]
    })

@app_web.route('/api/saldo-atual', methods=['GET'])
def api_saldo_atual():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"vinculado": False, "saldo": 0})
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            saldo = saldo_disponivel(cursor, tid)
    finally:
        conexao.close()
    return jsonify({"vinculado": True, "saldo": round(saldo, 2)})

# ==========================================
# ORÇAMENTOS, POUPANÇA E RELATÓRIOS
# ==========================================
CATEGORIAS_DESPESA = ["Alimentação", "Carro", "Internet", "Moradia", "Lazer", "Outros"]
# Despesas FIXAS (todas as outras contam como variáveis no relatório). Ajuste ao seu uso.
CATEGORIAS_FIXAS = {"moradia", "internet"}
MESES_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

# Mensagens do Telegram para guardar / sacar / consultar a poupança
_DEST_POUP = r'(?:poupan[çc]a|reserva)'
PADRAO_POUPAR = re.compile(
    r'^(?:guardar|guardei|poupar|poupei|reservar|reservei)\s+(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?'
    r'(?:\s+(?:na|a|para a|pra)\s+(?:minha\s+)?' + _DEST_POUP + r')?\s*(.*)$')
PADRAO_DEPOSITAR = re.compile(
    r'^(?:depositar|depositei|colocar|coloquei|adicionar|adicionei|aplicar|apliquei)\s+(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?'
    r'\s+(?:na|a|para a|pra|em)\s+(?:minha\s+)?' + _DEST_POUP + r'\s*(.*)$')
PADRAO_SACAR = re.compile(
    r'^(?:sacar|saquei|retirar|retirei|resgatar|resgatei|tirar|tirei)\s+(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?'
    r'\s+(?:da|de|na)\s+(?:minha\s+)?' + _DEST_POUP + r'\s*(.*)$')
PADRAO_VER_POUPANCA = re.compile(
    r'^(?:/poupanca|minha poupan[çc]a|saldo (?:da |na )?poupan[çc]a|'
    r'quanto (?:eu )?(?:tenho|guardei) (?:na|de|em) (?:minha )?poupan[çc]a)\??$')


# Mensagens do Telegram para registrar investimentos: "investi 500 em CDB" / "resgatei 200 do CDB"
PADRAO_INVESTIR = re.compile(
    r'^(?:investi|investir|aportei|aportar)\s+(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?\s*(?:(?:em|no|na|nos|nas|de)\s+)?(.*)$')
PADRAO_RESGATAR_INV = re.compile(
    r'^(?:resgatei|resgatar)\s+(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?\s*(?:(?:de|do|da|dos|das|em)\s+)?(.*)$')

# Outras formas de falar a mesma coisa: "apliquei 500 em investimentos", "aporte de 500",
# "500 em investimentos", "retirei 200 dos investimentos". Todas vão para a tabela investments (não para transactions).
_VALOR_MSG = r'(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?'
_DEST_INVESTIMENTOS = r'(?:meus?\s+)?investimentos?'
PADRAO_APLICAR_EM_INVESTIMENTOS = re.compile(
    r'^(?:aplicar|apliquei|colocar|coloquei|botar|botei|depositar|depositei|adicionar|adicionei|'
    r'mandar|mandei|enviar|enviei|passar|passei)\s+' + _VALOR_MSG +
    r'\s+(?:em|no|nos|para os?|pra os?|para|pra)\s+' + _DEST_INVESTIMENTOS + r'\s*(.*)$')
PADRAO_APORTE = re.compile(
    r'^(?:aporte|investimento)\s+(?:de\s+)?' + _VALOR_MSG + r'\s*(?:(?:em|no|na|nos|nas|de)\s+)?(.*)$')
PADRAO_VALOR_EM_INVESTIMENTOS = re.compile(
    r'^' + _VALOR_MSG + r'\s+(?:em|no|nos|de)\s+' + _DEST_INVESTIMENTOS + r'\s*(.*)$')
PADRAO_RESGATAR_DOS_INVESTIMENTOS = re.compile(
    r'^(?:resgatar|resgatei|sacar|saquei|retirar|retirei|tirar|tirei)\s+' + _VALOR_MSG +
    r'\s+(?:de|do|dos|da|das|em)\s+' + _DEST_INVESTIMENTOS + r'\s*(.*)$')


def interpretar_movimento_investimento(texto_lower):
    """Se a mensagem for aporte/resgate de investimento devolve (tipo, valor_txt, descricao); senão None."""
    for padrao in (PADRAO_INVESTIR, PADRAO_APLICAR_EM_INVESTIMENTOS, PADRAO_APORTE, PADRAO_VALOR_EM_INVESTIMENTOS):
        m = padrao.match(texto_lower)
        if m:
            return 'entrada', m.group(1), m.group(2)
    for padrao in (PADRAO_RESGATAR_INV, PADRAO_RESGATAR_DOS_INVESTIMENTOS):
        m = padrao.match(texto_lower)
        if m:
            return 'saida', m.group(1), m.group(2)
    return None


def parse_valor_br(texto):
    """'1.500,50' / '1500.50' / '200' -> float com 2 casas, ou None se inválido."""
    if texto is None:
        return None
    t = re.sub(r'[^\d.,]', '', str(texto)).strip('.,')
    if not t:
        return None
    if ',' in t and '.' in t:
        t = t.replace('.', '').replace(',', '.')
    elif ',' in t:
        t = t.replace(',', '.')
    elif '.' in t and re.fullmatch(r'\d{1,3}(\.\d{3})+', t):
        t = t.replace('.', '')
    try:
        v = round(float(t), 2)
    except ValueError:
        return None
    if v <= 0 or v > 99999999:
        return None
    return v


def validar_mes(mes):
    """Devolve 'YYYY-MM' (mês atual se vazio) ou None se o formato for inválido."""
    if not mes:
        return datetime.now().strftime('%Y-%m')
    return mes if re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', mes) else None


def deslocar_mes(mes, n):
    idx = int(mes[:4]) * 12 + (int(mes[5:7]) - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def rotulo_mes(mes):
    return f"{MESES_ABREV[int(mes[5:7]) - 1]}/{mes[:4]}"


def saldo_poupanca(cursor, telegram_id) -> float:
    cursor.execute("""
        SELECT COALESCE(SUM(CASE WHEN tipo = 'deposito' THEN valor ELSE -valor END), 0)
        FROM savings_movements WHERE telegram_id = %s;
    """, (telegram_id,))
    return float(cursor.fetchone()[0])


def saldo_disponivel(cursor, telegram_id) -> float:
    """Saldo livre para guardar: receitas - despesas - o que já está na poupança (mesma regra do dashboard)."""
    cursor.execute("""
        SELECT COALESCE(SUM(CASE WHEN tipo = 'renda' THEN valor
                                 WHEN tipo = 'despesa' THEN -valor END), 0)
        FROM transactions WHERE TRIM(telegram_id) = %s;
    """, (telegram_id,))
    return float(cursor.fetchone()[0]) - saldo_poupanca(cursor, telegram_id)


def registrar_movimento_poupanca(telegram_id, tipo, valor, descricao=None):
    """Guarda ('deposito') ou saca ('saque') da poupança.
    Depósito maior que o saldo disponível e saque maior que o guardado são recusados."""
    telegram_id = str(telegram_id).strip()
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            # trava por usuário: duas mensagens simultâneas não conseguem passar do saldo
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", (telegram_id,))
            saldo = saldo_poupanca(cursor, telegram_id)
            if tipo == 'saque' and round(valor, 2) > round(saldo, 2):
                conexao.rollback()
                return {"ok": False, "motivo": "sem_poupanca", "saldo": saldo}
            livre = saldo_disponivel(cursor, telegram_id)
            if tipo == 'deposito':
                if round(valor, 2) > round(livre, 2):
                    conexao.rollback()
                    return {"ok": False, "motivo": "sem_saldo", "saldo": saldo, "saldo_livre": livre}
            cursor.execute("""
                INSERT INTO savings_movements (telegram_id, tipo, valor, descricao)
                VALUES (%s, %s, %s, %s) RETURNING id;
            """, (telegram_id, tipo, valor, descricao))
            novo_id = cursor.fetchone()[0]
            cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (telegram_id,))
            row = cursor.fetchone()
        conexao.commit()
        novo_saldo = saldo + valor if tipo == 'deposito' else saldo - valor
        novo_livre = livre - valor if tipo == 'deposito' else livre + valor
        return {"ok": True, "id": novo_id, "saldo": round(novo_saldo, 2),
                "saldo_livre": round(novo_livre, 2),
                "meta": float(row[0]) if row else None}
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def _exigir_usuario():
    """(telegram_id, resposta_de_erro). resposta_de_erro é None quando o usuário está logado."""
    if 'telegram_id' not in session and 'user_id' not in session:
        return None, (jsonify({"erro": "Não autenticado"}), 401)
    return obter_telegram_id_atual(), None


def status_orcamento(percentual):
    if percentual <= 70:
        return 'ok'
    return 'alerta' if percentual <= 90 else 'critico'


def calcular_orcamentos(tid, mes):
    ano, m = int(mes[:4]), int(mes[5:7])
    dias_no_mes = calendar.monthrange(ano, m)[1]
    agora = datetime.now()
    mes_hoje = agora.strftime('%Y-%m')
    if mes == mes_hoje:
        dia_ref = agora.day
    elif mes < mes_hoje:
        dia_ref = dias_no_mes
    else:
        dia_ref = 0

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT id, categoria, limite FROM budgets WHERE telegram_id = %s ORDER BY categoria;", (tid,))
            budgets = cursor.fetchall()
            cursor.execute("""
                SELECT COALESCE(categoria, 'Sem categoria'), SUM(valor)
                FROM transactions
                WHERE TRIM(telegram_id) = %s AND tipo = 'despesa' AND TO_CHAR(data, 'YYYY-MM') = %s
                GROUP BY 1;
            """, (tid, mes))
            gastos_rows = cursor.fetchall()
            total_poup = saldo_poupanca(cursor, tid)
            cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (tid,))
            r = cursor.fetchone()
            meta = float(r[0]) if r else None
            cursor.execute("""
                SELECT id, tipo, valor, descricao, TO_CHAR(data, 'DD/MM/YYYY HH24:MI')
                FROM savings_movements WHERE telegram_id = %s
                ORDER BY data DESC, id DESC LIMIT 8;
            """, (tid,))
            movs = cursor.fetchall()
    finally:
        conexao.close()

    gastos = {}
    for nome, valor in gastos_rows:
        chave = nome.strip().lower()
        antigo = gastos.get(chave, (nome, 0.0))[1]
        gastos[chave] = (nome, antigo + float(valor))

    orcamentos = []
    usadas = set()
    for bid, categoria, limite in budgets:
        limite = float(limite)
        chave = categoria.strip().lower()
        usadas.add(chave)
        gasto = round(gastos.get(chave, (categoria, 0.0))[1], 2)
        percentual = gasto / limite * 100 if limite else 0.0
        ritmo = gasto / dia_ref if dia_ref else 0.0
        projecao = ritmo * dias_no_mes
        dia_estouro = None

        if gasto > limite:
            msg = f"Orçamento estourado: você já passou {fmt_brl(gasto - limite)} do limite."
        elif gasto == limite:
            msg = "Você atingiu exatamente o limite deste mês."
        elif mes == mes_hoje and gasto == 0:
            msg = "Nenhum gasto registrado ainda neste mês."
        elif mes == mes_hoje and projecao > limite:
            dia_estouro = math.ceil(limite / ritmo)
            msg = f"Neste ritmo, o orçamento de {categoria} vai estourar no dia {dia_estouro}."
        elif mes == mes_hoje:
            msg = f"Neste ritmo, você termina o mês com {fmt_brl(projecao)} de {fmt_brl(limite)}."
        elif dia_ref:
            msg = "Mês encerrado dentro do limite."
        else:
            msg = ""

        orcamentos.append({
            "id": bid, "categoria": categoria, "limite": limite, "gasto": gasto,
            "percentual": round(percentual, 1), "status": status_orcamento(percentual),
            "ritmo_diario": round(ritmo, 2),
            "projecao_fim_mes": round(projecao, 2) if mes == mes_hoje else None,
            "dia_estouro": dia_estouro, "mensagem": msg,
        })

    sem_limite = sorted(
        [{"categoria": n, "gasto": round(v, 2)} for k, (n, v) in gastos.items() if k not in usadas],
        key=lambda x: -x["gasto"])
    total_limite = sum(o["limite"] for o in orcamentos)
    total_gasto = sum(o["gasto"] for o in orcamentos)

    return {
        "vinculado": True, "mes": mes, "dia_atual": dia_ref, "dias_no_mes": dias_no_mes,
        "orcamentos": orcamentos, "sem_limite": sem_limite,
        "categorias_disponiveis": categorias_do_usuario(tid, 'despesa'),
        "totais": {"limite": round(total_limite, 2), "gasto": round(total_gasto, 2),
                   "percentual": round(total_gasto / total_limite * 100, 1) if total_limite else 0},
        "poupanca": {
            "total": round(total_poup, 2), "meta": meta,
            "percentual": round(min(total_poup / meta * 100, 100), 1) if meta else None,
            "movimentos": [{"id": a, "tipo": b, "valor": float(c), "descricao": d, "data_fmt": e}
                           for a, b, c, d, e in movs],
        },
    }


def _var_pct(atual, anterior):
    return (atual - anterior) / anterior * 100 if anterior else None


def calcular_relatorio(tid, mes):
    meses = [deslocar_mes(mes, -i) for i in range(5, -1, -1)]  # 6 meses; o selecionado é o último
    ant = deslocar_mes(mes, -1)
    inicio = datetime(int(meses[0][:4]), int(meses[0][5:7]), 1)
    prox = deslocar_mes(mes, 1)
    fim = datetime(int(prox[:4]), int(prox[5:7]), 1)

    receitas = {k: 0.0 for k in meses}
    despesas = {k: 0.0 for k in meses}
    por_cat = {k: {} for k in meses}
    dep, saq = {}, {}

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT TO_CHAR(data, 'YYYY-MM'), tipo, COALESCE(categoria, 'Sem categoria'), SUM(valor)
                FROM transactions
                WHERE TRIM(telegram_id) = %s AND data >= %s AND data < %s
                GROUP BY 1, 2, 3;
            """, (tid, inicio, fim))
            for k, tipo, cat, valor in cursor.fetchall():
                valor = float(valor)
                if tipo == 'renda':
                    receitas[k] += valor
                elif tipo == 'despesa':
                    despesas[k] += valor
                    por_cat[k][cat] = por_cat[k].get(cat, 0.0) + valor
            cursor.execute("""
                SELECT TO_CHAR(data, 'YYYY-MM'), tipo, SUM(valor)
                FROM savings_movements WHERE telegram_id = %s GROUP BY 1, 2;
            """, (tid,))
            for k, tipo, valor in cursor.fetchall():
                (dep if tipo == 'deposito' else saq)[k] = float(valor)
            total_poup = saldo_poupanca(cursor, tid)
            cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (tid,))
            r = cursor.fetchone()
            meta = float(r[0]) if r else None
    finally:
        conexao.close()

    # Série de 6 meses
    acumulado = sum(dep.get(k, 0) - saq.get(k, 0) for k in set(dep) | set(saq) if k < meses[0])
    serie = {"labels": [], "receitas": [], "despesas": [], "saldo": [],
             "depositos": [], "saques": [], "poupanca_liquida": [], "poupanca_acumulada": []}
    for k in meses:
        d, s = dep.get(k, 0.0), saq.get(k, 0.0)
        acumulado += d - s
        serie["labels"].append(rotulo_mes(k))
        serie["receitas"].append(round(receitas[k], 2))
        serie["despesas"].append(round(despesas[k], 2))
        serie["saldo"].append(round(receitas[k] - despesas[k], 2))
        serie["depositos"].append(round(d, 2))
        serie["saques"].append(round(s, 2))
        serie["poupanca_liquida"].append(round(d - s, 2))
        serie["poupanca_acumulada"].append(round(acumulado, 2))

    # DRE do mês selecionado
    cats = por_cat[mes]
    fixas = sum(v for c, v in cats.items() if c.strip().lower() in CATEGORIAS_FIXAS)
    variaveis = sum(cats.values()) - fixas
    desp_total = despesas[mes]
    rec = receitas[mes]
    poupado = dep.get(mes, 0.0) - saq.get(mes, 0.0)
    dre = {
        "receitas": round(rec, 2), "despesas_fixas": round(fixas, 2),
        "despesas_variaveis": round(variaveis, 2), "despesas_total": round(desp_total, 2),
        "saldo": round(rec - desp_total, 2), "poupado": round(poupado, 2),
        "depositos": round(dep.get(mes, 0.0), 2), "saques": round(saq.get(mes, 0.0), 2),
        "taxa_poupanca": round(poupado / rec * 100, 1) if rec > 0 else None,
        "categorias": sorted([
            {"categoria": c, "tipo": "fixa" if c.strip().lower() in CATEGORIAS_FIXAS else "variavel",
             "valor": round(v, 2), "pct_despesas": round(v / desp_total * 100, 1) if desp_total else 0}
            for c, v in cats.items()], key=lambda x: -x["valor"]),
    }

    # Vilões do orçamento: maior aumento de gasto (categorias variáveis) sobre o mês anterior
    cats_ant = por_cat[ant]
    viloes = []
    for c in set(cats) | set(cats_ant):
        if c.strip().lower() in CATEGORIAS_FIXAS:
            continue
        atual, antes = cats.get(c, 0.0), cats_ant.get(c, 0.0)
        if atual - antes > 0:
            viloes.append({"categoria": c, "atual": round(atual, 2), "anterior": round(antes, 2),
                           "aumento": round(atual - antes, 2),
                           "pct": round(_var_pct(atual, antes), 1) if antes else None})
    viloes.sort(key=lambda x: -x["aumento"])
    viloes = viloes[:3]

    # Insights automáticos (cálculo direto, sem inventar números)
    insights = []
    poupado_ant = dep.get(ant, 0.0) - saq.get(ant, 0.0)
    if rec == 0 and desp_total == 0 and not dep.get(mes) and not saq.get(mes):
        insights.append({"tipo": "info", "texto": "Nenhum lançamento neste mês ainda."})
    else:
        if rec > 0 and desp_total > rec:
            insights.append({"tipo": "alerta", "texto": f"As despesas superaram a renda em {fmt_brl(desp_total - rec)} neste mês."})
        elif rec == 0:
            insights.append({"tipo": "info", "texto": "Nenhuma renda registrada neste mês."})
        v = _var_pct(desp_total, despesas[ant])
        if v is not None and v >= 10:
            insights.append({"tipo": "alerta", "texto": f"Suas despesas totais aumentaram {v:.0f}% em relação a {rotulo_mes(ant)}."})
        elif v is not None and v <= -10:
            insights.append({"tipo": "conquista", "texto": f"Suas despesas totais caíram {abs(v):.0f}% em relação a {rotulo_mes(ant)}."})

        mudancas = []
        for c in set(cats) | set(cats_ant):
            atual, antes = cats.get(c, 0.0), cats_ant.get(c, 0.0)
            delta = atual - antes
            pct = _var_pct(atual, antes)
            if pct is not None and abs(delta) >= 50 and abs(pct) >= 30:
                mudancas.append((abs(delta), c, pct, antes, atual))
        for _, c, pct, antes, atual in sorted(mudancas, reverse=True)[:4]:
            if pct > 0:
                insights.append({"tipo": "alerta", "texto": f"Suas despesas com {c} aumentaram {pct:.0f}% em relação ao mês anterior ({fmt_brl(antes)} para {fmt_brl(atual)})."})
            else:
                insights.append({"tipo": "conquista", "texto": f"Suas despesas com {c} caíram {abs(pct):.0f}% em relação ao mês anterior ({fmt_brl(antes)} para {fmt_brl(atual)})."})

        if poupado > 0 and rec > 0:
            taxa = poupado / rec * 100
            insights.append({"tipo": "conquista" if taxa >= 20 else "info",
                             "texto": f"Você guardou {taxa:.0f}% da sua renda neste mês ({fmt_brl(poupado)})."})
        if poupado_ant > 0 and poupado > poupado_ant and (poupado / poupado_ant - 1) * 100 >= 10:
            insights.append({"tipo": "conquista", "texto": f"Você guardou {(poupado / poupado_ant - 1) * 100:.0f}% a mais na poupança do que no mês anterior."})
        if poupado < 0:
            insights.append({"tipo": "alerta", "texto": f"Você sacou {fmt_brl(abs(poupado))} a mais do que guardou na poupança neste mês."})

    try:
        for o in calcular_orcamentos(tid, mes)["orcamentos"]:
            if o["gasto"] > o["limite"]:
                insights.append({"tipo": "alerta", "texto": f"O orçamento de {o['categoria']} estourou em {fmt_brl(o['gasto'] - o['limite'])}."})
            elif o["percentual"] > 90:
                insights.append({"tipo": "alerta", "texto": f"O orçamento de {o['categoria']} já usou {o['percentual']:.0f}% do limite."})
    except Exception as e:
        print(f"Aviso: insights de orçamento indisponíveis: {e}")

    return {"vinculado": True, "mes": mes, "rotulo": rotulo_mes(mes), "dre": dre,
            "comparativo": serie, "viloes": viloes, "insights": insights,
            "poupanca": {"total": round(total_poup, 2), "meta": meta}}


def gerar_analise_ia(titulo, dados):
    """Pede à Groq um texto curto sobre números JÁ calculados (nunca envia o telegram_id)."""
    prompt = (
        f"Você é o Fincash, assistente financeiro pessoal. Com base SOMENTE nos números abaixo ({titulo}), "
        "escreva em português do Brasil de 3 a 5 tópicos curtos, cada um começando com \"- \", "
        "apontando riscos, conquistas e uma sugestão prática. Não invente valores nem categorias.\n"
        f"DADOS: {json.dumps(dados, ensure_ascii=False)}"
    )
    resposta = cliente_groq.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return resposta.choices[0].message.content.strip()


def csv_seguro(valor):
    """Evita injeção de fórmulas ao abrir o CSV no Excel."""
    s = "" if valor is None else str(valor)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


# ---------- Páginas ----------
@app_web.route('/orcamentos')
def orcamentos_page():
    if 'telegram_id' not in session and 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('orcamentos.html')


@app_web.route('/relatorios')
def relatorios_page():
    if 'telegram_id' not in session and 'user_id' not in session:
        return redirect(url_for('login_page'))
    return render_template('relatorios.html')


# ---------- API: orçamentos ----------
@app_web.route('/api/orcamentos', methods=['GET'])
def api_orcamentos():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    mes = validar_mes(request.args.get('mes'))
    if not mes:
        return jsonify({"erro": "Mês inválido"}), 400
    if not tid:
        return jsonify({"vinculado": False})
    return jsonify(calcular_orcamentos(tid, mes))


@app_web.route('/api/orcamentos', methods=['POST'])
def api_salvar_orcamento():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"erro": "Vincule seu Telegram antes de definir limites."}), 400
    dados = request.get_json(silent=True) or {}
    categoria = achar_categoria(tid, 'despesa', dados.get('categoria'))
    limite = parse_valor_br(dados.get('limite'))
    if not categoria:
        return jsonify({"erro": "Categoria inválida."}), 400
    if not limite:
        return jsonify({"erro": "Informe um limite maior que zero."}), 400

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                INSERT INTO budgets (telegram_id, categoria, limite) VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id, categoria) DO UPDATE SET limite = EXCLUDED.limite;
            """, (tid, categoria, limite))
        conexao.commit()
    except Exception as e:
        conexao.rollback()
        print(f"Erro ao salvar orçamento: {e}")
        return jsonify({"erro": "Não foi possível salvar o limite."}), 500
    finally:
        conexao.close()
    return jsonify({"ok": True})


@app_web.route('/api/orcamentos/<int:orcamento_id>', methods=['DELETE'])
def api_excluir_orcamento(orcamento_id):
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"erro": "Telegram não vinculado."}), 400
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("DELETE FROM budgets WHERE id = %s AND telegram_id = %s;", (orcamento_id, tid))
            apagados = cursor.rowcount
        conexao.commit()
    finally:
        conexao.close()
    if not apagados:
        return jsonify({"erro": "Limite não encontrado."}), 404
    return jsonify({"ok": True})


@app_web.route('/api/meta-poupanca', methods=['PUT'])
def api_meta_poupanca():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"erro": "Vincule seu Telegram antes de definir uma meta."}), 400
    dados = request.get_json(silent=True) or {}
    bruto = dados.get('valor')
    remover = bruto in (0, "0", "", None)
    valor = None if remover else parse_valor_br(bruto)
    if not remover and not valor:
        return jsonify({"erro": "Informe um valor válido para a meta."}), 400

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            if remover:
                cursor.execute("DELETE FROM savings_goals WHERE telegram_id = %s;", (tid,))
            else:
                cursor.execute("""
                    INSERT INTO savings_goals (telegram_id, valor_meta) VALUES (%s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE
                    SET valor_meta = EXCLUDED.valor_meta, atualizado_em = CURRENT_TIMESTAMP;
                """, (tid, valor))
        conexao.commit()
    except Exception as e:
        conexao.rollback()
        print(f"Erro ao salvar meta: {e}")
        return jsonify({"erro": "Não foi possível salvar a meta."}), 500
    finally:
        conexao.close()
    return jsonify({"ok": True, "meta": valor})


@app_web.route('/api/orcamentos/analise-ia', methods=['POST'])
def api_analise_orcamentos():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"erro": "Telegram não vinculado."}), 400
    mes = validar_mes((request.get_json(silent=True) or {}).get('mes'))
    if not mes:
        return jsonify({"erro": "Mês inválido"}), 400
    d = calcular_orcamentos(tid, mes)
    if not d["orcamentos"]:
        return jsonify({"erro": "Defina ao menos um limite para gerar a análise."}), 400
    resumo = {"mes": mes, "dia_atual": d["dia_atual"], "dias_no_mes": d["dias_no_mes"],
              "orcamentos": [{k: o[k] for k in ("categoria", "limite", "gasto", "percentual", "projecao_fim_mes", "dia_estouro")}
                             for o in d["orcamentos"]],
              "poupanca": {"total": d["poupanca"]["total"], "meta": d["poupanca"]["meta"]}}
    try:
        return jsonify({"texto": gerar_analise_ia("orçamentos do mês", resumo)})
    except Exception as e:
        print(f"Erro na análise de IA (orçamentos): {e}")
        return jsonify({"erro": "A IA está indisponível agora. Tente de novo em instantes."}), 502


# ---------- API: relatórios ----------
@app_web.route('/api/relatorios', methods=['GET'])
def api_relatorios():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    mes = validar_mes(request.args.get('mes'))
    if not mes:
        return jsonify({"erro": "Mês inválido"}), 400
    if not tid:
        return jsonify({"vinculado": False})
    return jsonify(calcular_relatorio(tid, mes))


@app_web.route('/api/relatorios/analise-ia', methods=['POST'])
def api_analise_relatorio():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"erro": "Telegram não vinculado."}), 400
    mes = validar_mes((request.get_json(silent=True) or {}).get('mes'))
    if not mes:
        return jsonify({"erro": "Mês inválido"}), 400
    r = calcular_relatorio(tid, mes)
    resumo = {"mes": r["rotulo"], "dre": {k: v for k, v in r["dre"].items()},
              "comparativo": r["comparativo"], "viloes": r["viloes"]}
    try:
        return jsonify({"texto": gerar_analise_ia("relatório mensal", resumo)})
    except Exception as e:
        print(f"Erro na análise de IA (relatório): {e}")
        return jsonify({"erro": "A IA está indisponível agora. Tente de novo em instantes."}), 502


@app_web.route('/api/relatorios/exportar.csv', methods=['GET'])
def api_exportar_csv():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    mes = validar_mes(request.args.get('mes'))
    if not mes or not tid:
        return jsonify({"erro": "Mês inválido ou Telegram não vinculado."}), 400

    r = calcular_relatorio(tid, mes)
    num = lambda v: f"{float(v):.2f}".replace(".", ",")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    d = r["dre"]

    w.writerow([f"Relatório Fincash - {r['rotulo']}"])
    w.writerow([])
    w.writerow(["Resumo do mês (DRE)"])
    w.writerow(["Item", "Valor (R$)"])
    w.writerow(["Receitas", num(d["receitas"])])
    w.writerow(["Despesas fixas", num(d["despesas_fixas"])])
    w.writerow(["Despesas variáveis", num(d["despesas_variaveis"])])
    w.writerow(["Total de despesas", num(d["despesas_total"])])
    w.writerow(["Saldo líquido", num(d["saldo"])])
    w.writerow(["Guardado na poupança (líquido)", num(d["poupado"])])
    w.writerow(["Taxa de poupança (%)", "" if d["taxa_poupanca"] is None else num(d["taxa_poupanca"])])
    w.writerow([])
    w.writerow(["Despesas por categoria"])
    w.writerow(["Categoria", "Tipo", "Valor (R$)", "% das despesas"])
    for c in d["categorias"]:
        w.writerow([csv_seguro(c["categoria"]), "Fixa" if c["tipo"] == "fixa" else "Variável", num(c["valor"]), num(c["pct_despesas"])])
    w.writerow([])
    w.writerow(["Comparativo mensal"])
    w.writerow(["Mês", "Receitas", "Despesas", "Saldo", "Depósitos na poupança", "Saques da poupança", "Poupança acumulada"])
    s = r["comparativo"]
    for i, rot in enumerate(s["labels"]):
        w.writerow([rot, num(s["receitas"][i]), num(s["despesas"][i]), num(s["saldo"][i]),
                    num(s["depositos"][i]), num(s["saques"][i]), num(s["poupanca_acumulada"][i])])
    w.writerow([])
    w.writerow(["Lançamentos do mês"])
    w.writerow(["ID", "Data", "Tipo", "Categoria", "Descrição", "Valor (R$)"])

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT id, TO_CHAR(data, 'DD/MM/YYYY HH24:MI'), tipo, categoria, descricao, valor
                FROM transactions
                WHERE TRIM(telegram_id) = %s AND TO_CHAR(data, 'YYYY-MM') = %s
                ORDER BY data, id;
            """, (tid, mes))
            for tid_, data_, tipo_, cat_, desc_, val_ in cursor.fetchall():
                w.writerow([tid_, data_, "Entrada" if tipo_ == "renda" else "Saída",
                            csv_seguro(cat_), csv_seguro(desc_), num(val_)])
    finally:
        conexao.close()

    return Response(
        buf.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={"Content-Disposition": f"attachment; filename=fincash_relatorio_{mes}.csv"},
    )

# ==========================================
# ANÁLISE PREDITIVA: PREVISÃO DE GASTOS DO PRÓXIMO MÊS
# ==========================================
MESES_BASE_PREVISAO = 3   # quantos meses fechados entram no cálculo
CONFIANCA_PREVISAO = {0: "baixa", 1: "baixa", 2: "média", 3: "boa"}


def calcular_previsao(tid):
    """Estima os gastos do próximo mês com média ponderada dos últimos meses fechados."""
    hoje = datetime.now()
    mes_atual = hoje.strftime('%Y-%m')
    prox = deslocar_mes(mes_atual, 1)
    base = {"vinculado": True, "mes": prox, "mes_rotulo": rotulo_mes(prox)}

    candidatos = [deslocar_mes(mes_atual, -i) for i in range(MESES_BASE_PREVISAO, 0, -1)]  # do mais antigo ao mais novo
    inicio = datetime(int(candidatos[0][:4]), int(candidatos[0][5:7]), 1)
    fim = datetime(int(mes_atual[:4]), int(mes_atual[5:7]), 1)

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT TO_CHAR(MIN(data), 'YYYY-MM') FROM transactions WHERE TRIM(telegram_id) = %s;", (tid,))
            primeiro = cursor.fetchone()[0]
            cursor.execute("""
                SELECT TO_CHAR(data, 'YYYY-MM'), tipo, COALESCE(categoria, 'Sem categoria'), SUM(valor)
                FROM transactions
                WHERE TRIM(telegram_id) = %s AND data >= %s AND data < %s
                GROUP BY 1, 2, 3;
            """, (tid, inicio, fim))
            linhas = cursor.fetchall()
            cursor.execute("""
                SELECT COALESCE(SUM(valor), 0) FROM transactions
                WHERE TRIM(telegram_id) = %s AND tipo = 'despesa' AND TO_CHAR(data, 'YYYY-MM') = %s;
            """, (tid, mes_atual))
            gasto_mes_atual = float(cursor.fetchone()[0])
    finally:
        conexao.close()

    if not primeiro:
        return {**base, "disponivel": False, "motivo": "sem_dados"}

    # só entram meses a partir do primeiro lançamento da pessoa (meses antes disso não são "zero gasto")
    meses_base = [k for k in candidatos if k >= primeiro]

    # Sem nenhum mês fechado: projeta pelo ritmo do mês atual (confiança baixa)
    if not meses_base:
        if gasto_mes_atual <= 0:
            return {**base, "disponivel": False, "motivo": "sem_dados"}
        dias_no_mes = calendar.monthrange(hoje.year, hoje.month)[1]
        proj = gasto_mes_atual / hoje.day * dias_no_mes
        return {**base, "disponivel": True, "metodo": "ritmo", "meses_base": 0,
                "previsao": round(proj, 2), "faixa_min": None, "faixa_max": None,
                "confianca": "baixa", "categorias": [],
                "avisos": ["Você ainda não tem um mês completo de histórico: a previsão usa o ritmo de gastos deste mês e fica mais precisa com o tempo."]}

    n = len(meses_base)
    pesos = list(range(1, n + 1))        # o mês mais recente pesa mais
    soma_pesos = sum(pesos)

    renda = {k: 0.0 for k in meses_base}
    nomes, por_cat = {}, {}
    for k, tipo, cat, valor in linhas:
        if k not in renda:
            continue
        valor = float(valor)
        if tipo == 'renda':
            renda[k] += valor
        elif tipo == 'despesa':
            ch = normalizar(cat)
            nomes.setdefault(ch, cat)
            por_cat.setdefault(ch, [0.0] * n)[meses_base.index(k)] += valor

    categorias = []
    for ch, serie in por_cat.items():
        media_pond = sum(v * p for v, p in zip(serie, pesos)) / soma_pesos
        fixa = nomes[ch].strip().lower() in CATEGORIAS_FIXAS
        valor_prev = serie[-1] if (fixa and serie[-1] > 0) else media_pond   # conta fixa se repete
        categorias.append({"categoria": nomes[ch], "tipo": "fixa" if fixa else "variavel",
                           "previsao": round(valor_prev, 2), "ultimo_mes": round(serie[-1], 2)})
    categorias.sort(key=lambda c: -c["previsao"])
    previsao = sum(c["previsao"] for c in categorias)

    # Faixa: previsão +/- a variação (desvio padrão) dos totais mensais
    faixa_min = faixa_max = None
    totais_hist = [sum(serie[i] for serie in por_cat.values()) for i in range(n)]
    if n >= 2:
        media = sum(totais_hist) / n
        desvio = math.sqrt(sum((t - media) ** 2 for t in totais_hist) / n)
        faixa_min, faixa_max = round(max(previsao - desvio, 0), 2), round(previsao + desvio, 2)

    # Tendência de alta: categorias variáveis cujo último mês subiu sobre a média dos anteriores
    altas = []
    if n >= 2:
        for ch, serie in por_cat.items():
            if nomes[ch].strip().lower() in CATEGORIAS_FIXAS:
                continue
            antes = sum(serie[:-1]) / (n - 1)
            aumento = serie[-1] - antes
            if antes > 0 and aumento >= 50 and aumento / antes >= 0.15:
                altas.append((aumento, nomes[ch]))
        altas.sort(reverse=True)

    renda_media = sum(renda.values()) / n
    avisos = []
    if renda_media > 0 and previsao > renda_media:
        avisos.append(f"A previsão supera a sua renda média mensal ({fmt_brl(renda_media)}). Vale rever os gastos.")
    if altas:
        avisos.append(f"Tendência de alta em \"{altas[0][1]}\", baseada nos últimos {n} meses.")
    if renda_media > 0 and previsao <= renda_media and previsao >= renda_media * 0.8:
        avisos.append(f"A previsão equivale a {previsao / renda_media * 100:.0f}% da sua renda média mensal.")
    if not avisos:
        avisos.append(f"Seus gastos estão estáveis nos últimos {n} {'mês' if n == 1 else 'meses'}.")
    if n == 1:
        avisos.append("Só há um mês de histórico, então a previsão ainda é bem aproximada.")

    return {**base, "disponivel": True, "metodo": "media_ponderada", "meses_base": n,
            "previsao": round(previsao, 2), "faixa_min": faixa_min, "faixa_max": faixa_max,
            "confianca": CONFIANCA_PREVISAO.get(n, "boa"), "categorias": categorias[:5], "avisos": avisos}


@app_web.route('/api/previsao', methods=['GET'])
def api_previsao():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"vinculado": False})
    try:
        return jsonify(calcular_previsao(tid))
    except Exception:
        traceback.print_exc()
        return jsonify({"erro": "Não foi possível calcular a previsão agora."}), 500


# ---------- Telegram: "qual a minha previsão de gastos para o mês que vem?" ----------
PADRAO_PREVISAO_TERMO = re.compile(r'\b(?:previsao|prever|projecao|projetar|estimativa|estimar)\b')
PADRAO_PREVISAO_ASSUNTO = re.compile(r'\b(?:gast\w*|despes\w*|mes que vem|proximo mes|mes seguinte)\b')
PADRAO_QUANTO_VOU_GASTAR = re.compile(r'\bquanto\s+(?:eu\s+)?(?:vou|devo|deverei|irei)\s+gastar\b')


def eh_pedido_previsao(texto) -> bool:
    t = _texto_limpo_menu(texto)
    if 'invest' in t:          # previsão de investimentos é assunto do módulo de investimentos
        return False
    return bool(PADRAO_QUANTO_VOU_GASTAR.search(t)
                or (PADRAO_PREVISAO_TERMO.search(t) and PADRAO_PREVISAO_ASSUNTO.search(t)))


def montar_texto_previsao(p) -> str:
    linhas = [f"🔮 *Previsão de gastos para {p['mes_rotulo']}*", ""]
    linhas.append(f"💸 Você tende a gastar cerca de *{fmt_brl(p['previsao'])}*")
    if p.get("faixa_min") is not None:
        linhas.append(f"📏 Faixa provável: {fmt_brl(p['faixa_min'])} a {fmt_brl(p['faixa_max'])}")
    if p["metodo"] == "media_ponderada":
        n = p["meses_base"]
        linhas.append(f"📊 Base: {n} {'mês fechado' if n == 1 else 'meses fechados'} (os mais recentes pesam mais)")
    if p["categorias"]:
        linhas += ["", "*Maiores categorias previstas:*"]
        linhas += [f"• {md_seguro(c['categoria'])}: {fmt_brl(c['previsao'])}" for c in p["categorias"][:3]]
    linhas += [""] + [f"⚠️ {md_seguro(a)}" for a in p["avisos"]]
    linhas += ["", f"Confiança da previsão: *{p['confianca']}*. É uma estimativa baseada no seu histórico, não uma garantia."]
    return "\n".join(linhas)


async def responder_previsao(update: Update, user_id: str):
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return
    try:
        p = calcular_previsao(str(user_id).strip())
    except Exception:
        traceback.print_exc()
        await update.message.reply_text("❌ Não consegui calcular a previsão agora. Tente novamente em instantes.")
        return
    if not p["disponivel"]:
        await update.message.reply_text("📭 Ainda não tenho gastos suficientes para prever o próximo mês. "
                                        "Registre suas despesas e tente de novo em alguns dias.")
        return
    await update.message.reply_text(montar_texto_previsao(p), parse_mode="Markdown")


async def comando_previsao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await responder_previsao(update, str(update.effective_user.id))

@app_web.route('/login')
def login_page():
    if 'telegram_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')

# 3. API Login
_TENTATIVAS_LOGIN = {}          # chave -> horários das falhas recentes
_LOGIN_MAX_FALHAS = 8
_LOGIN_JANELA_SEG = 300
_login_lock = threading.Lock()


def _login_bloqueado(chave) -> bool:
    agora = time.time()
    with _login_lock:
        falhas = [t for t in _TENTATIVAS_LOGIN.get(chave, []) if agora - t < _LOGIN_JANELA_SEG]
        _TENTATIVAS_LOGIN[chave] = falhas
        return len(falhas) >= _LOGIN_MAX_FALHAS


def _login_falhou(chave):
    with _login_lock:
        _TENTATIVAS_LOGIN.setdefault(chave, []).append(time.time())


def _login_ok(chave):
    with _login_lock:
        _TENTATIVAS_LOGIN.pop(chave, None)


@app_web.route('/api/login', methods=['POST'])
def handle_login():
    dados = request.get_json(silent=True) or {}
    identifier = str(dados.get('identifier', '')).strip()
    password = str(dados.get('password', '')).strip()

    if not identifier or not password:
        return jsonify({"erro": "Informe seu telefone ou e-mail e a senha."}), 400

    chave_login = f"{request.remote_addr}|{identifier.lower()}"
    if _login_bloqueado(chave_login):
        return jsonify({"erro": "Muitas tentativas. Aguarde alguns minutos e tente de novo."}), 429

    # Telefone digitado com máscara "(11) 99999-9999" vira "11999999999" (igual ao cadastro)
    telefone_limpo = limpar_telefone(identifier)

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
            _login_ok(chave_login)
            return jsonify({"sucesso": True, "mensagem": "Login efetuado!"})
        _login_falhou(chave_login)
        return jsonify({"erro": "Senha incorreta."}), 401
    _login_falhou(chave_login)
    return jsonify({"erro": "Usuário não encontrado."}), 404

# 4. API Cadastro
@app_web.route('/api/cadastrar', methods=['POST'])
def handle_register():
    dados = request.get_json() or {}
    nome = str(dados.get('name', '')).strip()
    email = str(dados.get('email', '')).strip()
    telefone = limpar_telefone(dados.get('phone', ''))
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
        print(f"❌ Erro no cadastro: {e}")
        traceback.print_exc()
        return jsonify({"erro": "Não foi possível criar a conta agora. Tente novamente em instantes."}), 500
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

# ==========================================
# 4.7 RECUPERAÇÃO DE SENHA (link por e-mail)
# ==========================================
RESET_VALIDADE_MIN = 30
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")   # ex.: https://fincash.com.br (defina no .env em produção)

_TENTATIVAS_RESET = {}
_RESET_MAX = 5
_RESET_JANELA_SEG = 900
_reset_lock = threading.Lock()


def _reset_limitado(chave) -> bool:
    """Máx. 5 pedidos de recuperação a cada 15 min por IP+identificador (evita spam de e-mails)."""
    agora = time.time()
    with _reset_lock:
        pedidos = [t for t in _TENTATIVAS_RESET.get(chave, []) if agora - t < _RESET_JANELA_SEG]
        if len(pedidos) >= _RESET_MAX:
            _TENTATIVAS_RESET[chave] = pedidos
            return True
        pedidos.append(agora)
        _TENTATIVAS_RESET[chave] = pedidos
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def montar_email_recuperacao(nome, email, link):
    msg = Message(subject="Fincash: alguém tentou acessar sua conta", recipients=[email])
    msg.body = (
        f"Olá, {nome}!\n\n"
        "Alguém solicitou a recuperação de senha da sua conta no Fincash.\n\n"
        f"Se foi você, abra o link abaixo para criar uma nova senha (vale por {RESET_VALIDADE_MIN} minutos):\n{link}\n\n"
        "Se NÃO foi você, ignore este e-mail: sua senha continua a mesma e ninguém acessou sua conta."
    )
    nome_seguro = escapar_html(nome)
    msg.html = f"""
    <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
        <h2 style="color: #2563eb; text-align: center;">fincash</h2>
        <hr style="border: 0; border-top: 1px solid #eee;">
        <p>Olá, <strong>{nome_seguro}</strong>!</p>
        <p><strong>Alguém está tentando acessar sua conta</strong> no Fincash (pedido de recuperação de senha).</p>
        <p>Se foi você, clique no botão abaixo para criar uma nova senha:</p>
        <p style="text-align: center; margin: 28px 0;">
            <a href="{link}" style="background-color: #2563eb; color: #ffffff; padding: 14px 28px; border-radius: 8px; text-decoration: none; font-weight: bold; display: inline-block;">
                Sim, fui eu — criar nova senha
            </a>
        </p>
        <p style="font-size: 13px; color: #64748b;">Este link vale por {RESET_VALIDADE_MIN} minutos e só pode ser usado uma vez.<br>
        Se o botão não funcionar, copie e cole este endereço no navegador:<br>
        <span style="word-break: break-all;">{link}</span></p>
        <div style="background-color: #f8fafc; padding: 15px; border-left: 4px solid #ef4444; margin: 20px 0; border-radius: 4px;">
            <p style="margin: 0; font-size: 14px; color: #475569;">
                ⚠️ <strong>Não foi você?</strong><br>
                Ignore este e-mail: sua senha continua a mesma e ninguém acessou sua conta.
                Se receber vários avisos assim, fale com nosso SAC:
                <a href="mailto:{SAC_EMAIL}" style="color: #ef4444; font-weight: bold;">{SAC_EMAIL}</a>
            </p>
        </div>
    </div>
    """
    return msg


def montar_email_senha_alterada(nome, email):
    msg = Message(subject="Fincash: sua senha foi alterada", recipients=[email])
    msg.body = (
        f"Olá, {nome}!\n\nA senha da sua conta no Fincash acabou de ser alterada.\n"
        f"Se não foi você, entre em contato imediatamente com o SAC: {SAC_EMAIL}"
    )
    msg.html = f"""
    <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
        <h2 style="color: #2563eb; text-align: center;">fincash</h2>
        <hr style="border: 0; border-top: 1px solid #eee;">
        <p>Olá, <strong>{escapar_html(nome)}</strong>!</p>
        <p>A senha da sua conta no Fincash <strong>acabou de ser alterada</strong>.</p>
        <div style="background-color: #f8fafc; padding: 15px; border-left: 4px solid #ef4444; margin: 20px 0; border-radius: 4px;">
            <p style="margin: 0; font-size: 14px; color: #475569;">
                ⚠️ <strong>Não foi você?</strong> Entre em contato agora com o SAC:
                <a href="mailto:{SAC_EMAIL}" style="color: #ef4444; font-weight: bold;">{SAC_EMAIL}</a>
            </p>
        </div>
    </div>
    """
    return msg


@app_web.route('/api/esqueci-senha', methods=['POST'])
def esqueci_senha():
    dados = request.get_json(silent=True) or {}
    identifier = str(dados.get('identifier', '')).strip()
    resposta_generica = jsonify({
        "sucesso": True,
        "mensagem": "Se houver uma conta com esses dados, enviamos um link de recuperação para o e-mail cadastrado."
    })

    if not identifier:
        return jsonify({"erro": "Informe seu e-mail ou telefone."}), 400

    if _reset_limitado(f"{request.remote_addr}|{identifier.lower()}"):
        return jsonify({"erro": "Muitos pedidos. Aguarde alguns minutos e tente de novo."}), 429

    telefone_limpo = limpar_telefone(identifier)
    conexao = obter_conexao()
    cursor = conexao.cursor()
    try:
        cursor.execute("""
            SELECT id, nome, email FROM users
            WHERE LOWER(email) = LOWER(%s) OR telefone = %s;
        """, (identifier, telefone_limpo))
        user = cursor.fetchone()

        # Resposta idêntica exista a conta ou não (não revela quem é cliente)
        if not user or not user[2]:
            return resposta_generica

        user_id, nome, email = user
        token = secrets.token_urlsafe(32)

        # Só o link mais recente vale: invalida pedidos anteriores ainda abertos
        cursor.execute("UPDATE password_resets SET usado = TRUE WHERE user_id = %s AND usado = FALSE;", (user_id,))
        cursor.execute("""
            INSERT INTO password_resets (user_id, token_hash, expira_em)
            VALUES (%s, %s, NOW() + (%s * INTERVAL '1 minute'));
        """, (user_id, _hash_token(token), RESET_VALIDADE_MIN))
        conexao.commit()
    except Exception:
        conexao.rollback()
        traceback.print_exc()
        return jsonify({"erro": "Não foi possível processar agora. Tente novamente em instantes."}), 500
    finally:
        cursor.close()
        conexao.close()

    base = APP_BASE_URL or request.host_url.rstrip("/")
    link = f"{base}/redefinir-senha?token={token}"
    msg = montar_email_recuperacao(nome, email, link)
    threading.Thread(target=enviar_email_async, args=(app_web, msg), daemon=True).start()
    return resposta_generica


def _token_reset_valido(token: str) -> bool:
    if not token:
        return False
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT 1 FROM password_resets
                WHERE token_hash = %s AND usado = FALSE AND expira_em > NOW();
            """, (_hash_token(token),))
            return cursor.fetchone() is not None
    finally:
        conexao.close()


@app_web.route('/redefinir-senha')
def redefinir_senha_page():
    token = request.args.get('token', '').strip()
    resp = Response(render_template('redefinir_senha.html', token_valido=_token_reset_valido(token)))
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    return resp


@app_web.route('/api/redefinir-senha', methods=['POST'])
def redefinir_senha():
    dados = request.get_json(silent=True) or {}
    token = str(dados.get('token', '')).strip()
    nova_senha = str(dados.get('password', '')).strip()
    confirmar = str(dados.get('confirm', '')).strip()

    if not token or not nova_senha or not confirmar:
        return jsonify({"erro": "Preencha todos os campos."}), 400
    if len(nova_senha) < 6:
        return jsonify({"erro": "A senha precisa ter pelo menos 6 caracteres."}), 400
    if nova_senha != confirmar:
        return jsonify({"erro": "As senhas não coincidem."}), 400

    conexao = obter_conexao()
    cursor = conexao.cursor()
    try:
        # Consome o token de forma atômica (impede uso duplo mesmo com dois cliques simultâneos)
        cursor.execute("""
            UPDATE password_resets SET usado = TRUE
            WHERE token_hash = %s AND usado = FALSE AND expira_em > NOW()
            RETURNING user_id;
        """, (_hash_token(token),))
        row = cursor.fetchone()
        if not row:
            conexao.rollback()
            return jsonify({"erro": "Este link expirou ou já foi usado. Solicite um novo."}), 400

        user_id = row[0]
        cursor.execute("UPDATE users SET senha = %s WHERE id = %s RETURNING nome, email;",
                       (generate_password_hash(nova_senha), user_id))
        nome, email = cursor.fetchone()

        cursor.execute("UPDATE password_resets SET usado = TRUE WHERE user_id = %s AND usado = FALSE;", (user_id,))
        cursor.execute("""
            INSERT INTO dados_salvos (user_id, tipo_operacao, dados_antigos, dados_novos)
            VALUES (%s, %s, %s, %s);
        """, (user_id, 'RECUPERACAO_SENHA', json.dumps({"campo": "senha"}), json.dumps({"campo": "senha", "alterada": True})))
        conexao.commit()
    except Exception:
        conexao.rollback()
        traceback.print_exc()
        return jsonify({"erro": "Não foi possível redefinir a senha agora. Tente novamente."}), 500
    finally:
        cursor.close()
        conexao.close()

    if email:
        threading.Thread(target=enviar_email_async, args=(app_web, montar_email_senha_alterada(nome, email)), daemon=True).start()
    return jsonify({"sucesso": True, "mensagem": "Senha redefinida com sucesso!"})


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
@app_web.route('/api/dados-dashboard', methods=['GET'])
def obter_dados_dashboard():
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
            WHERE TRIM(telegram_id) = %s {condicao_data};
        """
        cursor.execute(query_totais, params_base)
        row_totais = cursor.fetchone()
        total_renda = float(row_totais[0]) if row_totais else 0.0
        total_despesas = float(row_totais[1]) if row_totais else 0.0

        # Poupança
        # Total guardado = depósitos - saques (acumulado de sempre, vai no card de poupança)
        total_poupanca = saldo_poupanca(cursor, telegram_id)

        # Movimentos da poupança no período filtrado (mesmo filtro de mês dos totais)
        cursor.execute(f"""
            SELECT
                COALESCE(SUM(CASE WHEN tipo = 'deposito' THEN valor ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN tipo = 'saque' THEN valor ELSE 0 END), 0)
            FROM savings_movements
            WHERE TRIM(telegram_id) = %s {condicao_data};
        """, params_base)
        row_poup = cursor.fetchone()
        depositos_periodo = float(row_poup[0]) if row_poup else 0.0
        saques_periodo = float(row_poup[1]) if row_poup else 0.0
        poupanca_liquida = depositos_periodo - saques_periodo

        # Saldo livre = receitas - despesas - poupança líquida (guardar reduz, sacar aumenta).
        # As despesas e as categorias continuam SEM a poupança: guardar não é gastar.
        saldo = total_renda - total_despesas - poupanca_liquida
        cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (telegram_id,))
        meta_row = cursor.fetchone()
        meta_poupanca = float(meta_row[0]) if meta_row else 0.0

        # Categoria
        query_categorias = f"""
            SELECT categoria, SUM(valor) 
            FROM transactions 
            WHERE TRIM(telegram_id) = %s AND tipo = 'despesa' {condicao_data}
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
            WHERE TRIM(telegram_id) = %s {condicao_data};
        """
        cursor.execute(query_ultima_atividade, params_base)
        last_row = cursor.fetchone()
        ultima_atividade = last_row[0] if last_row and last_row[0] else None

        # Investimentos (acumulado de sempre, como a poupança).
        # Fica ANTES do close() e isolado: se falhar, o resto do dashboard continua funcionando.
        invest_entradas = invest_saidas = 0.0
        try:
            cursor.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN tipo = 'entrada' THEN valor ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN tipo = 'saida' THEN valor ELSE 0 END), 0)
                FROM investments
                WHERE TRIM(telegram_id) = %s;
            """, (telegram_id,))
            row_inv = cursor.fetchone()
            invest_entradas = float(row_inv[0] or 0)
            invest_saidas = float(row_inv[1] or 0)
        except Exception as e:
            conexao.rollback()
            print(f"Aviso: investimentos indisponíveis no dashboard: {e}")
        total_investimentos = invest_entradas - invest_saidas

        # Evolução do saldo: saldo acumulado ao fim de cada um dos 6 últimos meses (termina no mês filtrado).
        # Mesma regra do card de saldo: receitas - despesas - poupança líquida. Isolado: se falhar, o resto segue.
        evolucao = None
        try:
            mes_fim = validar_mes(mes_filtro) or datetime.now().strftime('%Y-%m')
            meses_ev = [deslocar_mes(mes_fim, -i) for i in range(5, -1, -1)]
            cursor.execute("""
                SELECT TO_CHAR(data, 'YYYY-MM'),
                       COALESCE(SUM(CASE WHEN tipo = 'renda' THEN valor ELSE 0 END), 0)
                     - COALESCE(SUM(CASE WHEN tipo = 'despesa' THEN valor ELSE 0 END), 0)
                FROM transactions
                WHERE TRIM(telegram_id) = %s AND data IS NOT NULL
                GROUP BY 1;
            """, (telegram_id,))
            liquido_mes = {k: float(v) for k, v in cursor.fetchall()}
            cursor.execute("""
                SELECT TO_CHAR(data, 'YYYY-MM'),
                       COALESCE(SUM(CASE WHEN tipo = 'deposito' THEN valor ELSE 0 END), 0)
                     - COALESCE(SUM(CASE WHEN tipo = 'saque' THEN valor ELSE 0 END), 0)
                FROM savings_movements
                WHERE TRIM(telegram_id) = %s AND data IS NOT NULL
                GROUP BY 1;
            """, (telegram_id,))
            for k, v in cursor.fetchall():
                liquido_mes[k] = liquido_mes.get(k, 0.0) - float(v)
            acumulado_ev = sum(v for k, v in liquido_mes.items() if k < meses_ev[0])
            evolucao = {"labels": [], "valores": []}
            for k in meses_ev:
                acumulado_ev += liquido_mes.get(k, 0.0)
                evolucao["labels"].append(rotulo_mes(k))
                evolucao["valores"].append(round(acumulado_ev, 2))
        except Exception as e:
            conexao.rollback()
            evolucao = None
            print(f"Aviso: evolução do saldo indisponível no dashboard: {e}")

        cursor.close()
        conexao.close()

        return jsonify({
            "usuario": nome_usuario,
            "totais": {
                "renda": total_renda,
                "despesa": total_despesas,
                "saldo": saldo,
                "poupanca": total_poupanca,
                "poupanca_liquida": poupanca_liquida,
                "depositos": depositos_periodo,
                "saques": saques_periodo,
                "meta_poupanca": meta_poupanca
            },
            "categorias": {
                "labels": categorias,
                "valores": valores_categorias
            },
            "chatbot": {
                "ultima_atividade": ultima_atividade,
                "vinculado": bool(vinculado)
            },
            "investimentos": {
                "entradas": invest_entradas,
                "saidas": invest_saidas,
                "total": total_investimentos
            },
            "evolucao": evolucao

        })

    except Exception as e:
        print(f"Erro interno no servidor (dados-dashboard): {e}")
        traceback.print_exc()
        return jsonify({"erro": "Erro interno ao carregar o dashboard."}), 500
            
def rodar_flask():
    """Roda o servidor web na porta 5000"""
    app_web.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# Inicializa o cliente da Groq
cliente_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ==========================================
# LÓGICA DE NEGÓCIO E INTEGRAÇÃO GROQ
# ==========================================
def interpretar_mensagem_financeira(texto_usuario: str, telegram_id=None) -> dict:
    """Envia o texto para a Groq (Llama 3) e retorna um dicionário JSON estruturado."""
    cats_despesa = ", ".join(categorias_do_usuario(telegram_id, 'despesa'))
    cats_renda = ", ".join(categorias_do_usuario(telegram_id, 'renda'))
    prompt = f"""
    Você é um assistente financeiro especialista em extrair lançamentos de texto livre.
    Analise o texto a seguir: "{texto_usuario}"

    CATEGORIAS DE DESPESA:
    - {cats_despesa}.

    CATEGORIAS DE RENDA:
    - {cats_renda}.

    REGRAS DE CATEGORIA:
    - Escolha SEMPRE uma categoria da lista quando ela servir de verdade.
    - "Outros" (despesa) e "Outras Rendas" (renda) são o ÚLTIMO recurso: só para itens impossíveis de classificar
      (ex.: "coisas", "diversos"). Qualquer item concreto que não se encaixe na lista (ex.: computador, celular,
      presente, roupa, veterinário, academia) deve receber em "categoria" um NOME NOVO, curto (1 a 2 palavras),
      como "Eletrônicos", "Vestuário", "Pets", "Saúde".
    - Em "categoria_sugerida" coloque SEMPRE o nome específico e curto que você daria a este item
      (ex.: computador -> "Eletrônicos"), mesmo quando escolher uma categoria da lista.

    REGRA DE MOVIMENTO (muito importante):
    - Colocar dinheiro em investimentos (investi, apliquei, aportei, comprei CDB/ações/tesouro/FII/cripto) ou
      resgatar/tirar de investimentos NÃO é receita nem despesa: use "movimento": "investimento".
    - Guardar ou sacar da poupança/reserva de emergência também NÃO é receita nem despesa: use "movimento": "poupanca".
    - Nesses dois casos "direcao" é "entrada" (colocou/guardou/aportou) ou "saida" (resgatou/sacou/retirou).
    - Rendimentos RECEBIDOS (dividendos, juros, lucro de venda) continuam sendo "renda" normal, com "movimento": "nenhum".
    - Em todo o resto, use "movimento": "nenhum" e "direcao": "entrada".

    Retorne APENAS o JSON no formato:
    {{
        "descricao": "Nome legível do item",
        "valor": 0.00,
        "categoria": "Nome da Categoria",
        "categoria_sugerida": "Nome curto e específico",
        "tipo": "despesa" ou "renda",
        "movimento": "nenhum" ou "investimento" ou "poupanca",
        "direcao": "entrada" ou "saida"
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
    conexao = obter_conexao()
    try:
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
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()

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
            "Olá! 👋 Sou o Fincash, o seu assistente financeiro pessoal.\n\n"
            "Estou aqui para te ajudar a registrar gastos, organizar as suas contas e dar dicas de economia.\n\n"
            "Como posso ajudar hoje? 😊"
        )

    prompt = f"""
    Você é o Fincash, assistente financeiro pessoal.
    Análise a mensagem: "{texto_usuario}"

    1. Se for sobre finanças/economia: responda de forma prática, em português do Brasil.
       ESTILO (muito importante):
       - Tom amigável e acolhedor, como uma conversa com um amigo que entende de dinheiro.
       - No máximo 3 parágrafos curtos (2 a 3 frases cada), com uma LINHA EM BRANCO entre eles.
       - Sem Markdown: nada de **, #, tabelas ou crases. Use no máximo 1 ou 2 emojis.
       - Se precisar listar, use linhas começando com "• ", no máximo 4 itens.
       - Até 700 caracteres.
    2. Se for sobre outro assunto: responda EXATAMENTE:
       "⚠️ Esta mensagem vai contra as diretrizes do nosso sistema. Caso precise de ajuda ou suporte, entre em contato com nosso SAC através do e-mail: {SAC_EMAIL}"
    """

    resposta = cliente_groq.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return limpar_resposta_ia(resposta.choices[0].message.content, limite=1200)

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
    # Resposta a uma pergunta em andamento (nova categoria / confirmar exclusão)
    if await tratar_conversa_pendente(update, context, user_id, texto_usuario):
        return

    # Saudação ("oi", "bom dia", /start) e menu principal (/menu). Vem antes do parser de gastos.
    if await interceptar_saudacao_e_menu(update, context, user_id, texto_usuario):
        return

    # Módulo de investimentos (menu, "dicas de investimentos", conversa com a IA).
    # Precisa vir ANTES do parser de gastos, senão a mensagem nunca chega nele.
    if await investimentos.interceptar_mensagem(update, context, user_id, texto_usuario):
        return

    # Categorias: listar, criar e excluir
    if PADRAO_VER_CATEGORIAS.match(texto_usuario):
        await responder_lista_categorias(update, user_id)
        return
    m_cat = PADRAO_CRIAR_CATEGORIA.match(texto_usuario)
    if m_cat:
        await criar_categoria_manual(update, user_id, m_cat.group(2), (m_cat.group(1) or 'despesa').lower())
        return
    m_cat = PADRAO_EXCLUIR_CATEGORIA.match(texto_usuario)
    if m_cat:
        await iniciar_exclusao_categoria(update, context, user_id, m_cat.group(2).strip(),
                                         m_cat.group(1).lower() if m_cat.group(1) else None)
        return

    # Interceptador de Exclusão
    padrao_exclusao = r'^(?:apagar|deletar|excluir|remover)\s+(?:id\s*|#\s*)?(\d+)$'
    match_exclusao = re.match(padrao_exclusao, texto_lower)

    if match_exclusao:
        await pedir_confirmacao_exclusao_lancamento(update, context, user_id, int(match_exclusao.group(1)))
        return

    # Interceptador de Meta de poupança ("definir meta de 5000", "remover meta", "minha meta")
    if PADRAO_REMOVER_META.match(texto_lower):
        await pedir_confirmacao_remover_meta(update, context, user_id)
        return
    if PADRAO_VER_META.match(texto_lower):
        await responder_ver_meta(update, user_id)
        return
    m_meta = (PADRAO_DEFINIR_META.match(texto_lower) or PADRAO_META_E.match(texto_lower)
              or PADRAO_QUERO_JUNTAR.match(texto_lower))
    if m_meta:
        await responder_definir_meta(update, user_id, m_meta.group(1))
        return

    # Interceptador de Poupança (guardar / sacar / consultar)
    m_guardar = PADRAO_POUPAR.match(texto_lower) or PADRAO_DEPOSITAR.match(texto_lower)
    if m_guardar:
        await responder_movimento_poupanca(update, user_id, 'deposito', m_guardar.group(1), m_guardar.group(2))
        return
    m_sacar = PADRAO_SACAR.match(texto_lower)
    if m_sacar:
        await responder_movimento_poupanca(update, user_id, 'saque', m_sacar.group(1), m_sacar.group(2))
        return
    if PADRAO_VER_POUPANCA.match(texto_lower):
        await comando_poupanca(update, context)
        return

    # Interceptador de Investimentos (aportar / resgatar) -> tabela investments, igual à poupança com savings_movements
    mov_inv = interpretar_movimento_investimento(texto_lower)
    if mov_inv:
        await responder_movimento_investimento(update, user_id, *mov_inv)
        return

    # Interceptador de Previsão ("qual a minha previsão de gasto para o mês que vem?")
    if eh_pedido_previsao(texto_usuario):
        await responder_previsao(update, user_id)
        return

    # Interceptador de Resumo
    termos_resumo = ["quanto eu gastei", "quanto gastei", "quanto eu tenho", "meu saldo", "resumo de gastos", "quanto já gastei"]
    if any(termo in texto_lower for termo in termos_resumo):
        await comando_resumo(update, context)
        return

    # Lançamentos Financeiros e Consultoria IA
    try:
        dados = interpretar_mensagem_financeira(texto_usuario, user_id)
        if dados.get("valor") and float(dados["valor"]) > 0:
            # Investimento e poupança são movimentos próprios (tabelas investments / savings_movements),
            # nunca receita/despesa nem categoria.
            movimento = str(dados.get("movimento") or "nenhum").strip().lower()
            if movimento in ("investimento", "poupanca", "poupança"):
                colocou = str(dados.get("direcao") or "entrada").strip().lower() != "saida"
                valor_txt = f"{float(dados['valor']):.2f}".replace(".", ",")
                desc_mov = str(dados.get("descricao") or "")[:200]
                if movimento == "investimento":
                    await responder_movimento_investimento(update, user_id, 'entrada' if colocou else 'saida', valor_txt, desc_mov)
                else:
                    await responder_movimento_poupanca(update, user_id, 'deposito' if colocou else 'saque', valor_txt, desc_mov)
                return

            dados["valor"] = float(dados["valor"])
            dados["tipo"] = "renda" if dados.get("tipo") == "renda" else "despesa"
            dados["descricao"] = dados.get("descricao") or texto_usuario[:100]

            existente = achar_categoria(user_id, dados["tipo"], dados.get("categoria"))

            # A IA caiu em "Outros"/"Outras Rendas", mas deu um nome específico (ex.: computador -> Eletrônicos):
            # se esse nome já existe, usa; se não existe, trata como categoria nova e PERGUNTA em vez de ir direto.
            generica = CATEGORIA_GENERICA[dados["tipo"]]
            if existente and normalizar(existente) == normalizar(generica):
                sugerida = limpar_nome_categoria(dados.get("categoria_sugerida"))
                sem_valor = {normalizar(x) for x in ("outros", "outro", "outras rendas", "outra renda", "diversos", "geral", "variados")}
                if len(sugerida) >= 2 and normalizar(sugerida) not in sem_valor:
                    ja_existe = achar_categoria(user_id, dados["tipo"], sugerida)
                    if ja_existe:
                        existente = ja_existe
                    else:
                        existente = None
                        dados["categoria"] = sugerida

            if existente:
                dados["categoria"] = existente
            else:
                nome_novo = limpar_nome_categoria(dados.get("categoria"))
                if len(nome_novo) < 2:
                    dados["categoria"] = CATEGORIA_GENERICA[dados["tipo"]]
                else:
                    # categoria desconhecida: pergunta se cria ou se usa "Outros"
                    _iniciar_conversa(context, 'nova_categoria', dados=dados, tipo=dados["tipo"], nome=nome_novo)
                    await update.message.reply_text(
                        montar_pergunta_nova_categoria(nome_novo, dados, dados["tipo"]), parse_mode="Markdown")
                    return

            await salvar_e_responder_lancamento(update, user_id, dados)
            return
    except Exception:
        # Antes era "pass": qualquer falha (Groq fora do ar, banco, JSON ruim) sumia sem rastro.
        print("⚠ Falha ao interpretar/salvar lançamento; seguindo para a consultoria:")
        traceback.print_exc()

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

    guardado = saldo_poupanca(cursor, user_id)

    cursor.close()
    conexao.close()

    saldo = float(total_renda) - float(total_despesas) - guardado
    alerta_saldo = "⚠️ **Atenção:** Seus gastos superaram a sua renda!" if saldo < 0 else "✅ **Saúde Financeira:** Saldo positivo!"

    texto_categorias = "\n".join([f"• **{cat}:** R$ {val:.2f}" for cat, val in categorias]) or "Nenhuma despesa registrada."

    mensagem = (
        f"📊 **SEU BALANÇO FINANCEIRO**\n\n"
        f"💵 **Total Recebido (Renda):** R$ {total_renda:.2f}\n"
        f"💸 **Total Gasto (Despesas):** R$ {total_despesas:.2f}\n"
        f"🐷 **Guardado na poupança:** R$ {guardado:.2f}\n"
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

    await pedir_confirmacao_exclusao_lancamento(update, context, user_id, id_deletar)

# ==========================================
# CATEGORIAS PERSONALIZADAS E CONFIRMAÇÃO ANTES DE EXCLUIR (TELEGRAM)
# ==========================================
CATEGORIAS_RENDA = ["Salário", "Freelance", "Investimentos", "Outras Rendas"]
# Categoria "coringa" de cada tipo: é onde o lançamento cai quando a pessoa escolhe a opção 2
CATEGORIA_GENERICA = {"despesa": "Outros", "renda": "Outras Rendas"}
TEMPO_CONVERSA_SEG = 600                         # pergunta sem resposta expira em 10 minutos

AVISO_SEM_DESFAZER = (
    "⚠️ Depois de confirmar, a ação *não poderá mais ser desfeita*.\n"
    f"Para mais informações, entre em contato com o SAC: `{SAC_EMAIL}`"
)

PADRAO_VER_CATEGORIAS = re.compile(r'^(?:/categorias|minhas categorias|ver categorias|listar categorias)$', re.I)
PADRAO_CRIAR_CATEGORIA = re.compile(
    r'^(?:criar|nova|novo|adicionar|cadastrar)\s+(?:uma\s+)?(?:nova\s+)?categoria(?:\s+de\s+(despesa|renda))?\s+(.+)$', re.I)
PADRAO_EXCLUIR_CATEGORIA = re.compile(
    r'^(?:apagar|deletar|excluir|remover)\s+(?:a\s+)?categoria(?:\s+de\s+(despesa|renda))?\s+(.+)$', re.I)

# Respostas aceitas (já sem acento, minúsculas e sem pontuação)
RESP_CANCELAR = {"cancelar", "cancela", "cancel", "desistir", "esquece"}
RESP_CRIAR = {"1", "um", "criar", "criar categoria", "criar nova", "criar nova categoria", "criar a categoria",
              "nova", "nova categoria", "quero criar", "sim", "s"}
RESP_OUTROS = {"2", "dois", "outros", "outro", "outras rendas", "em outros", "colocar em outros",
               "pode ser outros", "nao", "n"}
RESP_PERSONALIZADA = {"3", "tres", "personalizada", "personalizar", "categoria personalizada", "criar personalizada",
                      "criar categoria personalizada", "criar uma categoria personalizada", "outro nome",
                      "escolher nome", "escolher o nome", "quero escolher", "quero personalizar"}
RESP_SIM = {"sim", "s", "yes", "y", "confirmo", "confirmar", "pode", "pode excluir", "claro", "ok"}
RESP_NAO = {"nao", "n", "no", "cancelar", "cancela", "cancel", "desistir", "voltar"}


def limpar_nome_categoria(texto) -> str:
    """Deixa só letras, números, espaço, & e - (no máximo 30 caracteres) e põe a 1ª letra maiúscula."""
    texto = str(texto or '').replace('_', ' ')
    texto = re.sub(r'[^\w\s&-]', '', texto)
    texto = re.sub(r'\s+', ' ', texto).strip()[:30].strip()
    return texto[:1].upper() + texto[1:]


def md_seguro(texto) -> str:
    """Tira os caracteres que quebram o Markdown do Telegram."""
    return re.sub(r'[*_`\[\]]', '', str(texto or ''))


def _resposta_limpa(texto) -> str:
    return re.sub(r'[^\w\s]', '', normalizar(texto)).strip()


def _plural(n, singular, plural) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _listar_categorias(cursor, tid, tipo):
    """Retorna (padrão, extras). Extras = criadas pela pessoa + qualquer categoria que já aparece nos lançamentos."""
    padrao = list(CATEGORIAS_DESPESA if tipo == 'despesa' else CATEGORIAS_RENDA)
    vistos = {normalizar(c) for c in padrao}
    cursor.execute("SELECT nome FROM categories WHERE telegram_id = %s AND tipo = %s ORDER BY nome;", (tid, tipo))
    nomes = [r[0] for r in cursor.fetchall()]
    cursor.execute("""
        SELECT DISTINCT TRIM(categoria) FROM transactions
        WHERE TRIM(telegram_id) = %s AND tipo = %s AND categoria IS NOT NULL AND TRIM(categoria) <> ''
        ORDER BY 1;
    """, (tid, tipo))
    nomes += [r[0] for r in cursor.fetchall()]
    extras = []
    for n in nomes:
        k = normalizar(n)
        if k and k not in vistos:
            vistos.add(k)
            extras.append(n)
    return padrao, extras


def categorias_do_usuario(telegram_id, tipo='despesa'):
    """Categorias padrão + as da pessoa (usado no Telegram, nos orçamentos e no prompt da IA)."""
    if not telegram_id:
        return list(CATEGORIAS_DESPESA if tipo == 'despesa' else CATEGORIAS_RENDA)
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            padrao, extras = _listar_categorias(cursor, str(telegram_id).strip(), tipo)
        return padrao + extras
    finally:
        conexao.close()


def achar_categoria(telegram_id, tipo, nome):
    """Devolve o nome oficial da categoria (ignora maiúscula/acento) ou None se ela não existir."""
    alvo = normalizar(nome)
    if not alvo:
        return None
    for c in categorias_do_usuario(telegram_id, tipo):
        if normalizar(c) == alvo:
            return c
    return None


def criar_categoria(telegram_id, tipo, nome):
    """Retorna ('criada' | 'existe' | 'invalida', nome_final)."""
    telegram_id = str(telegram_id).strip()
    nome = limpar_nome_categoria(nome)
    if len(nome) < 2:
        return 'invalida', nome
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", ('categoria:' + telegram_id,))
            padrao, extras = _listar_categorias(cursor, telegram_id, tipo)
            for existente in padrao + extras:
                if normalizar(existente) == normalizar(nome):
                    conexao.rollback()
                    return 'existe', existente
            cursor.execute("INSERT INTO categories (telegram_id, nome, tipo) VALUES (%s, %s, %s);",
                           (telegram_id, nome, tipo))
        conexao.commit()
        return 'criada', nome
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def localizar_categoria_para_excluir(telegram_id, nome, tipo=None):
    """Retorna (status, tipo, nome_oficial). status: 'ok' | 'padrao' | 'nao_existe' | 'ambigua'."""
    alvo = normalizar(nome)
    achadas, e_padrao = [], False
    for t in ([tipo] if tipo else ['despesa', 'renda']):
        padrao = CATEGORIAS_DESPESA if t == 'despesa' else CATEGORIAS_RENDA
        if alvo in {normalizar(c) for c in padrao}:
            e_padrao = True
            continue
        for c in categorias_do_usuario(telegram_id, t):
            if normalizar(c) == alvo and normalizar(c) not in {normalizar(p) for p in padrao}:
                achadas.append((t, c))
    if len(achadas) == 1:
        return 'ok', achadas[0][0], achadas[0][1]
    if len(achadas) > 1:
        return 'ambigua', None, None
    return ('padrao' if e_padrao else 'nao_existe'), None, None


def _dados_da_categoria(cursor, tid, tipo, nome):
    """Tudo que está ligado à categoria: lançamentos, limite de orçamento e o cadastro dela."""
    alvo = normalizar(nome)
    cursor.execute("""
        SELECT id, valor, descricao, data, categoria FROM transactions
        WHERE TRIM(telegram_id) = %s AND tipo = %s ORDER BY data, id;
    """, (tid, tipo))
    lancs = [r for r in cursor.fetchall() if normalizar(r[4]) == alvo]
    orcamentos = []
    if tipo == 'despesa':
        cursor.execute("SELECT id, categoria, limite FROM budgets WHERE telegram_id = %s;", (tid,))
        orcamentos = [r for r in cursor.fetchall() if normalizar(r[1]) == alvo]
    cursor.execute("SELECT id, nome FROM categories WHERE telegram_id = %s AND tipo = %s;", (tid, tipo))
    cadastros = [r for r in cursor.fetchall() if normalizar(r[1]) == alvo]
    return lancs, orcamentos, cadastros


def _resumir(tipo, nome, lancs, orcamentos):
    return {
        "nome": nome, "tipo": tipo,
        "lancamentos": [{"id": r[0], "valor": float(r[1]), "descricao": r[2],
                         "data": r[3].strftime('%d/%m/%Y') if r[3] else ''} for r in lancs],
        "total": round(sum(float(r[1]) for r in lancs), 2),
        "limite": float(orcamentos[0][2]) if orcamentos else None,
    }


def resumo_exclusao_categoria(telegram_id, tipo, nome):
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            lancs, orcamentos, _ = _dados_da_categoria(cursor, str(telegram_id).strip(), tipo, nome)
        return _resumir(tipo, nome, lancs, orcamentos)
    finally:
        conexao.close()


def excluir_categoria(telegram_id, tipo, nome):
    """Apaga a categoria, os lançamentos dela e o limite de orçamento. Os lançamentos vão para
    transactions_backup e um registro fica em dados_salvos (auditoria). Tudo na mesma transação."""
    tid = str(telegram_id).strip()
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s));", ('categoria:' + tid,))
            lancs, orcamentos, cadastros = _dados_da_categoria(cursor, tid, tipo, nome)
            resumo = _resumir(tipo, nome, lancs, orcamentos)
            if lancs:
                cursor.execute("""
                    WITH removidos AS (
                        DELETE FROM transactions
                        WHERE id = ANY(%s) AND TRIM(telegram_id) = %s
                        RETURNING id, telegram_id, tipo, valor, categoria, descricao, data
                    )
                    INSERT INTO transactions_backup
                        (id_original, telegram_id, tipo, valor, categoria, descricao, data)
                    SELECT id, telegram_id, tipo, valor, categoria, descricao, data FROM removidos;
                """, ([r[0] for r in lancs], tid))
            if orcamentos:
                cursor.execute("DELETE FROM budgets WHERE id = ANY(%s) AND telegram_id = %s;",
                               ([r[0] for r in orcamentos], tid))
            if cadastros:
                cursor.execute("DELETE FROM categories WHERE id = ANY(%s) AND telegram_id = %s;",
                               ([r[0] for r in cadastros], tid))
            cursor.execute("SELECT id FROM users WHERE telegram_id = %s;", (tid,))
            u = cursor.fetchone()
            cursor.execute("""
                INSERT INTO dados_salvos (user_id, tipo_operacao, dados_antigos)
                VALUES (%s, 'exclusao_categoria', %s::jsonb);
            """, (u[0] if u else None, json.dumps(resumo, ensure_ascii=False)))
        conexao.commit()
        return resumo
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def obter_transacao(telegram_id, id_transacao):
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT id, tipo, valor, categoria, descricao, TO_CHAR(data, 'DD/MM/YYYY HH24:MI')
                FROM transactions WHERE id = %s AND TRIM(telegram_id) = %s;
            """, (id_transacao, str(telegram_id).strip()))
            return cursor.fetchone()
    finally:
        conexao.close()


# ---------- mensagens (texto simples e direto) ----------
def montar_pergunta_nova_categoria(nome, dados, tipo):
    generica = CATEGORIA_GENERICA[tipo]
    return (
        f"🤔 Ainda não existe uma categoria para *{md_seguro(nome)}*.\n\n"
        f"📌 {md_seguro(dados.get('descricao'))} · {fmt_brl(dados['valor'])}\n\n"
        "O que você prefere?\n"
        f"1️⃣ Criar a categoria *{md_seguro(nome)}*\n"
        f"2️⃣ Colocar em *{generica}*\n"
        "3️⃣ Criar uma categoria personalizada (você escolhe o nome)\n\n"
        "Responda *1*, *2* ou *3* (se preferir, pode escrever \"criar\", \"outros\" ou \"personalizada\")."
    )


def montar_aviso_exclusao_categoria(info):
    nome = md_seguro(info['nome'])
    tipo_txt = "despesa" if info['tipo'] == 'despesa' else "renda"
    linhas = [
        f"⚠️ *Tem certeza que deseja excluir a categoria {nome}?*",
        "",
        "Veja tudo o que será apagado:",
        f"🏷️ Categoria *{nome}* ({tipo_txt})",
    ]
    qtd = len(info['lancamentos'])
    if qtd:
        linhas.append(f"🧾 {_plural(qtd, 'lançamento', 'lançamentos')}, somando {fmt_brl(info['total'])}:")
        for l in info['lancamentos'][:10]:
            linhas.append(f"   • #{l['id']:02d} · {l['data']} · {md_seguro(l['descricao'])[:30]} · {fmt_brl(l['valor'])}")
        if qtd > 10:
            linhas.append(f"   … e mais {qtd - 10}")
        linhas.append("   (eles também deixam de contar no seu saldo e nos gráficos)")
    else:
        linhas.append("🧾 Nenhum lançamento nesta categoria")
    if info['limite'] is not None:
        linhas.append(f"💰 Limite mensal de orçamento: {fmt_brl(info['limite'])}")
    linhas += ["", "Responda *sim* para excluir ou *não* para cancelar.", "", AVISO_SEM_DESFAZER]
    return "\n".join(linhas)


def montar_aviso_exclusao_lancamento(row):
    id_, tipo, valor, categoria, descricao, data = row
    return (
        f"⚠️ *Tem certeza que deseja excluir este lançamento?*\n\n"
        "Veja o que será apagado:\n"
        f"🆔 #{id_:02d}\n"
        f"📌 {md_seguro(descricao)}\n"
        f"💰 {fmt_brl(valor)}\n"
        f"🏷️ {md_seguro(categoria) or 'Sem categoria'}\n"
        f"📊 {'Renda' if tipo == 'renda' else 'Despesa'}\n"
        f"📅 {data}\n\n"
        "Responda *sim* para excluir ou *não* para cancelar.\n\n"
        f"{AVISO_SEM_DESFAZER}"
    )


# ---------- conversa em andamento (guardada na memória do bot, por usuário) ----------
def _iniciar_conversa(context, etapa, **dados):
    context.user_data['conversa'] = {"etapa": etapa, "criado_em": time.time(), **dados}


def _conversa_ativa(context):
    c = context.user_data.get('conversa')
    if not c:
        return None
    if time.time() - c['criado_em'] > TEMPO_CONVERSA_SEG:
        context.user_data.pop('conversa', None)
        return None
    return c

# ==========================================
# ALERTAS DE TETO DE GASTOS (teto = renda do mês)
# ==========================================
def avaliar_alerta_teto(telegram_id):
    """Se o gasto do mês chegou perto (ou passou) da renda e esse nível ainda não foi avisado,
    registra o aviso e devolve os dados para enviar. Senão devolve None."""
    tid = str(telegram_id).strip()
    mes = datetime.now().strftime('%Y-%m')
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT ativo, limiar FROM alert_settings WHERE telegram_id = %s;", (tid,))
            cfg = cursor.fetchone()
            ativo, limiar = cfg if cfg else (True, 80)
            if not ativo:
                return None

            cursor.execute("""
                SELECT COALESCE(SUM(CASE WHEN tipo = 'renda' THEN valor END), 0),
                       COALESCE(SUM(CASE WHEN tipo = 'despesa' THEN valor END), 0)
                FROM transactions
                WHERE TRIM(telegram_id) = %s AND TO_CHAR(data, 'YYYY-MM') = %s;
            """, (tid, mes))
            renda, despesa = (float(x) for x in cursor.fetchone())
            if renda <= 0:
                return None   # sem renda no mês não existe teto para comparar

            pct = despesa / renda * 100
            if pct >= 100:
                nivel = 'estourou'
            elif pct >= limiar:
                nivel = 'proximo'
            else:
                return None

            # A constraint UNIQUE garante 1 aviso por nível/mês, mesmo com mensagens simultâneas
            cursor.execute("""
                INSERT INTO alerts_sent (telegram_id, mes, nivel) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING id;
            """, (tid, mes, nivel))
            if cursor.fetchone() is None:
                return None
            if nivel == 'estourou':   # pulou direto: não manda o aviso "próximo" depois
                cursor.execute("""
                    INSERT INTO alerts_sent (telegram_id, mes, nivel) VALUES (%s, %s, 'proximo')
                    ON CONFLICT DO NOTHING;
                """, (tid, mes))

            cursor.execute("SELECT nome, email, COALESCE(email_invalido, FALSE) FROM users WHERE telegram_id = %s;", (tid,))
            u = cursor.fetchone()
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()

    nome, email, email_invalido = u if u else ("", None, True)
    return {"nivel": nivel, "pct": pct, "renda": renda, "despesa": despesa,
            "restante": renda - despesa, "nome": (nome or "").split()[0] if nome else "",
            "email": None if email_invalido else email}


def montar_textos_alerta(a):
    if a["nivel"] == 'estourou':
        titulo = "🚨 Você ultrapassou o seu teto de gastos!"
        detalhe = f"Você já gastou {fmt_brl(a['despesa'])} neste mês, {fmt_brl(a['despesa'] - a['renda'])} acima da sua renda ({fmt_brl(a['renda'])})."
    else:
        titulo = "⚠️ Você está perto do seu teto de gastos!"
        detalhe = (f"Você já usou {a['pct']:.0f}% da sua renda do mês: {fmt_brl(a['despesa'])} de {fmt_brl(a['renda'])}. "
                   f"Ainda restam {fmt_brl(a['restante'])}.")
    telegram = f"{titulo}\n\n{detalhe}\n\nPara ajustar os avisos: /alertas"
    html = f"""
    <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
        <h2 style="color: #2563eb; text-align: center;">fincash</h2>
        <hr style="border: 0; border-top: 1px solid #eee;">
        <p>Olá{', <strong>' + escapar_html(a['nome']) + '</strong>' if a['nome'] else ''}!</p>
        <p><strong>{titulo}</strong></p>
        <p>{detalhe}</p>
        <p style="font-size: 13px; color: #64748b;">Para desativar ou ajustar esses avisos, envie /alertas no Telegram.</p>
    </div>
    """
    return titulo, telegram, html


async def enviar_alertas_teto(update: Update, user_id: str):
    """Chamada depois de registrar uma despesa. Nunca deve derrubar o registro do lançamento."""
    try:
        a = avaliar_alerta_teto(user_id)
        if not a:
            return
        titulo, texto_telegram, html = montar_textos_alerta(a)
        await update.message.reply_text(texto_telegram)
        if a["email"]:
            with app_web.app_context():
                msg = Message(subject=titulo, recipients=[a["email"]], body=texto_telegram, html=html)
            threading.Thread(target=enviar_email_async, args=(app_web, msg), daemon=True).start()
        else:
            print("⚠ Alerta por e-mail não enviado: usuário sem e-mail ou com e-mail marcado como inválido.")
    except Exception:
        print("⚠ Falha ao avaliar/enviar alerta de teto:")
        traceback.print_exc()


def salvar_config_alertas(telegram_id, ativo=None, limiar=None):
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                INSERT INTO alert_settings (telegram_id, ativo, limiar)
                VALUES (%s, COALESCE(%s, TRUE), COALESCE(%s, 80))
                ON CONFLICT (telegram_id) DO UPDATE
                SET ativo = COALESCE(%s, alert_settings.ativo),
                    limiar = COALESCE(%s, alert_settings.limiar);
            """, (str(telegram_id).strip(), ativo, limiar, ativo, limiar))
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


async def comando_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return

    arg = context.args[0].strip().lower().rstrip('%') if context.args else ""
    if arg in ("on", "ligar", "ativar"):
        salvar_config_alertas(user_id, ativo=True)
        await update.message.reply_text("🔔 Alertas de teto de gastos *ativados*.", parse_mode="Markdown")
        return
    if arg in ("off", "desligar", "desativar"):
        salvar_config_alertas(user_id, ativo=False)
        await update.message.reply_text("🔕 Alertas de teto de gastos *desativados*.", parse_mode="Markdown")
        return
    if arg.isdigit():
        n = int(arg)
        if not 50 <= n <= 99:
            await update.message.reply_text("⚠️ Escolha um percentual entre 50 e 99. Exemplo: `/alertas 90`", parse_mode="Markdown")
            return
        salvar_config_alertas(user_id, ativo=True, limiar=n)
        await update.message.reply_text(f"✅ Vou te avisar quando você usar *{n}%* da renda do mês.", parse_mode="Markdown")
        return

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT ativo, limiar FROM alert_settings WHERE telegram_id = %s;", (user_id,))
            cfg = cursor.fetchone()
    finally:
        conexao.close()
    ativo, limiar = cfg if cfg else (True, 80)
    await update.message.reply_text(
        f"{'🔔' if ativo else '🔕'} *Alertas de teto de gastos:* {'ativados' if ativo else 'desativados'}\n"
        f"🎯 Aviso ao usar *{limiar}%* da renda do mês (e outro ao passar de 100%).\n"
        "📬 Enviados pelo Telegram e por e-mail.\n\n"
        "• `/alertas on` ou `/alertas off`\n"
        "• `/alertas 90` para mudar o percentual",
        parse_mode="Markdown")

async def salvar_e_responder_lancamento(update: Update, user_id: str, dados: dict):
    transacao_id = salvar_transacao(user_id, dados)
    tipo_str = "🟢 Renda" if dados["tipo"] == "renda" else "🔴 Despesa"
    resposta = (
        f"✅ **Lançamento Registrado!**\n\n"
        f"🆔 **ID:** {transacao_id:02d}\n"
        f"📌 **Descrição:** {dados['descricao']}\n"
        f"💰 **Valor:** R$ {dados['valor']:.2f}\n"
        f"🏷️ **Categoria:** {dados['categoria']}\n"
        f"📊 **Tipo:** {tipo_str}"
    )
    await update.message.reply_text(resposta, parse_mode="Markdown")
    
    if dados["tipo"] == "despesa":
        await enviar_alertas_teto(update, user_id)


async def tratar_conversa_pendente(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, texto: str) -> bool:
    """Se existe uma pergunta esperando resposta, trata a resposta. Retorna True se a mensagem foi consumida."""
    conversa = _conversa_ativa(context)
    if not conversa:
        return False
    resp = _resposta_limpa(texto)
    etapa = conversa['etapa']
    responder = lambda msg: update.message.reply_text(msg, parse_mode="Markdown")

    try:
        if etapa == 'nova_categoria':
            dados, tipo = conversa['dados'], conversa['tipo']
            if resp in RESP_CANCELAR:
                context.user_data.pop('conversa', None)
                await responder("Tudo bem, cancelei. Nenhum lançamento foi registrado. 👍")
            elif resp in RESP_PERSONALIZADA:
                # próxima mensagem da pessoa será o nome da categoria
                _iniciar_conversa(context, 'nome_categoria', dados=dados, tipo=tipo)
                await responder("✏️ Qual nome você quer dar para a categoria?\n"
                                "Envie só o nome (ex.: *Pets*). Para desistir, escreva *cancelar*.")
            elif resp in RESP_CRIAR:
                status, nome = criar_categoria(user_id, tipo, conversa['nome'])
                dados['categoria'] = nome if status != 'invalida' else CATEGORIA_GENERICA[tipo]
                context.user_data.pop('conversa', None)
                if status == 'criada':
                    await responder(f"🏷️ Categoria *{md_seguro(nome)}* criada!")
                await salvar_e_responder_lancamento(update, user_id, dados)
            elif resp in RESP_OUTROS:
                dados['categoria'] = CATEGORIA_GENERICA[tipo]
                context.user_data.pop('conversa', None)
                await salvar_e_responder_lancamento(update, user_id, dados)
            else:
                await responder("Não entendi 😅 Responda *1* para criar a categoria, *2* para usar "
                                f"*{CATEGORIA_GENERICA[tipo]}*, *3* para escolher outro nome ou escreva *cancelar*.")
            return True

        if etapa == 'nome_categoria':
            dados, tipo = conversa['dados'], conversa['tipo']
            if resp in RESP_CANCELAR:
                context.user_data.pop('conversa', None)
                await responder("Tudo bem, cancelei. Nenhum lançamento foi registrado. 👍")
                return True
            status, nome = criar_categoria(user_id, tipo, texto)
            if status == 'invalida':
                conversa['criado_em'] = time.time()          # renova o prazo e pede de novo
                await responder("Esse nome não serve 😅 Use pelo menos 2 letras (ex.: *Pets*) ou escreva *cancelar*.")
                return True
            dados['categoria'] = nome
            context.user_data.pop('conversa', None)
            if status == 'criada':
                await responder(f"🏷️ Categoria *{md_seguro(nome)}* criada!")
            else:                                             # 'existe': já havia uma com esse nome, usa ela
                await responder(f"A categoria *{md_seguro(nome)}* já existe, vou usar ela. 👍")
            await salvar_e_responder_lancamento(update, user_id, dados)
            return True

        if etapa == 'excluir_categoria':
            if resp in RESP_SIM:
                context.user_data.pop('conversa', None)
                r = excluir_categoria(user_id, conversa['tipo'], conversa['nome'])
                msg = f"🗑️ Categoria *{md_seguro(r['nome'])}* excluída."
                if r['lancamentos']:
                    msg += f"\nForam apagados {_plural(len(r['lancamentos']), 'lançamento', 'lançamentos')} ({fmt_brl(r['total'])})."
                if r['limite'] is not None:
                    msg += "\nO limite de orçamento dela também foi removido."
                await responder(msg)
            elif resp in RESP_NAO:
                context.user_data.pop('conversa', None)
                await responder("Tudo bem, nada foi excluído. ✅")
            else:
                await responder("Responda *sim* para excluir ou *não* para cancelar.")
            return True

        if etapa == 'remover_meta':
            if resp in RESP_SIM:
                context.user_data.pop('conversa', None)
                await asyncio.to_thread(gravar_meta_poupanca, user_id, None)
                await responder("🗑️ Meta de poupança removida. O dinheiro guardado continua igual.")
            elif resp in RESP_NAO:
                context.user_data.pop('conversa', None)
                await responder("Tudo bem, a meta foi mantida. ✅")
            else:
                await responder("Responda *sim* para remover ou *não* para cancelar.")
            return True

        if etapa == 'excluir_lancamento':
            id_ = conversa['id']
            if resp in RESP_SIM:
                context.user_data.pop('conversa', None)
                if deletar_transacao_por_id(user_id, id_):
                    await responder(f"🗑️ Lançamento *#{id_:02d}* excluído com sucesso!")
                else:
                    await responder(f"❌ Nenhum lançamento encontrado com o ID *#{id_:02d}*.")
            elif resp in RESP_NAO:
                context.user_data.pop('conversa', None)
                await responder("Tudo bem, nada foi excluído. ✅")
            else:
                await responder("Responda *sim* para excluir ou *não* para cancelar.")
            return True
    except Exception as e:
        context.user_data.pop('conversa', None)
        print(f"❌ Erro na conversa pendente ({etapa}): {e}")
        await update.message.reply_text("❌ Ocorreu um erro. Nada foi alterado, tente de novo.")
        return True

    context.user_data.pop('conversa', None)
    return False


async def pedir_confirmacao_exclusao_lancamento(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, id_deletar: int):
    row = obter_transacao(user_id, id_deletar)
    if not row:
        await update.message.reply_text(f"❌ Nenhum lançamento encontrado com o ID **#{id_deletar:02d}**.", parse_mode="Markdown")
        return
    _iniciar_conversa(context, 'excluir_lancamento', id=id_deletar)
    await update.message.reply_text(montar_aviso_exclusao_lancamento(row), parse_mode="Markdown")


async def iniciar_exclusao_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, nome: str, tipo=None):
    status, tipo_ok, nome_ok = localizar_categoria_para_excluir(user_id, nome, tipo)
    nome_txt = md_seguro(nome)
    if status == 'padrao':
        await update.message.reply_text(f"🔒 *{nome_txt}* é uma categoria padrão do Fincash e não pode ser excluída.", parse_mode="Markdown")
    elif status == 'nao_existe':
        await update.message.reply_text(f"❌ Não encontrei a categoria *{nome_txt}*. Envie */categorias* para ver as suas.", parse_mode="Markdown")
    elif status == 'ambigua':
        await update.message.reply_text(
            f"Existe uma *{nome_txt}* em despesas e outra em rendas. Qual delas?\n"
            f"• `excluir categoria de despesa {nome}`\n• `excluir categoria de renda {nome}`", parse_mode="Markdown")
    else:
        info = resumo_exclusao_categoria(user_id, tipo_ok, nome_ok)
        _iniciar_conversa(context, 'excluir_categoria', tipo=tipo_ok, nome=nome_ok)
        await update.message.reply_text(montar_aviso_exclusao_categoria(info), parse_mode="Markdown")


async def criar_categoria_manual(update: Update, user_id: str, nome: str, tipo: str):
    status, nome_final = criar_categoria(user_id, tipo, nome)
    if status == 'invalida':
        await update.message.reply_text("⚠️ Esse nome não serve. Use de 2 a 30 letras, por exemplo: `criar categoria Pet`", parse_mode="Markdown")
    elif status == 'existe':
        await update.message.reply_text(f"ℹ️ A categoria *{md_seguro(nome_final)}* já existe.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"🏷️ Categoria *{md_seguro(nome_final)}* criada!\nAgora é só registrar normalmente, por exemplo: "
            f"`{md_seguro(nome_final).lower()} 50 reais`", parse_mode="Markdown")


async def responder_lista_categorias(update: Update, user_id: str):
    linhas = ["🏷️ *Suas categorias*"]
    for tipo, titulo in (('despesa', '🔴 Despesas'), ('renda', '🟢 Rendas')):
        conexao = obter_conexao()
        try:
            with conexao.cursor() as cursor:
                padrao, extras = _listar_categorias(cursor, user_id, tipo)
        finally:
            conexao.close()
        linhas += ["", f"*{titulo}*", ", ".join(md_seguro(c) for c in padrao)]
        if extras:
            linhas.append("➕ Criadas por você: " + ", ".join(md_seguro(c) for c in extras))
    linhas += ["", "Para criar: `criar categoria Pet`", "Para excluir: `excluir categoria Pet`"]
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def comando_categorias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return
    await responder_lista_categorias(update, user_id)


async def comando_nova_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return
    if not context.args:
        await update.message.reply_text("⚠️ **Uso correto:** `/novacategoria Pet`", parse_mode="Markdown")
        return
    await criar_categoria_manual(update, user_id, " ".join(context.args), 'despesa')


async def comando_excluir_categoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return
    if not context.args:
        await update.message.reply_text("⚠️ **Uso correto:** `/excluircategoria Pet`", parse_mode="Markdown")
        return
    await iniciar_exclusao_categoria(update, context, user_id, " ".join(context.args))


# ==========================================
# POUPANÇA NO TELEGRAM (guardar / sacar / consultar)
# ==========================================
def usuario_esta_vinculado(telegram_id: str) -> bool:
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT 1 FROM users WHERE telegram_id = %s;", (str(telegram_id),))
            return cursor.fetchone() is not None
    finally:
        conexao.close()


async def responder_movimento_poupanca(update: Update, user_id: str, tipo: str, valor_txt: str, descricao: str = ""):
    valor = parse_valor_br(valor_txt)
    if not valor:
        exemplo = "guardar 200" if tipo == 'deposito' else "sacar 100 da poupança"
        await update.message.reply_text(f"❌ Informe um valor válido. Exemplo: `{exemplo}`", parse_mode="Markdown")
        return
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Seu Telegram ainda não está vinculado ao **Fincash**. Use `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return

    try:
        r = registrar_movimento_poupanca(user_id, tipo, valor, (descricao or "").strip()[:200] or None)
    except Exception as e:
        print(f"❌ Erro ao registrar movimento de poupança: {e}")
        await update.message.reply_text("❌ Não consegui registrar agora. Tente novamente em instantes.")
        return

    if not r["ok"]:
        if r.get("motivo") == "sem_saldo":
            livre = r["saldo_livre"]
            if livre <= 0:
                situacao = ("Você está sem saldo disponível no momento"
                            + (", e seus gastos já superam a sua renda." if livre < 0 else "."))
            else:
                situacao = f"Seu saldo disponível é de apenas {fmt_brl(livre)}."
            await update.message.reply_text(
                f"❌ Não consegui guardar {fmt_brl(valor)} na poupança.\n\n"
                f"{situacao}\n"
                "Só dá para guardar dinheiro que você realmente tem sobrando. "
                "Registre sua renda (ex.: recebi 3000 de salário) ou guarde um valor menor.")
        else:
            await update.message.reply_text(
                f"❌ Saldo insuficiente na poupança. Você tem {fmt_brl(r['saldo'])} guardado.")
        return

    if tipo == 'deposito':
        linhas = [f"🐷 **Guardado!** +{fmt_brl(valor)} na poupança."]
    else:
        linhas = [f"💸 **Retirada registrada!** -{fmt_brl(valor)} da poupança."]
    linhas.append(f"💰 **Total guardado:** {fmt_brl(r['saldo'])}")
    linhas.append(f"💵 **Saldo disponível:** {fmt_brl(r['saldo_livre'])}")
    if r["meta"]:
        linhas.append(f"🎯 **Meta:** {min(r['saldo'] / r['meta'] * 100, 100):.0f}% de {fmt_brl(r['meta'])}")
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def responder_movimento_investimento(update: Update, user_id: str, tipo: str, valor_txt: str, descricao: str = ""):
    """Registra um aporte ('entrada') ou resgate ('saida') na tabela investments."""
    valor = parse_valor_br(valor_txt)
    if not valor:
        exemplo = "investi 500 em CDB" if tipo == 'entrada' else "resgatei 200 do CDB"
        await update.message.reply_text(f"❌ Informe um valor válido. Exemplo: `{exemplo}`", parse_mode="Markdown")
        return
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Seu Telegram ainda não está vinculado ao **Fincash**. Use `/login <email_ou_telefone> <senha>`.", parse_mode="Markdown")
        return

    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                INSERT INTO investments (telegram_id, tipo, valor, descricao)
                VALUES (%s, %s, %s, %s);
            """, (str(user_id).strip(), tipo, valor, (descricao or "").strip()[:200] or None))
            cursor.execute("""
                SELECT COALESCE(SUM(CASE WHEN tipo = 'entrada' THEN valor ELSE -valor END), 0)
                FROM investments WHERE TRIM(telegram_id) = %s;
            """, (str(user_id).strip(),))
            total = float(cursor.fetchone()[0])
        conexao.commit()
    except Exception as e:
        conexao.rollback()
        print(f"❌ Erro ao registrar investimento: {e}")
        await update.message.reply_text("❌ Não consegui registrar agora. Tente novamente em instantes.")
        return
    finally:
        conexao.close()

    if tipo == 'entrada':
        linhas = [f"📈 **Aporte registrado!** +{fmt_brl(valor)} em investimentos."]
    else:
        linhas = [f"💸 **Resgate registrado!** -{fmt_brl(valor)} dos investimentos."]
    linhas.append(f"💼 **Total investido:** {fmt_brl(total)}")
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def comando_poupanca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Seu Telegram ainda não está vinculado ao **Fincash**.", parse_mode="Markdown")
        return
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            saldo = saldo_poupanca(cursor, user_id)
            cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (user_id,))
            row = cursor.fetchone()
    finally:
        conexao.close()
    linhas = [f"💰 **Total guardado:** {fmt_brl(saldo)}"]
    if row:
        meta = float(row[0])
        linhas.append(f"🎯 **Meta:** {min(saldo / meta * 100, 100):.0f}% de {fmt_brl(meta)}")
    linhas.append("\nPara mexer: `guardar 200` ou `sacar 100 da poupança`.")
    await update.message.reply_text("\n".join(linhas), parse_mode="Markdown")


async def comando_guardar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ **Uso correto:** `/guardar 200`", parse_mode="Markdown")
        return
    await responder_movimento_poupanca(update, str(update.effective_user.id), 'deposito',
                                       context.args[0], " ".join(context.args[1:]))


async def comando_sacar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ **Uso correto:** `/sacar 100`", parse_mode="Markdown")
        return
    await responder_movimento_poupanca(update, str(update.effective_user.id), 'saque',
                                       context.args[0], " ".join(context.args[1:]))



# ==========================================
# SAUDAÇÃO E MENU PRINCIPAL NO TELEGRAM (/menu)
# ==========================================
# "oi" / "bom dia" / /start -> apresentação do Fincash + botões. /menu -> tutoriais e atalho para Investimentos.
# Os botões usam callback_data "menu:<acao>"; o botão de Investimentos chama "inv:menu" (tratado no investimentos.py).
# Aplicado depois de normalizar (sem acento/pontuação) e de colapsar letras repetidas ("oiii" -> "oi").
PADRAO_SAUDACAO = re.compile(
    r'^(?:oi|ola|eai|e ai|eae|opa|hey|hi|helo|salve|fala|bom dia|boa tarde|boa noite|tudo bem|comecar)'
    r'(?:\s+(?:tudo bem|td bem|fincash|bot|bom dia|boa tarde|boa noite))*$')
PEDIDOS_DE_MENU = {"menu", "ajuda", "help", "opcoes", "mais opcoes", "ver menu", "abrir menu"}

AVISO_VINCULAR_HTML = "⚠️ Vincule sua conta primeiro com /login &lt;email_ou_telefone&gt; &lt;senha&gt;."

TEXTO_MENU_PRINCIPAL = ("📋 <b>Menu do Fincash</b>\n\n"
                        "O que você quer ver? Toque em uma opção.\n"
                        "<i>Para voltar aqui depois, digite /menu.</i>")

TUTORIAIS_MENU = {
    "tutorial": (
        "📖 <b>Como usar o Fincash</b>\n\n"
        "É só conversar comigo, como em qualquer chat:\n\n"
        "1️⃣ <b>Registre gastos e ganhos</b> escrevendo normalmente: "
        "<i>gastei 50 no mercado</i> ou <i>recebi 2000 de salário</i>.\n\n"
        "2️⃣ <b>Guarde dinheiro</b>: <i>guardar 200</i>. Para sacar: <i>sacar 100 da poupança</i>.\n\n"
        "3️⃣ <b>Veja como você está</b>: <i>meu saldo</i> ou /resumo. "
        "Pergunte também sua <i>previsão para o mês que vem</i> e ative os /alertas de gastos.\n\n"
        "4️⃣ <b>Investimentos</b>: <i>investi 500 em CDB</i>, ou abra o menu de investimentos "
        "para ver notícias, riscos e dicas.\n\n"
        "5️⃣ <b>Tire dúvidas de finanças</b>: pergunte qualquer coisa sobre dinheiro.\n\n"
        "6️⃣ <b>Acompanhe tudo no site</b>: gráficos, orçamentos e relatórios."),
    "gastos": (
        "💸 <b>Tutorial: gastos e receitas</b>\n\n"
        "Escreva do seu jeito que eu entendo o valor e a categoria:\n"
        "• <i>gastei 35 no almoço</i>\n"
        "• <i>paguei 120 de internet</i>\n"
        "• <i>recebi 2500 de salário</i>\n\n"
        "Cada lançamento ganha um <b>ID</b> (ex.: #05). Para apagar, digite <i>apagar 5</i> "
        "ou /deletar 5. Eu peço confirmação antes, porque não dá para desfazer.\n\n"
        "Se a categoria ainda não existir, eu pergunto se você quer criá-la."),
    "poupanca": (
        "🐷 <b>Tutorial: poupança</b>\n\n"
        "• <b>Guardar:</b> <i>guardar 200</i> ou /guardar 200\n"
        "• <b>Sacar:</b> <i>sacar 100 da poupança</i> ou /sacar 100\n"
        "• <b>Ver o total guardado:</b> <i>minha poupança</i> ou /poupanca\n"
        "• <b>Meta:</b> <i>/meta 5000</i> para definir, /meta para ver e <i>/meta remover</i> para apagar\n\n"
        "Guardar dinheiro não conta como gasto. Você também pode definir uma <b>meta de poupança</b> "
        "na página Orçamentos do site e acompanhar o progresso por aqui e no painel."),
    "categorias": (
        "🏷️ <b>Tutorial: categorias</b>\n\n"
        "• <b>Ver as suas:</b> /categorias ou <i>minhas categorias</i>\n"
        "• <b>Criar:</b> <i>criar categoria Pets</i>\n"
        "• <b>Criar de renda:</b> <i>criar categoria de renda Aluguel</i>\n"
        "• <b>Excluir:</b> <i>excluir categoria Pets</i> (eu peço confirmação)\n\n"
        "Assim seus gastos aparecem organizados nos gráficos e relatórios."),
    "resumo": (
        "📈 <b>Tutorial: resumo e saldo</b>\n\n"
        "Digite /resumo, <i>meu saldo</i> ou <i>quanto gastei</i>.\n\n"
        "Eu mostro o total recebido, o total gasto, o saldo atual e quanto foi para cada categoria."),
    "alertas": (
        "🔔 <b>Tutorial: alertas de gastos</b>\n\n"
        "Eu te aviso quando seus gastos do mês chegarem perto da sua <b>renda do mês</b>, "
        "que funciona como o seu teto de gastos.\n\n"
        "• <b>Perto do teto:</b> aviso ao usar 80% da renda (você pode mudar esse percentual)\n"
        "• <b>Estourou:</b> aviso ao passar de 100% da renda\n\n"
        "Cada aviso chega <b>uma vez por mês</b>, pelo Telegram e por e-mail, "
        "logo depois de você registrar uma despesa.\n\n"
        "<b>Comandos:</b>\n"
        "• /alertas para ver como está\n"
        "• <i>/alertas off</i> para desligar e <i>/alertas on</i> para ligar\n"
        "• <i>/alertas 90</i> para ser avisada ao usar 90% da renda (aceita de 50 a 99)\n\n"
        "💡 Os alertas só funcionam depois que você registrar a sua renda do mês, "
        "por exemplo: <i>recebi 3000 de salário</i>."),
    "previsao": (
        "🔮 <b>Tutorial: previsão de gastos</b>\n\n"
        "Eu estimo quanto você deve gastar no <b>próximo mês</b>, com base nos seus últimos "
        "3 meses fechados. Os meses mais recentes pesam mais, e contas fixas como moradia "
        "e internet repetem o último valor.\n\n"
        "<b>Para consultar, pergunte do seu jeito:</b>\n"
        "• <i>qual a minha previsão de gasto para o mês que vem?</i>\n"
        "• <i>quanto vou gastar no próximo mês?</i>\n"
        "• ou use o comando /previsao\n\n"
        "Eu mostro o valor previsto, a faixa provável, as maiores categorias e avisos de "
        "tendência de alta. Quanto mais meses de histórico, mais precisa fica a estimativa.\n\n"
        "O mesmo resumo aparece no card <b>Análise Preditiva</b> do painel no site."),
    "site": (
        "🌐 <b>Tutorial: painel no site</b>\n\n"
        "Entre no site do Fincash com o mesmo e-mail ou telefone do seu cadastro. Lá você encontra:\n"
        "• <b>Visão Geral:</b> gráficos e saldo do mês\n"
        "• <b>Transações:</b> todos os seus lançamentos\n"
        "• <b>Orçamentos:</b> limites por categoria e meta de poupança\n"
        "• <b>Investimentos:</b> notícias, indicadores e análises\n"
        "• <b>Relatórios:</b> comparativos e exportação\n\n"
        "O que você registra aqui no Telegram aparece lá automaticamente."),
}


def _texto_limpo_menu(texto) -> str:
    return re.sub(r'[^\w\s]', '', normalizar(texto)).strip()


def eh_saudacao(texto) -> bool:
    bruto = (texto or '').strip().lower()
    if bruto == '/start' or bruto.startswith('/start '):          # /start (inclusive com link de convite)
        return True
    colapsado = re.sub(r'(.)\1+', r'\1', _texto_limpo_menu(texto))
    return bool(PADRAO_SAUDACAO.match(colapsado))


def eh_pedido_de_menu(texto) -> bool:
    return _texto_limpo_menu(texto) in PEDIDOS_DE_MENU


def _em_conversa_com_ia(context) -> bool:
    """No modo 'Conversar com a IA' (investimentos.py) um 'oi' é uma pergunta, não uma saudação."""
    chat = context.user_data.get('inv_chat')
    return bool(chat) and chat.get('ate', 0) > time.time()


def _primeiro_nome_html(telegram_id) -> str:
    try:
        conexao = obter_conexao()
        try:
            with conexao.cursor() as cursor:
                cursor.execute("SELECT nome FROM users WHERE telegram_id = %s;", (str(telegram_id),))
                row = cursor.fetchone()
        finally:
            conexao.close()
        return escapar_html(row[0].split()[0]) if row and row[0] and row[0].split() else ""
    except Exception as e:
        print(f"⚠️ Menu: não consegui ler o nome: {e}")
        return ""


def _texto_saudacao(nome) -> str:
    ola = f"Olá, <b>{nome}</b>! 👋" if nome else "Olá! 👋"
    return (f"{ola}\n"
            "Eu sou o <b>Fincash</b>, seu assistente financeiro pessoal. 💙\n\n"
            "Posso te ajudar a registrar gastos e receitas, guardar dinheiro na poupança, "
            "acompanhar seus investimentos e tirar dúvidas sobre finanças.\n\n"
            "Para ver mais opções, digite /menu ou toque no botão abaixo 👇")


def _teclado_saudacao():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Abrir o menu", callback_data="menu:principal")],
        [InlineKeyboardButton("📖 Como usar", callback_data="menu:tutorial"),
         InlineKeyboardButton("📊 Investimentos", callback_data="inv:menu")],
    ])


def _teclado_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Como usar o Fincash", callback_data="menu:tutorial")],
        [InlineKeyboardButton("💸 Gastos e receitas", callback_data="menu:gastos"),
         InlineKeyboardButton("🐷 Poupança", callback_data="menu:poupanca")],
        [InlineKeyboardButton("📊 Investimentos", callback_data="inv:menu"),   # abre o menu que já existe
         InlineKeyboardButton("🏷️ Categorias", callback_data="menu:categorias")],
        [InlineKeyboardButton("📈 Resumo e saldo", callback_data="menu:resumo"),
         InlineKeyboardButton("🌐 Painel no site", callback_data="menu:site")],
        [InlineKeyboardButton("🔔 Alertas", callback_data="menu:alertas"),
         InlineKeyboardButton("🔮 Previsão", callback_data="menu:previsao")],
    ])


def _teclado_voltar_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Voltar ao menu", callback_data="menu:principal")]])


def _sair_de_outros_modos(context):
    """Abrir o menu principal encerra a conversa com a IA e o menu digitável de investimentos."""
    context.user_data.pop('inv_chat', None)
    context.user_data.pop('inv_menu', None)


async def enviar_saudacao(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str):
    await update.message.reply_text(_texto_saudacao(_primeiro_nome_html(user_id)), parse_mode="HTML",
                                    reply_markup=_teclado_saudacao())


async def enviar_menu_principal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _sair_de_outros_modos(context)
    await update.message.reply_text(TEXTO_MENU_PRINCIPAL, parse_mode="HTML", reply_markup=_teclado_menu_principal())


async def comando_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not usuario_esta_vinculado(str(update.effective_user.id)):
        await update.message.reply_text(AVISO_VINCULAR_HTML, parse_mode="HTML")
        return
    await enviar_menu_principal(update, context)


async def tratar_callback_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not usuario_esta_vinculado(str(q.from_user.id)):
        await q.message.reply_text(AVISO_VINCULAR_HTML, parse_mode="HTML")
        return

    acao = (q.data or "")[5:]                       # tira o prefixo "menu:"
    if acao in TUTORIAIS_MENU:
        texto, teclado = TUTORIAIS_MENU[acao], _teclado_voltar_menu()
    else:                                           # "principal" (ou qualquer valor desconhecido)
        _sair_de_outros_modos(context)
        texto, teclado = TEXTO_MENU_PRINCIPAL, _teclado_menu_principal()

    try:                                            # troca o conteúdo da própria mensagem (sem poluir o chat)
        await q.edit_message_text(texto, parse_mode="HTML", reply_markup=teclado, disable_web_page_preview=True)
    except Exception:                               # "message is not modified" ou mensagem antiga demais
        await q.message.reply_text(texto, parse_mode="HTML", reply_markup=teclado, disable_web_page_preview=True)


async def interceptar_saudacao_e_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str, texto: str) -> bool:
    """Chamado pelo processar_mensagem (usuário já vinculado). Retorna True se tratou a mensagem."""
    if _em_conversa_com_ia(context):                # no modo conversa, quem manda é o investimentos.py
        return False
    if eh_saudacao(texto):
        await enviar_saudacao(update, context, user_id)
        return True
    if eh_pedido_de_menu(texto):
        await enviar_menu_principal(update, context)
        return True
    return False

# ==========================================
# META DE POUPANÇA PELO TELEGRAM
# ==========================================
PADRAO_DEFINIR_META = re.compile(
    r'^(?:definir|defino|criar|crio|colocar|coloco|alterar|altero|mudar|mudo|ajustar|ajusto|quero\s+(?:definir|criar|ter))'
    r'\s+(?:uma\s+|minha\s+|a\s+)?meta(?:\s+de\s+poupan[çc]a)?(?:\s+(?:de|para|em|como))?\s*(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?\s*$')
PADRAO_META_E = re.compile(
    r'^(?:minha\s+)?meta(?:\s+de\s+poupan[çc]a)?\s+(?:[eé]|ser[aá]|fica|vai\s+ser)\s*(?:de\s+)?(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?\s*$')
PADRAO_QUERO_JUNTAR = re.compile(
    r'^(?:quero|vou|pretendo)\s+(?:juntar|poupar|guardar)\s+(?:r\$\s*)?(\d[\d.,]*)(?:\s*reais)?\s*$')
PADRAO_REMOVER_META = re.compile(
    r'^(?:remover|remova|apagar|apague|excluir|exclua|deletar|tirar)\s+(?:a\s+|minha\s+)?meta(?:\s+de\s+poupan[çc]a)?\s*$')
PADRAO_VER_META = re.compile(r'^(?:minha\s+meta(?:\s+de\s+poupan[çc]a)?|ver\s+(?:minha\s+)?meta|qual\s+(?:[eé]\s+)?a?\s*minha\s+meta)\s*\??$')


def obter_meta_poupanca(telegram_id):
    """Devolve (guardado, meta) — meta é None se não houver."""
    tid = str(telegram_id).strip()
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            guardado = saldo_poupanca(cursor, tid)
            cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (tid,))
            row = cursor.fetchone()
        return guardado, (float(row[0]) if row else None)
    finally:
        conexao.close()


def gravar_meta_poupanca(telegram_id, valor):
    """valor=None remove a meta."""
    tid = str(telegram_id).strip()
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            if valor is None:
                cursor.execute("DELETE FROM savings_goals WHERE telegram_id = %s;", (tid,))
            else:
                cursor.execute("""
                    INSERT INTO savings_goals (telegram_id, valor_meta) VALUES (%s, %s)
                    ON CONFLICT (telegram_id) DO UPDATE
                    SET valor_meta = EXCLUDED.valor_meta, atualizado_em = CURRENT_TIMESTAMP;
                """, (tid, valor))
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def texto_progresso_meta(guardado, meta):
    if guardado >= meta:
        return f"🏆 Você já guardou {fmt_brl(guardado)}: a meta está atingida!"
    pct = guardado / meta * 100
    return (f"💰 Guardado até agora: {fmt_brl(guardado)} ({pct:.0f}% da meta)\n"
            f"Faltam {fmt_brl(meta - guardado)}.")


async def responder_ver_meta(update: Update, user_id: str):
    guardado, meta = await asyncio.to_thread(obter_meta_poupanca, user_id)
    if not meta:
        await update.message.reply_text(
            "🎯 Você ainda não tem uma meta de poupança.\n\n"
            "Defina uma enviando, por exemplo: /meta 5000\n"
            "Dica: uma boa referência é a reserva de emergência, de 3 meses de gastos.")
        return
    await update.message.reply_text(
        f"🎯 Sua meta de poupança: {fmt_brl(meta)}\n\n{texto_progresso_meta(guardado, meta)}\n\n"
        "Para alterar: /meta 8000\nPara remover: /meta remover")


async def responder_definir_meta(update: Update, user_id: str, valor_txt: str):
    valor = parse_valor_br(valor_txt)
    if not valor:
        await update.message.reply_text("❌ Informe um valor válido. Exemplo: /meta 5000")
        return
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com /login <email_ou_telefone> <senha>.")
        return
    try:
        _, meta_antiga = await asyncio.to_thread(obter_meta_poupanca, user_id)
        await asyncio.to_thread(gravar_meta_poupanca, user_id, valor)
        guardado, _ = await asyncio.to_thread(obter_meta_poupanca, user_id)
    except Exception as e:
        print(f"❌ Erro ao salvar meta: {e}")
        await update.message.reply_text("❌ Não consegui salvar a meta agora. Tente novamente em instantes.")
        return
    titulo = "🎯 Meta atualizada!" if meta_antiga else "🎯 Meta definida!"
    await update.message.reply_text(
        f"{titulo} {fmt_brl(valor)}\n\n{texto_progresso_meta(guardado, valor)}\n\n"
        "Para guardar dinheiro: guardar 200\nPara ver sua meta: /meta")


async def pedir_confirmacao_remover_meta(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: str):
    _, meta = await asyncio.to_thread(obter_meta_poupanca, user_id)
    if not meta:
        await update.message.reply_text("Você não tem uma meta de poupança definida.")
        return
    _iniciar_conversa(context, 'remover_meta')
    await update.message.reply_text(
        f"⚠️ Tem certeza que deseja remover a sua meta de {fmt_brl(meta)}?\n\n"
        "Será apagada só a meta. O dinheiro guardado não muda.\n\n"
        "Responda sim para remover ou não para cancelar.")


async def comando_meta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com /login <email_ou_telefone> <senha>.")
        return
    if not context.args:
        await responder_ver_meta(update, user_id)
        return
    arg = " ".join(context.args).strip().lower()
    if arg in ("remover", "apagar", "excluir", "deletar", "tirar", "0"):
        await pedir_confirmacao_remover_meta(update, context, user_id)
        return
    await responder_definir_meta(update, user_id, arg)

# ==========================================
# ALERTAS DE POUPANÇA (incentivo a guardar, alinhado à meta)
# ==========================================
POUPANCA_PAUSA_SEG = 7 * 24 * 3600     # no máximo 1 aviso por semana
POUPANCA_HORAS = (9, 20)               # só das 9h às 20h
POUPANCA_INTERVALO_JOB = 1800          # o robô confere a cada 30 min


def _dentro_do_horario_poupanca() -> bool:
    try:
        from zoneinfo import ZoneInfo
        hora = datetime.now(ZoneInfo("America/Sao_Paulo")).hour
    except Exception:
        hora = datetime.now().hour
    return POUPANCA_HORAS[0] <= hora < POUPANCA_HORAS[1]


def salvar_alerta_poupanca(telegram_id, ativo):
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            # ao ligar, zera o último envio: o primeiro aviso vem na hora (Telegram) ou no próximo ciclo (site)
            cursor.execute("""
                INSERT INTO poupanca_alertas (telegram_id, ativo, ultimo_envio)
                VALUES (%s, %s, NULL)
                ON CONFLICT (telegram_id) DO UPDATE
                SET ativo = EXCLUDED.ativo,
                    ultimo_envio = CASE WHEN EXCLUDED.ativo THEN NULL ELSE poupanca_alertas.ultimo_envio END;
            """, (str(telegram_id).strip(), bool(ativo)))
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise
    finally:
        conexao.close()


def alerta_poupanca_ativo(telegram_id) -> bool:
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT ativo FROM poupanca_alertas WHERE telegram_id = %s;", (str(telegram_id).strip(),))
            row = cursor.fetchone()
        return bool(row and row[0])
    finally:
        conexao.close()


def _marcar_envio_poupanca(telegram_id):
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("UPDATE poupanca_alertas SET ultimo_envio = CURRENT_TIMESTAMP WHERE telegram_id = %s;",
                           (str(telegram_id).strip(),))
        conexao.commit()
    finally:
        conexao.close()


def situacao_poupanca(telegram_id):
    """Números que personalizam o aviso: guardado, meta, saldo livre, o que guardou na semana e uma meta sugerida."""
    tid = str(telegram_id).strip()
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            guardado = saldo_poupanca(cursor, tid)
            livre = saldo_disponivel(cursor, tid)
            cursor.execute("SELECT valor_meta FROM savings_goals WHERE telegram_id = %s;", (tid,))
            r = cursor.fetchone()
            meta = float(r[0]) if r else None
            cursor.execute("""
                SELECT COALESCE(SUM(valor), 0) FROM savings_movements
                WHERE TRIM(telegram_id) = %s AND tipo = 'deposito' AND data >= %s;
            """, (tid, datetime.now() - timedelta(days=7)))
            guardou_semana = float(cursor.fetchone()[0])
            cursor.execute("""
                SELECT COALESCE(SUM(valor), 0) FROM transactions
                WHERE TRIM(telegram_id) = %s AND tipo = 'despesa' AND data >= %s;
            """, (tid, datetime.now() - timedelta(days=90)))
            gasto_90 = float(cursor.fetchone()[0])
    finally:
        conexao.close()
    # reserva de emergência = 3 meses de gastos (os últimos 90 dias), arredondada para centenas
    sugestao_meta = round(gasto_90 / 100) * 100 if gasto_90 >= 50 else None
    return {"guardado": guardado, "meta": meta, "livre": livre,
            "guardou_semana": guardou_semana, "sugestao_meta": sugestao_meta or None}


def montar_texto_poupanca(s) -> str:
    guardado, meta, livre = s["guardado"], s["meta"], s["livre"]
    linhas = []
    if s["guardou_semana"] > 0:
        linhas += [f"👏 Você guardou {fmt_brl(s['guardou_semana'])} nos últimos 7 dias. Continue assim!", ""]

    if not meta:
        linhas += ["🎯 Que tal definir uma meta de poupança?", "",
                   f"Você já tem {fmt_brl(guardado)} guardado. Quem tem uma meta clara guarda com muito mais constância."]
        if s["sugestao_meta"]:
            linhas.append(f"Uma boa referência é uma reserva de emergência de 3 meses de gastos. "
                          f"Pelo seu histórico, algo perto de {fmt_brl(s['sugestao_meta'])}.")
        linhas += ["", "Para definir agora, é só enviar: /meta 5000 (troque pelo valor que quiser)."]
    elif guardado >= meta:
        linhas += ["🏆 Meta de poupança atingida! Parabéns!", "",
                   f"Você guardou {fmt_brl(guardado)} e a meta era {fmt_brl(meta)}.",
                   "Que tal definir uma meta maior na página Orçamentos ou conhecer opções para fazer esse dinheiro render? "
                   "Envie /investir para ver o menu de investimentos."]
    else:
        pct = guardado / meta * 100
        falta = meta - guardado
        if pct < 25:
            fase = "Os primeiros passos são os mais importantes."
        elif pct < 50:
            fase = "Você já percorreu um bom pedaço do caminho."
        elif pct < 75:
            fase = "Mais da metade do caminho feita!"
        else:
            fase = "Reta final: falta pouco!"
        linhas += [f"🎯 Sua meta de poupança está em {pct:.0f}%", "",
                   f"{fase} Você tem {fmt_brl(guardado)} de {fmt_brl(meta)}. Faltam {fmt_brl(falta)}.", ""]
        if livre > 0:
            sug = min(falta, livre)
            sug = float(int(sug)) if sug >= 10 else round(sug, 2)
            cmd = f"{sug:.2f}".replace(".", ",")
            linhas += [f"💵 Seu saldo disponível é de {fmt_brl(livre)}. Guardar {fmt_brl(sug)} agora já aproxima você da meta.",
                       f"É só enviar: guardar {cmd}"]
        else:
            linhas.append("💡 Seu saldo disponível está zerado agora. Segurar os gastos desta semana ajuda "
                          "você a voltar a guardar.")
    linhas += ["", "Para pausar estes avisos: /alertapoupanca off"]
    return "\n".join(linhas)


async def _enviar_alerta_poupanca(bot, telegram_id):
    s = await asyncio.to_thread(situacao_poupanca, telegram_id)
    await bot.send_message(chat_id=telegram_id, text=montar_texto_poupanca(s))
    await asyncio.to_thread(_marcar_envio_poupanca, telegram_id)


def _usuarios_alerta_poupanca():
    conexao = obter_conexao()
    try:
        with conexao.cursor() as cursor:
            cursor.execute("""
                SELECT p.telegram_id, p.ultimo_envio
                FROM poupanca_alertas p JOIN users u ON u.telegram_id = p.telegram_id
                WHERE p.ativo = TRUE;
            """)
            return cursor.fetchall()
    finally:
        conexao.close()


async def _job_alertas_poupanca(context: ContextTypes.DEFAULT_TYPE):
    """Roda a cada 30 min: manda o lembrete a quem ligou, no máximo 1 vez por semana e só de dia."""
    if not _dentro_do_horario_poupanca():
        return
    try:
        usuarios = await asyncio.to_thread(_usuarios_alerta_poupanca)
    except Exception as e:
        print(f"❌ Alertas de poupança (lista): {e}")
        return
    agora = datetime.now()
    for tid, ultimo in usuarios:
        if ultimo and (agora - ultimo).total_seconds() < POUPANCA_PAUSA_SEG:
            continue
        try:
            await _enviar_alerta_poupanca(context.bot, tid)
        except Exception as e:
            if any(w in str(e).lower() for w in ("blocked", "forbidden", "chat not found")):
                await asyncio.to_thread(salvar_alerta_poupanca, tid, False)   # bloqueou o bot: para de tentar
            print(f"⚠️ Alerta de poupança ({tid}): {e}")
        await asyncio.sleep(0.1)


async def comando_alerta_poupanca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not usuario_esta_vinculado(user_id):
        await update.message.reply_text("⚠️ Vincule sua conta primeiro com /login <email_ou_telefone> <senha>.")
        return
    arg = context.args[0].strip().lower() if context.args else ""
    if arg in ("on", "ligar", "ativar"):
        salvar_alerta_poupanca(user_id, True)
        await update.message.reply_text("🔔 Alertas de poupança ligados! Vou te incentivar a guardar, "
                                        "no máximo uma vez por semana e nunca de madrugada.")
        try:
            await _enviar_alerta_poupanca(context.bot, user_id)
        except Exception as e:
            print(f"⚠️ Primeiro alerta de poupança: {e}")
        return
    if arg in ("off", "desligar", "desativar"):
        salvar_alerta_poupanca(user_id, False)
        await update.message.reply_text("🔕 Alertas de poupança desligados. Você pode ligar de novo quando quiser.")
        return
    ativo = alerta_poupanca_ativo(user_id)
    await update.message.reply_text(
        f"{'🔔' if ativo else '🔕'} Alertas de poupança: {'ligados' if ativo else 'desligados'}\n\n"
        "Lembretes semanais para você guardar dinheiro, alinhados à sua meta.\n"
        "• /alertapoupanca on para ligar\n• /alertapoupanca off para desligar")


# ---------- API do site ----------
@app_web.route('/api/alertas-poupanca', methods=['GET'])
def api_alerta_poupanca_get():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    if not tid:
        return jsonify({"ativo": False, "vinculado": False})
    return jsonify({"ativo": alerta_poupanca_ativo(tid), "vinculado": True})


@app_web.route('/api/alertas-poupanca', methods=['PUT'])
def api_alerta_poupanca_put():
    tid, erro = _exigir_usuario()
    if erro:
        return erro
    ativo = (request.get_json(silent=True) or {}).get('ativo')
    if not isinstance(ativo, bool):
        return jsonify({"erro": "Valor inválido"}), 400
    if not tid:
        return jsonify({"erro": "Vincule sua conta ao bot do Telegram para receber os alertas."}), 400
    salvar_alerta_poupanca(tid, ativo)
    return jsonify({"sucesso": True, "ativo": ativo})

# ==========================================
# MÓDULO DE INVESTIMENTOS
# ==========================================
investimentos.configurar(
    obter_conexao=obter_conexao,
    cliente_groq=cliente_groq,
    modelo="openai/gpt-oss-120b",   # <- era "model", o nome certo é "modelo"
    usuario_esta_vinculado=usuario_esta_vinculado
)
investimentos.registrar_web(app_web)   # rotas web podem ser registradas já aqui

#
# INICIALIZAÇÃO DA APLICAÇÃO (BOT + WEB) - NOVO
#

def registrar_comandos_telegram(app):
    """Registra os comandos e mensagens do bot FinCash."""

    # Investimentos
    investimentos.registrar_telegram(app)

    # Menu principal
    app.add_handler(CommandHandler("menu", comando_menu))
    app.add_handler(CommandHandler("ajuda", comando_menu))
    app.add_handler(CommandHandler("help", comando_menu))
    app.add_handler(
        CallbackQueryHandler(
            tratar_callback_menu,
            pattern=r"^menu:"
        )
    )

    # Usuários e alertas
    app.add_handler(CommandHandler("start", processar_mensagem))
    app.add_handler(CommandHandler("alertapoupanca", comando_alerta_poupanca))
    app.add_handler(CommandHandler("login", processar_mensagem))
    app.add_handler(CommandHandler("alertas", comando_alertas))

    # Finanças
    app.add_handler(CommandHandler("resumo", comando_resumo))
    app.add_handler(CommandHandler("deletar", comando_deletar))
    app.add_handler(CommandHandler("apagar", comando_deletar))
    app.add_handler(CommandHandler("excluir", comando_deletar))
    app.add_handler(CommandHandler("categorias", comando_categorias))
    app.add_handler(CommandHandler("novacategoria", comando_nova_categoria))
    app.add_handler(CommandHandler("excluircategoria", comando_excluir_categoria))
    app.add_handler(CommandHandler("guardar", comando_guardar))
    app.add_handler(CommandHandler("sacar", comando_sacar))
    app.add_handler(CommandHandler("poupanca", comando_poupanca))
    app.add_handler(CommandHandler("previsao", comando_previsao))
    app.add_handler(CommandHandler("meta", comando_meta))

    # Mensagens e contatos
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CONTACT) & ~filters.COMMAND,
            processar_mensagem
        )
    )

    # Alertas programados
    if app.job_queue:
        app.job_queue.run_repeating(
            _job_alertas_poupanca,
            interval=POUPANCA_INTERVALO_JOB,
            first=120,
            name="alertas_poupanca"
        )


# ==========================================
# WEBHOOK DO TELEGRAM
# ==========================================

telegram_webhook = None


def iniciar_telegram_no_render():
    global telegram_webhook

    if telegram_webhook is not None:
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado")

    aplicacao = ApplicationBuilder().token(token).build()

    registrar_comandos_telegram(aplicacao)

    webhook = TelegramWebhook(aplicacao)
    webhook.iniciar()

    telegram_webhook = webhook

    print("✅ Telegram inicializado no Render")

@app_web.route("/telegram/webhook", methods=["POST"])
def receber_webhook_telegram():
    global telegram_webhook

    # Verifica a chave de segurança do Telegram
    segredo = os.getenv("TELEGRAM_WEBHOOK_SECRET")

    if not segredo:
        return jsonify({"erro": "Webhook não configurado"}), 503

    segredo_recebido = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if not secrets.compare_digest(
        segredo_recebido, segredo
    ):
        return jsonify({"erro": "Não autorizado"}), 403

    if telegram_webhook is None:
        return jsonify({"erro": "Bot não inicializado"}), 503

    dados = request.get_json(silent=True)

    if not isinstance(dados, dict):
        return jsonify({"erro": "JSON inválido"}), 400

    try:
        telegram_webhook.processar(dados)
        return jsonify({"ok": True}), 200

    except Exception:
        app_web.logger.exception(
            "Erro ao processar mensagem do Telegram"
        )
        return jsonify({"erro": "Erro interno"}), 500


# ==========================================
# INICIALIZAÇÃO DA APLICAÇÃO (BOT + WEB)
# ==========================================

if __name__ == "__main__":
    investimentos.preparar_banco()   

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

        # Registra todos os comandos do FinCash
        registrar_comandos_telegram(app)

        print("🚀 Bot rodando com sucesso! Aguardando mensagens...")
        app.run_polling()
