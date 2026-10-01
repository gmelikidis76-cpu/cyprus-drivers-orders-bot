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

# На всякий случай принимаем и старую кнопку
OLD_ORDER_BUTTON = "🚕 Δώσε διαδρομή"

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
# КЛАВИАТУРА
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    if update.effective_chat.id == GROUP_ID:

        # Сначала устанавливаем клавиатуру
        bot_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text="🚕 Έτοιμο.",
            reply_markup=main_keyboard(),
        )

        # Удаляем /start
        try:
            await update.message.delete()
        except Exception:
            pass

        # Служебное сообщение удаляем чуть позже
        await asyncio.sleep(2)

        try:
            await bot_message.delete()
        except Exception:
            pass

        return

    await update.message.reply_text(
        "🚕 Cyprus Drivers Order\n\n"
        "Οι διαδρομές δίνονται μέσα από την ομάδα "
        "«Κούρσες δωρεάν»."
    )


# =========================================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ ГРУППЫ
# =========================================================

async def group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global next_order_id

    message = update.message

    if not message:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = (message.text or "").strip()


    # =====================================================
    # 1. НАЖАТИЕ "НОВЫЙ ЗАКАЗ"
    # =====================================================

    if text in (NEW_ORDER_BUTTON, OLD_ORDER_BUTTON):

        # Если было старое ожидание этого же водителя,
        # удаляем его
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
            or "Οδηγό"
        )

        # ВАЖНО:
        # сначала создаём сообщение ожидания
        waiting_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"⏳ Αναμονή πληροφοριών από {driver_name}..."
            ),
        )

        # И сразу записываем, что именно этот водитель
        # теперь должен написать информацию
        waiting_for_order[user.id] = (
            waiting_message.message_id
        )

        # И только ПОСЛЕ этого удаляем сообщение,
        # появившееся от нажатия кнопки
        try:
            await message.delete()
        except Exception:
            pass

        return


    # =====================================================
    # 2. ВОДИТЕЛЬ НЕ СОЗДАЁТ ЗАКАЗ
    # =====================================================

    if user.id not in waiting_for_order:

        # Обычная переписка запрещена
        try:
            await message.delete()
        except Exception:
            pass

        return


    # =====================================================
    # 3. ЭТО ИНФОРМАЦИЯ ДЛЯ ЗАКАЗА
    # =====================================================

    waiting_message_id = waiting_for_order.get(user.id)

    # Защита от пустого текста
    if not text:
        try:
            await message.delete()
        except Exception:
            pass
        return

    # Сначала создаём заказ в памяти
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
        "text": text,
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

    # СНАЧАЛА публикуем готовый заказ
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"{text}\n\n"
                f"👤 Από: {creator_name}"
            ),
            reply_markup=accept_keyboard,
        )

    except Exception:
        # Если готовый заказ не удалось опубликовать,
        # режим ожидания НЕ сбрасываем
        orders.pop(order_id, None)
        logger.exception("Could not publish order")
        return

    # Готовый заказ успешно появился.
    # Теперь убираем режим ожидания.
    waiting_for_order.pop(user.id, None)

    # Удаляем исходный текст водителя
    try:
        await message.delete()
    except Exception:
        pass

    # Удаляем промежуточное сообщение ожидания
    if waiting_message_id:
        try:
            await context.bot.delete_message(
                chat_id=GROUP_ID,
                message_id=waiting_message_id,
            )
        except Exception:
            pass


# =========================================================
# ВЗЯТЬ ЗАКАЗ
# =========================================================

async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if not query:
        return

    user = query.from_user

    try:
        order_id = int(query.data.split(":")[1])
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

        if user.id == order["creator_id"]:
            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )
            return

        if order["status"] != "open":
            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )
            return

        order["status"] = "accepted"
        order["accepted_by"] = user.id

    await query.answer(
        "Η διαδρομή είναι δική σου! ✅"
    )

    driver_name = (
        user.full_name
        or user.first_name
        or "Οδηγός"
    )

    username = (
        f"@{user.username}"
        if user.username
        else ""
    )

    # Убираем кнопку "взять"
    await query.edit_message_text(
        text=(
            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
            f"{order['text']}\n\n"
            f"👤 Από: {order['creator_name']}\n"
            f"🚕 Την πήρε: {driver_name}"
            + (f" ({username})" if username else "")
        )
    )

    # Личное уведомление автору, если Telegram разрешает
    try:
        await context.bot.send_message(
            chat_id=order["creator_id"],
            text=(
                "✅ Η διαδρομή σου δόθηκε.\n\n"
                f"🚕 Οδηγός: {driver_name}"
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
# ФОТО / СТИКЕРЫ / ГОЛОСОВЫЕ И Т.Д.
# =========================================================

async def delete_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    try:
        await update.message.delete()
    except Exception:
        pass


# =========================================================
# ОШИБКИ
# =========================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):

    logger.error(
        "Telegram error:",
        exc_info=context.error,
    )


# =========================================================
# ЗАПУСК
# =========================================================

def main():

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(
            take_order,
            pattern=r"^take:\d+$",
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

    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & ~filters.TEXT
            & ~filters.COMMAND,
            delete_non_text,
        )
    )

    app.add_error_handler(error_handler)

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
