"""Funções utilitárias compartilhadas por fincash.py e investimentos.py (sem dependências do Flask/banco)."""
import re
import unicodedata


def fmt_brl(valor, casas=2) -> str:
    """1234.5 -> 'R$ 1.234,50'."""
    texto = f"{float(valor):,.{casas}f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def normalizar(texto) -> str:
    """minúsculas, sem acento e sem espaços repetidos: 'Saúde ' e 'saude' viram a mesma coisa."""
    texto = unicodedata.normalize('NFKD', str(texto or '')).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'\s+', ' ', texto.lower()).strip()


def limpar_telefone(valor) -> str:
    """'(11) 99999-9999' -> '11999999999'."""
    return str(valor or '').replace(" ", "").replace("-", "").replace("(", "").replace(")", "").strip()


def limpar_resposta_ia(texto, limite=None) -> str:
    """Deixa a resposta da IA pronta para chat: sem símbolos de Markdown, em parágrafos curtos.

    - remove **negrito**, __, `crases` e títulos (#); troca "- item" / "* item" por "• item"
    - se a IA mandou tudo num bloco só, quebra em parágrafos de 2 frases
    - se passar do limite, corta no fim de uma frase (e não no meio de uma palavra)
    """
    t = str(texto or '').replace('\r\n', '\n').strip()
    t = re.sub(r'^\s{0,3}#{1,6}\s*', '', t, flags=re.M)             # títulos "## Texto"
    t = re.sub(r'^\s*[-*•]\s+', '• ', t, flags=re.M)                # listas
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', t, flags=re.S)              # **negrito**
    t = re.sub(r'__(.+?)__', r'\1', t, flags=re.S)                  # __negrito__
    t = re.sub(r'(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])', r'\1', t)   # *itálico*
    t = t.replace('**', '').replace('`', '')
    t = re.sub(r'[ \t]+\n', '\n', t)
    t = re.sub(r'\n{3,}', '\n\n', t).strip()

    if limite and len(t) > limite:
        corte = t[:limite]
        fim = max(corte.rfind('. '), corte.rfind('! '), corte.rfind('? '), corte.rfind('.\n'), corte.rfind('\n\n'))
        t = corte[:fim + 1].rstrip() if fim >= limite * 0.5 else corte.rstrip() + '…'

    if '\n' not in t and len(t) > 260:                              # bloco único: separa em parágrafos
        frases = re.split(r'(?<=[.!?])\s+', t)
        paragrafos, atual = [], []
        for f in frases:
            atual.append(f)
            if len(atual) >= 2 or len(' '.join(atual)) >= 220:
                paragrafos.append(' '.join(atual))
                atual = []
        if atual:
            paragrafos.append(' '.join(atual))
        t = '\n\n'.join(paragrafos)
    return t