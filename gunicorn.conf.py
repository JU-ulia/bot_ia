
# Configuração do Gunicorn para o FinCash

workers = 1
threads = 4
timeout = 120
preload_app = False


def post_worker_init(worker):
    """Inicializa o Telegram após o worker carregar o Flask."""
    from fincash import iniciar_telegram_no_render
    iniciar_telegram_no_render()
