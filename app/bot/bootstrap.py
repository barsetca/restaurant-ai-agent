import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text("Restaurant AI Agent is running.")


def build_telegram_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    return application


async def start_polling(application: Application) -> None:
    await application.initialize()
    await application.start()
    if application.updater is None:
        raise RuntimeError("Telegram updater is unavailable")
    await application.updater.start_polling()
    logger.info("Telegram long polling started")


async def stop_polling(application: Application) -> None:
    if application.updater and application.updater.running:
        await application.updater.stop()
    if application.running:
        await application.stop()
    await application.shutdown()
    logger.info("Telegram long polling stopped")
