# ==========================================
# IMPORTS
# ==========================================

import telebot
from groq import Groq
from dotenv import load_dotenv

import os
import sqlite3
import json
import re

from datetime import (
    datetime,
    timedelta
)

# ==========================================
# LOAD ENV
# ==========================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN"
)

GROQ_API_KEY = os.getenv(
    "GROQ_API_KEY"
)

# ==========================================
# TELEGRAM + GROQ
# ==========================================

bot = telebot.TeleBot(
    TELEGRAM_TOKEN
)

client = Groq(
    api_key=GROQ_API_KEY
)

# ==========================================
# DATABASE
# ==========================================

conn = sqlite3.connect(
    "fincash.db",
    check_same_thread=False
)

cursor = conn.cursor()

# ==========================================
# TABLES
# ==========================================

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id TEXT PRIMARY KEY,
    nome TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT,
    tipo TEXT,
    valor REAL,
    categoria TEXT,
    descricao TEXT,
    data TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT,
    nome TEXT
)
""")

conn.commit()

# ==========================================
# ESTADO TEMPORÁRIO
# ==========================================

usuarios_editando = {}

# ==========================================
# CATEGORIAS PADRÃO
# ==========================================

CATEGORIAS_PADRAO = [
    "alimentacao",
    "transporte",
    "lazer",
    "mercado",
    "salario",
    "saude",
    "contas",
    "moradia",
    "outros"
]

# ==========================================
# VALIDAR DATA
# ==========================================

def validar_data(data_texto):

    try:

        datetime.strptime(
            data_texto,
            "%d/%m/%Y"
        )

        return True

    except:

        return False

# ==========================================
# DATA SOLTA
# ==========================================

def parece_data_solta(texto):

    regex = r"^\d{1,2}/\d{1,2}$"

    return re.match(
        regex,
        texto.strip()
    )

# ==========================================
# SEGURANÇA IA
# ==========================================

def verificar_seguranca(texto):

    resposta = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
Você é o sistema de segurança do Fincash.

Classifique SOMENTE como:

- seguro
- ofensivo
- ilegal
- dados_confidenciais
- ameaca

Retorne SOMENTE UMA palavra.
"""
            },
            {
                "role": "user",
                "content": texto
            }
        ]
    )

    classificacao = (
        resposta
        .choices[0]
        .message
        .content
        .strip()
        .lower()
    )

    if classificacao == "ofensivo":

        return """
⚠️ Detectei uma mensagem ofensiva.

O Fincash segue políticas
de respeito e uso responsável.

📩 SAC:
jdfj@gmail.com
"""

    if classificacao == "ilegal":

        return """
⚠️ Solicitação bloqueada.

O Fincash não auxilia
atividades ilegais,
fraudes ou invasões.

📩 SAC:
jdfj@gmail.com
"""

    if classificacao == "dados_confidenciais":

        return """
🔒 Não posso ajudar com
dados confidenciais.

O Fincash segue a LGPD
(Lei Geral de Proteção de Dados).

📩 SAC:
jdfj@gmail.com
"""

    if classificacao == "ameaca":

        return """
⚠️ Detectei conteúdo inadequado.

📩 SAC:
jdfj@gmail.com
"""

    return None

# ==========================================
# CLASSIFICAR INTENÇÃO
# ==========================================

def classificar_intencao(texto):

    resposta = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
Você classifica mensagens.

Categorias:

- movimentacao
- consulta
- conversa

Retorne SOMENTE UMA palavra.

movimentacao:
compras,
gastos,
salário,
receitas,
despesas.

consulta:
saldo,
gastos,
categorias,
movimentações.

conversa:
mensagens comuns.
"""
            },
            {
                "role": "user",
                "content": texto
            }
        ]
    )

    return (
        resposta
        .choices[0]
        .message
        .content
        .strip()
        .lower()
    )

# ==========================================
# EXTRAIR MOVIMENTAÇÃO
# ==========================================

def extrair_movimentacao(texto):

    agora = datetime.now()

    hoje = agora.strftime(
        "%d/%m/%Y"
    )

    ontem = (
        agora - timedelta(days=1)
    ).strftime("%d/%m/%Y")

    resposta = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": f"""
Você extrai movimentações financeiras.

Retorne SOMENTE JSON válido.

Formato:

{{
    "tipo": "",
    "valor": 0.00,
    "categoria": "",
    "descricao": "",
    "data": ""
}}

Regras:

- tipo:
receita ou despesa

- valor:
número decimal

- categoria:
minúsculo

- hoje:
{hoje}

- ontem:
{ontem}

- se a data não existir:

{{
   "erro": "data_invalida"
}}

Exemplos:

"mercado 50 hoje"
"uber 20 ontem"
"salário 3500"
"""
            },
            {
                "role": "user",
                "content": texto
            }
        ]
    )

    return (
        resposta
        .choices[0]
        .message
        .content
        .strip()
    )

# ==========================================
# CONVERSA
# ==========================================

def responder_conversa(texto):

    hoje = datetime.now().strftime(
        "%d/%m/%Y"
    )

    resposta = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        temperature=0.5,
        messages=[
            {
                "role": "system",
                "content": f"""
Você é o Fincash.

Hoje é:
{hoje}

Você é um assistente financeiro.

Regras:

- nunca invente saldo
- nunca invente datas
- nunca invente movimentações
- seja breve
- responda naturalmente
"""
            },
            {
                "role": "user",
                "content": texto
            }
        ]
    )

    return (
        resposta
        .choices[0]
        .message
        .content
        .strip()
    )

# ==========================================
# SALDO
# ==========================================

def obter_saldo(telegram_id):

    cursor.execute("""
    SELECT SUM(valor)
    FROM transactions
    WHERE telegram_id = ?
    AND tipo = 'receita'
    """, (telegram_id,))

    receitas = (
        cursor.fetchone()[0]
        or 0
    )

    cursor.execute("""
    SELECT SUM(valor)
    FROM transactions
    WHERE telegram_id = ?
    AND tipo = 'despesa'
    """, (telegram_id,))

    despesas = (
        cursor.fetchone()[0]
        or 0
    )

    saldo = receitas - despesas

    return (
        receitas,
        despesas,
        saldo
    )

# ==========================================
# LISTAR MOVIMENTAÇÕES
# ==========================================

def listar_movimentacoes(
    telegram_id
):

    cursor.execute("""
    SELECT
        id,
        categoria,
        valor,
        data
    FROM transactions
    WHERE telegram_id = ?
    ORDER BY id DESC
    LIMIT 10
    """, (telegram_id,))

    resultados = cursor.fetchall()

    if not resultados:

        return """
📭 Nenhuma movimentação encontrada.
"""

    resposta = (
        "📄 Últimas movimentações:\n\n"
    )

    for item in resultados:

        resposta += (
            f"#{item[0]} | "
            f"{item[1].title()} | "
            f"R$ {item[2]:.2f} | "
            f"{item[3]}\n"
        )

    resposta += """

✏️ Para editar,
envie:

editar
"""

    return resposta

# ==========================================
# CONSULTAS
# ==========================================

def responder_consulta(
    texto,
    telegram_id
):

    texto_lower = texto.lower()

    # listar

    if (
        "listar" in texto_lower
        or "movimentações" in texto_lower
    ):

        return listar_movimentacoes(
            telegram_id
        )

    # categorias

    if "categoria" in texto_lower:

        cursor.execute("""
        SELECT nome
        FROM categories
        WHERE telegram_id = ?
        """, (telegram_id,))

        categorias_usuario = [
            c[0]
            for c in cursor.fetchall()
        ]

        categorias = list(
            set(
                CATEGORIAS_PADRAO
                + categorias_usuario
            )
        )

        categorias.sort()

        resposta = (
            "📂 Categorias disponíveis:\n\n"
        )

        for categoria in categorias:

            resposta += (
                f"• {categoria.title()}\n"
            )

        return resposta

    # saldo

    if "saldo" in texto_lower:

        receitas, despesas, saldo = (
            obter_saldo(
                telegram_id
            )
        )

        return f"""
💰 Seu saldo atual:

💵 R$ {saldo:.2f}

📈 Entradas:
R$ {receitas:.2f}

📉 Saídas:
R$ {despesas:.2f}
"""

    # quanto gastou

    if (
        "quanto gastei"
        in texto_lower
        or "gastei"
        in texto_lower
    ):

        cursor.execute("""
        SELECT SUM(valor)
        FROM transactions
        WHERE telegram_id = ?
        AND tipo = 'despesa'
        """, (telegram_id,))

        total = (
            cursor.fetchone()[0]
            or 0
        )

        return f"""
📉 Você já gastou:

R$ {total:.2f}
"""

    return responder_conversa(
        texto
    )

# ==========================================
# START
# ==========================================

@bot.message_handler(
    commands=['start']
)
def start(message):

    telegram_id = str(
        message.from_user.id
    )

    nome = (
        message.from_user.first_name
    )

    cursor.execute("""
    SELECT *
    FROM users
    WHERE telegram_id = ?
    """, (telegram_id,))

    usuario = cursor.fetchone()

    if not usuario:

        cursor.execute("""
        INSERT INTO users (
            telegram_id,
            nome
        )
        VALUES (?, ?)
        """, (
            telegram_id,
            nome
        ))

        conn.commit()

        texto = f"""
Olá {nome}! 👋

Eu sou o Fincash 💰

Exemplos:

• mercado 150 hoje
• uber 30 ontem
• salário 3500
• quanto gastei?
• qual meu saldo?
• listar movimentações
"""

    else:

        texto = f"""
Bem-vindo de volta,
{nome}! 🚀
"""

    bot.reply_to(
        message,
        texto
    )

# ==========================================
# MENSAGENS
# ==========================================

@bot.message_handler(
    func=lambda message: True
)
def mensagens(message):

    try:

        texto_usuario = (
            message.text.strip()
        )

        telegram_id = str(
            message.from_user.id
        )

        # ==========================================
        # DATA SOLTA
        # ==========================================

        if parece_data_solta(
            texto_usuario
        ):

            bot.reply_to(
                message,
                """
⚠️ Você enviou apenas uma data.

Exemplo correto:

mercado 50 05/03/2025
"""
            )

            return

        # ==========================================
        # SEGURANÇA
        # ==========================================

        seguranca = (
            verificar_seguranca(
                texto_usuario
            )
        )

        if seguranca:

            bot.reply_to(
                message,
                seguranca
            )

            return

        # ==========================================
        # INICIAR EDIÇÃO
        # ==========================================

        if (
            texto_usuario.lower().strip()
            == "editar"
        ):

            usuarios_editando[
                telegram_id
            ] = {
                "etapa": "aguardando_id"
            }

            bot.reply_to(
                message,
                """
✏️ Qual o ID (#)
da movimentação?

Exemplo:

#5
"""
            )

            return

        # ==========================================
        # FLUXO EDIÇÃO
        # ==========================================

        if telegram_id in usuarios_editando:

            estado = usuarios_editando[
                telegram_id
            ]

            etapa = estado["etapa"]

            # ==========================================
            # AGUARDANDO ID
            # ==========================================

            if etapa == "aguardando_id":

                match = re.search(
                    r"\#?(\d+)",
                    texto_usuario
                )

                if not match:

                    bot.reply_to(
                        message,
                        """
⚠️ Envie um ID válido.

Exemplo:

#5

📩 SAC:
jdfj@gmail.com
"""
                    )

                    return

                transaction_id = (
                    match.group(1)
                )

                cursor.execute("""
                SELECT
                    id,
                    categoria,
                    data
                FROM transactions
                WHERE id = ?
                AND telegram_id = ?
                """, (
                    transaction_id,
                    telegram_id
                ))

                transacao = (
                    cursor.fetchone()
                )

                # ID inexistente

                if not transacao:

                    bot.reply_to(
                        message,
                        """
❌ Movimentação não encontrada.

📩 SAC:
jdfj@gmail.com
"""
                    )

                    return

                categoria = (
                    transacao[1]
                )

                data_movimentacao = (
                    transacao[2]
                )

                # bloquear salário

                if (
                    categoria.lower()
                    == "salario"
                ):

                    bot.reply_to(
                        message,
                        """
🔒 Não é possível editar
movimentações salariais.

📩 SAC:
jdfj@gmail.com
"""
                    )

                    return

                # bloquear antigas

                data_obj = datetime.strptime(
                    data_movimentacao,
                    "%d/%m/%Y"
                )

                dias_passados = (
                    datetime.now()
                    - data_obj
                ).days

                if dias_passados > 90:

                    bot.reply_to(
                        message,
                        """
🔒 Movimentações com mais
de 90 dias não podem
ser editadas.

📩 SAC:
jdfj@gmail.com
"""
                    )

                    return

                estado["id"] = (
                    transaction_id
                )

                estado["etapa"] = (
                    "aguardando_campo"
                )

                bot.reply_to(
                    message,
                    f"""
✏️ O que deseja editar
na movimentação #{transaction_id}?

• valor
• categoria
• data
"""
                )

                return

            # ==========================================
            # AGUARDANDO CAMPO
            # ==========================================

            if etapa == "aguardando_campo":

                campo = (
                    texto_usuario
                    .lower()
                    .strip()
                )

                campos_validos = [
                    "valor",
                    "categoria",
                    "data"
                ]

                if (
                    campo
                    not in campos_validos
                ):

                    bot.reply_to(
                        message,
                        """
⚠️ Campo inválido.

Use:

• valor
• categoria
• data
"""
                    )

                    return

                estado["campo"] = campo

                estado["etapa"] = (
                    "aguardando_valor"
                )

                if campo == "valor":

                    bot.reply_to(
                        message,
                        """
💰 Qual o novo valor?
"""
                    )

                    return

                if campo == "categoria":

                    bot.reply_to(
                        message,
                        """
📂 Qual a nova categoria?
"""
                    )

                    return

                if campo == "data":

                    bot.reply_to(
                        message,
                        """
📅 Qual a nova data?

Formato:

DD/MM/YYYY
"""
                    )

                    return

            # ==========================================
            # AGUARDANDO VALOR
            # ==========================================

            if etapa == "aguardando_valor":

                campo = estado["campo"]

                transaction_id = (
                    estado["id"]
                )

                novo_valor = (
                    texto_usuario.strip()
                )

                # validar valor

                if campo == "valor":

                    try:

                        novo_valor = float(
                            novo_valor
                        )

                    except:

                        bot.reply_to(
                            message,
                            """
⚠️ Valor inválido.
"""
                        )

                        return

                # validar data

                if campo == "data":

                    if not validar_data(
                        novo_valor
                    ):

                        bot.reply_to(
                            message,
                            """
⚠️ Data inválida.

Use:

DD/MM/YYYY
"""
                        )

                        return

                # salvar categoria

                if campo == "categoria":

                    novo_valor = (
                        novo_valor
                        .lower()
                        .strip()
                    )

                    if (
                        novo_valor
                        not in CATEGORIAS_PADRAO
                    ):

                        cursor.execute("""
                        SELECT *
                        FROM categories
                        WHERE telegram_id = ?
                        AND nome = ?
                        """, (
                            telegram_id,
                            novo_valor
                        ))

                        existe = (
                            cursor.fetchone()
                        )

                        if not existe:

                            cursor.execute("""
                            INSERT INTO categories (
                                telegram_id,
                                nome
                            )
                            VALUES (?, ?)
                            """, (
                                telegram_id,
                                novo_valor
                            ))

                # atualizar

                cursor.execute(f"""
                UPDATE transactions
                SET {campo} = ?
                WHERE id = ?
                """, (
                    novo_valor,
                    transaction_id
                ))

                conn.commit()

                del usuarios_editando[
                    telegram_id
                ]

                receitas, despesas, saldo = (
                    obter_saldo(
                        telegram_id
                    )
                )

                bot.reply_to(
                    message,
                    f"""
✅ Movimentação #{transaction_id}
atualizada com sucesso.

📊 Resumo atualizado:

📈 Entradas:
R$ {receitas:.2f}

📉 Saídas:
R$ {despesas:.2f}

💵 Saldo:
R$ {saldo:.2f}
"""
                )

                return

        # ==========================================
        # CLASSIFICAR
        # ==========================================

        intencao = (
            classificar_intencao(
                texto_usuario
            )
        )

        # ==========================================
        # MOVIMENTAÇÃO
        # ==========================================

        if intencao == "movimentacao":

            resposta_json = (
                extrair_movimentacao(
                    texto_usuario
                )
            )

            dados = json.loads(
                resposta_json
            )

            if (
                "erro" in dados
            ):

                bot.reply_to(
                    message,
                    """
⚠️ Data inválida.

Use datas reais.

📩 SAC:
jdfj@gmail.com
"""
                )

                return

            tipo = dados["tipo"]

            valor = float(
                dados["valor"]
            )

            categoria = (
                dados["categoria"]
            )

            descricao = (
                dados["descricao"]
            )

            data = dados["data"]

            if not validar_data(
                data
            ):

                bot.reply_to(
                    message,
                    """
⚠️ Data inválida.

📩 SAC:
jdfj@gmail.com
"""
                )

                return

            # salvar categoria

            if (
                categoria
                not in CATEGORIAS_PADRAO
            ):

                cursor.execute("""
                SELECT *
                FROM categories
                WHERE telegram_id = ?
                AND nome = ?
                """, (
                    telegram_id,
                    categoria
                ))

                existe = (
                    cursor.fetchone()
                )

                if not existe:

                    cursor.execute("""
                    INSERT INTO categories (
                        telegram_id,
                        nome
                    )
                    VALUES (?, ?)
                    """, (
                        telegram_id,
                        categoria
                    ))

            # salvar movimentação

            cursor.execute("""
            INSERT INTO transactions (
                telegram_id,
                tipo,
                valor,
                categoria,
                descricao,
                data
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """, (
                telegram_id,
                tipo,
                valor,
                categoria,
                descricao,
                data
            ))

            conn.commit()

            transaction_id = (
                cursor.lastrowid
            )

            receitas, despesas, saldo = (
                obter_saldo(
                    telegram_id
                )
            )

            bot.reply_to(
                message,
                f"""
✅ Movimentação registrada! #{transaction_id}

📂 Categoria:
{categoria.title()}

💰 Valor:
R$ {valor:.2f}

📅 Data:
{data}

📊 Resumo atual:

📈 Entradas:
R$ {receitas:.2f}

📉 Saídas:
R$ {despesas:.2f}

💵 Saldo:
R$ {saldo:.2f}
"""
            )

            return

        # ==========================================
        # CONSULTA
        # ==========================================

        if intencao == "consulta":

            resposta = (
                responder_consulta(
                    texto_usuario,
                    telegram_id
                )
            )

            bot.reply_to(
                message,
                resposta
            )

            return

        # ==========================================
        # CONVERSA
        # ==========================================

        resposta = (
            responder_conversa(
                texto_usuario
            )
        )

        bot.reply_to(
            message,
            resposta
        )

    except Exception as e:

        print(
            "ERRO:",
            e
        )

        bot.reply_to(
            message,
            """
⚠️ Não consegui processar
sua solicitação.

📩 SAC:
jdfj@gmail.com
"""
        )

# ==========================================
# START BOT
# ==========================================

print(
    "🚀 Fincash iniciado..."
)

bot.infinity_polling()