import os
import logging
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


TOKEN = os.getenv("BOT_TOKEN")

GROUP_ID = -1004449292276

NEW_ORDER_BUTTON = "🚕 Νέα διαδρομή"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# user_id -> message_id сообщения ожидания
waiting_for_order = {}

# Заказы
orders = {}

next_order_id = 1

accept_lock = asyncio.Lock()


# =========================================================
# ПОСТОЯННАЯ КНОПКА
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Νέα διαδρομή...",
    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    # В группе
    if update.effective_chat.id == GROUP_ID:

        # Отправляем служебное сообщение,
        # чтобы Telegram установил клавиатуру
        bot_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text="🚕 Έτοιμο.",
            reply_markup=main_keyboard(),
        )

        # Удаляем /start водителя
        try:
            await update.message.delete()
        except Exception:
            pass

        # Небольшая пауза нужна, чтобы Telegram
        # успел показать клавиатуру
        await asyncio.sleep(1)

        # Удаляем и сообщение "Έτοιμο."
        # Кнопка внизу при этом остаётся
        try:
            await bot_message.delete()
        except Exception:
            pass

        return

    # В личном чате
    await update.message.reply_text(
        "🚕 Cyprus Drivers Order\n\n"
        "Οι διαδρομές δίνονται μέσα από την ομάδα "
        "«Κούρσες δωρεάν»."
    )


# =========================================================
# СООБЩЕНИЯ В ГРУППЕ
# =========================================================

async def group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global next_order_id

    message = update.message

    if not message:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = message.text or ""


    # =====================================================
    # НАЖАЛИ "НОВЫЙ ЗАКАЗ"
    # =====================================================

    if text == NEW_ORDER_BUTTON:

        # Удаляем сообщение "🚕 Νέα διαδρομή",
        # которое Telegram отправляет в группу
        try:
            await message.delete()
        except Exception:
            pass

        # Если водитель уже начинал заказ,
        # удаляем его старое ожидание
        old_waiting_id = waiting_for_order.pop(
            user.id,
            None,
        )

        if old_waiting_id:
            try:
                await context.bot.delete_message(
                    chat_id=GROUP_ID,
                    message_id=old_waiting_id,
                )
            except Exception:
                pass

        driver_name = (
            user.first_name
            or user.full_name
            or "οδηγό"
        )

        # Промежуточное сообщение
        waiting_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"⏳ Αναμονή πληροφοριών από {driver_name}..."
            ),
        )

        waiting_for_order[user.id] = (
            waiting_message.message_id
        )

        return


    # =====================================================
    # ВОДИТЕЛЬ НЕ НАЖИМАЛ "НОВЫЙ ЗАКАЗ"
    # =====================================================

    if user.id not in waiting_for_order:

        # Любое обычное сообщение удаляем
        try:
            await message.delete()
        except Exception as e:
            logger.warning(
                "Δεν ήταν δυνατή η διαγραφή μηνύματος: %s",
                e,
            )

        return


    # =====================================================
    # ВОДИТЕЛЬ ПИШЕТ СВОЙ ЗАКАЗ
    # =====================================================

    waiting_message_id = waiting_for_order.pop(
        user.id
    )

    order_text = text.strip()

    # Если вместо текста отправили что-то другое
    if not order_text:

        try:
            await message.delete()
        except Exception:
            pass

        # Оставляем водителя в режиме ожидания
        waiting_for_order[user.id] = (
            waiting_message_id
        )

        return


    # Удаляем исходный текст водителя
    try:
        await message.delete()
    except Exception:
        pass


    # Удаляем:
    # "Αναμονή πληροφοριών από..."
    try:
        await context.bot.delete_message(
            chat_id=GROUP_ID,
            message_id=waiting_message_id,
        )
    except Exception:
        pass


    # =====================================================
    # СОЗДАЁМ ГОТОВЫЙ ЗАКАЗ
    # =====================================================

    order_id = next_order_id
    next_order_id += 1

    creator_name = (
        user.full_name
        or user.first_name
        or "Οδηγός"
    )

    orders[order_id] = {
        "creator_id": user.id,
        "creator_name": creator_name,
        "text": order_text,
        "status": "open",
        "accepted_by": None,
    }

    accept_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Πάρε τη διαδρομή",
                    callback_data=f"take:{order_id}",
                )
            ]
        ]
    )

    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            f"{order_text}\n\n"
            f"👤 Από: {creator_name}"
        ),
        reply_markup=accept_keyboard,
    )


# =========================================================
# ВЗЯТЬ ЗАКАЗ
# =========================================================

async def take_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    user = query.from_user

    try:
        order_id = int(
            query.data.split(":")[1]
        )
    except Exception:
        await query.answer()
        return


    async with accept_lock:

        order = orders.get(order_id)

        if not order:

            await query.answer(
                "Η διαδρομή δεν είναι πλέον διαθέσιμη.",
                show_alert=True,
            )

            return


        # Нельзя взять собственный заказ
        if user.id == order["creator_id"]:

            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )

            return


        # Заказ уже взяли
        if order["status"] != "open":

            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )

            return


        # Первый водитель получает заказ
        order["status"] = "accepted"
        order["accepted_by"] = user.id


    await query.answer(
        "Η διαδρομή είναι δική σου! ✅"
    )


    username = (
        f"@{user.username}"
        if user.username
        else ""
    )

    accepted_driver = (
        user.full_name
        or user.first_name
        or "Οδηγός"
    )


    # =====================================================
    # ОБНОВЛЯЕМ ЗАКАЗ
    # =====================================================

    await query.edit_message_text(
        text=(
            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
            f"{order['text']}\n\n"
            f"👤 Από: {order['creator_name']}\n"
            f"🚕 Την πήρε: {accepted_driver}"
            + (
                f" ({username})"
                if username
                else ""
            )
        )
    )


    # =====================================================
    # УВЕДОМЛЯЕМ АВТОРА ЛИЧНО
    # =====================================================

    try:

        await context.bot.send_message(
            chat_id=order["creator_id"],
            text=(
                "✅ Η διαδρομή σου δόθηκε.\n\n"
                f"🚕 Οδηγός: {accepted_driver}"
                + (
                    f"\nTelegram: {username}"
                    if username
                    else ""
                )
            ),
        )

    except Exception:
        pass


# =========================================================
# УДАЛЯЕМ ВСЁ ОСТАЛЬНОЕ ИЗ ГРУППЫ
# =========================================================

async def delete_other_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    # Сообщения самого бота не трогаем
    if update.effective_user and update.effective_user.is_bot:
        return

    try:
        await message.delete()
    except Exception:
        pass


# =========================================================
# ОШИБКИ
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram error:",
        exc_info=context.error,
    )


# =========================================================
# ЗАПУСК
# =========================================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set"
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # /start
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # Кнопка "взять заказ"
    app.add_handler(
        CallbackQueryHandler(
            take_order,
            pattern=r"^take:\d+$",
        )
    )

    # Текстовые сообщения
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            group_message,
        )
    )

    # Фото, видео, стикеры, голосовые и т.д.
    # тоже удаляем из рабочей группы
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & ~filters.TEXT
            & ~filters.COMMAND,
            delete_other_messages,
        )
    )

    app.add_error_handler(
        error_handler
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
