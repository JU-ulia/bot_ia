
import asyncio
import threading
from telegram import Update


class TelegramWebhook:
    def __init__(self, application):
        self.application = application
        self.loop = asyncio.new_event_loop()
        self.thread = None
        self.pronto = threading.Event()
        self.erro = None

    def iniciar(self):
        """Inicializa o Telegram em um loop assíncrono."""

        if self.thread is not None:
            if not self.pronto.wait(timeout=30):
                raise RuntimeError("Telegram não inicializou")
            if self.erro:
                raise RuntimeError("Falha ao iniciar Telegram") from self.erro
            return

        def executar():
            asyncio.set_event_loop(self.loop)

            async def inicializar():
                await self.application.initialize()
                await self.application.start()

            try:
                self.loop.run_until_complete(inicializar())
                self.pronto.set()
                self.loop.run_forever()
            except Exception as e:
                self.erro = e
                self.pronto.set()
                print(f"Erro ao iniciar webhook: {e}")

        self.thread = threading.Thread(
            target=executar,
            daemon=True
        )
        self.thread.start()

        if not self.pronto.wait(timeout=30):
            raise RuntimeError("Tempo esgotado ao iniciar Telegram")

        if self.erro:
            raise RuntimeError("Falha ao iniciar Telegram") from self.erro

    def processar(self, dados):
        """Processa uma atualização recebida do Telegram."""

        if not self.pronto.is_set() or self.erro:
            raise RuntimeError("Telegram não está pronto")

        update = Update.de_json(dados, self.application.bot)

        if update is None:
            raise ValueError("Mensagem inválida")

        futuro = asyncio.run_coroutine_threadsafe(
            self.application.process_update(update),
            self.loop
        )

        return futuro.result(timeout=60)
