import asyncio
import os
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
        """Inicializa o Telegram em um loop assíncrono e registra o webhook."""

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

        self.registrar_webhook()

    def registrar_webhook(self):
        """Avisa o Telegram para enviar as mensagens para este servidor (não derruba o app se falhar)."""
        base = (
            os.getenv("WEBHOOK_BASE_URL")
            or os.getenv("APP_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or ""
        ).rstrip("/")
        segredo = os.getenv("TELEGRAM_WEBHOOK_SECRET")

        if not base or not segredo:
            print("⚠ Webhook NÃO registrado: defina TELEGRAM_WEBHOOK_SECRET e "
                  "WEBHOOK_BASE_URL (ou APP_BASE_URL / RENDER_EXTERNAL_URL).")
            return

        url = f"{base}/telegram/webhook"
        try:
            futuro = asyncio.run_coroutine_threadsafe(
                self.application.bot.set_webhook(
                    url=url,
                    secret_token=segredo,
                    allowed_updates=Update.ALL_TYPES,
                ),
                self.loop,
            )
            futuro.result(timeout=30)
            print(f"✅ Webhook registrado em {url}")
        except Exception as e:
            print(f"❌ Falha ao registrar webhook em {url}: {e}")

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