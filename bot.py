import os
import logging

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

# Группа «Κούρσες δωρεάν»
GROUP_ID = -1004449292276

NEW_ORDER_BUTTON = "🚕 Νέα διαδρομή"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# Водители, которые сейчас создают заказ.
# user_id -> message_id сообщения "ожидаем информацию"
waiting_for_order = {}

# Заказы
orders = {}
next_order_id = 1


# ==================================================
# ПОСТОЯННАЯ КНОПКА
# ==================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Γράψε μήνυμα...",
    )


# ==================================================
# /START
# ==================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    if update.effective_chat.id == GROUP_ID:

        await update.message.reply_text(
            "🚕 Το σύστημα διαδρομών είναι ενεργό.",
            reply_markup=main_keyboard(),
        )

        return

    await update.message.reply_text(
        "🚕 Cyprus Drivers Orders\n\n"
        "Οι διαδρομές δίνονται μέσα από την ομάδα "
        "«Κούρσες δωρεάν»."
    )


# ==================================================
# СООБЩЕНИЯ В ГРУППЕ
# ==================================================

async def group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global next_order_id

    if not update.message:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = update.message.text

    if not text:
        return

    # ==================================================
    # ВОДИТЕЛЬ НАЖАЛ "НОВЫЙ ЗАКАЗ"
    # ==================================================

    if text == NEW_ORDER_BUTTON:

        # Telegram сначала отправляет текст кнопки в группу.
        # Сразу его удаляем.
        try:
            await update.message.delete()
        except Exception:
            pass

        # Если водитель уже начал создавать заказ,
        # удаляем его старое сообщение ожидания.
        old_waiting_message_id = waiting_for_order.get(user.id)

        if old_waiting_message_id:
            try:
                await context.bot.delete_message(
                    chat_id=GROUP_ID,
                    message_id=old_waiting_message_id,
                )
            except Exception:
                pass

        # Бот показывает только короткое сообщение ожидания.
        waiting_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"⏳ Αναμονή πληροφοριών από {user.first_name}..."
            ),
        )

        # Запоминаем сообщение.
        waiting_for_order[user.id] = waiting_message.message_id

        return


    # ==================================================
    # ОБЫЧНОЕ СООБЩЕНИЕ
    # ==================================================

    if user.id not in waiting_for_order:
        return


    # ==================================================
    # ВОДИТЕЛЬ НАПИСАЛ ИНФОРМАЦИЮ О ЗАКАЗЕ
    # ==================================================

    waiting_message_id = waiting_for_order.pop(user.id)

    # Удаляем сообщение водителя с исходным текстом.
    try:
        await update.message.delete()
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

    # Создаём заказ
    order_id = next_order_id
    next_order_id += 1

    orders[order_id] = {
        "creator_id": user.id,
        "creator_name": user.full_name,
        "text": text,
        "accepted": False,
        "accepted_by": None,
    }

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Πάρε τη διαδρομή",
                    callback_data=f"accept_{order_id}",
                )
            ]
        ]
    )

    # Теперь появляется только полноценный заказ.
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            f"{text}\n\n"
            f"👤 {user.full_name}"
        ),
        reply_markup=keyboard,
    )


# ==================================================
# ПРИНЯТЬ ЗАКАЗ
# ==================================================

async def accept_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    user = query.from_user

    try:
        order_id = int(
            query.data.split("_")[1]
        )
    except (ValueError, IndexError):
        await query.answer()
        return

    order = orders.get(order_id)

    # Заказ не найден
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

    # Заказ уже забрали
    if order["accepted"]:

        await query.answer(
            "Η διαδρομή έχει ήδη δοθεί.",
            show_alert=True,
        )

        return

    # Первый водитель забирает заказ
    order["accepted"] = True
    order["accepted_by"] = user.id

    await query.answer(
        "Η διαδρομή είναι δική σου! ✅"
    )

    username = (
        f"@{user.username}"
        if user.username
        else ""
    )

    # Меняем готовый заказ.
    # Кнопка исчезает.
    await query.edit_message_text(
        text=(
            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
            f"{order['text']}\n\n"
            f"🚕 Την πήρε: {user.full_name}"
            + (
                f" ({username})"
                if username
                else ""
            )
        )
    )

    # Пробуем сообщить автору заказа лично.
    try:

        await context.bot.send_message(
            chat_id=order["creator_id"],
            text=(
                "✅ Η διαδρομή σου δόθηκε.\n\n"
                f"🚕 Οδηγός: {user.full_name}"
                + (
                    f"\nTelegram: {username}"
                    if username
                    else ""
                )
            ),
        )

    except Exception:
        pass


# ==================================================
# ОШИБКИ
# ==================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Exception while handling an update:",
        exc_info=context.error,
    )


# ==================================================
# ЗАПУСК
# ==================================================

def main():

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            accept_order,
            pattern=r"^accept_\d+$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            group_message,
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
