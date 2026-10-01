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


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

# Группа «Κούρσες δωρεάν»
GROUP_ID = -1004449292276

# Постоянная кнопка внизу Telegram
NEW_ORDER_BUTTON = "🚕 Νέα διαδρομή"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# ДАННЫЕ
# ============================================================

# user_id -> message_id сообщения:
# "Αναμονή πληροφοριών από..."
waiting_for_order = {}

# order_id -> данные заказа
orders = {}

next_order_id = 1

# Защита от ситуации, когда два человека одновременно
# нажимают "взять" один заказ
accept_lock = asyncio.Lock()


# ============================================================
# ПОСТОЯННАЯ КЛАВИАТУРА
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Γράψε μήνυμα...",
    )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    # Если /start отправлен в нашей группе
    if update.effective_chat.id == GROUP_ID:

        await update.message.reply_text(
            "🚕 Το σύστημα διαδρομών είναι έτοιμο.",
            reply_markup=main_keyboard(),
        )

        return

    # Если /start отправлен лично боту
    await update.message.reply_text(
        "🚕 Cyprus Drivers Order\n\n"
        "Χρησιμοποίησε την ομάδα «Κούρσες δωρεάν» "
        "για τις διαδρομές."
    )


# ============================================================
# СООБЩЕНИЯ В ГРУППЕ
# ============================================================

async def group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global next_order_id

    message = update.message

    if not message:
        return

    # Работаем ТОЛЬКО в нужной группе
    if update.effective_chat.id != GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = message.text

    if not text:
        return


    # ========================================================
    # 1. ВОДИТЕЛЬ НАЖАЛ "Νέα διαδρομή"
    # ========================================================

    if text == NEW_ORDER_BUTTON:

        # Telegram сам отправляет текст Reply-кнопки в чат.
        # Сразу удаляем его.
        try:
            await message.delete()
        except Exception as e:
            logger.warning(
                "Could not delete NEW ORDER button message: %s",
                e,
            )

        # Если этот водитель уже начал другой заказ,
        # удаляем старое сообщение ожидания.
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

        # Имя берём автоматически из Telegram
        driver_name = user.first_name or user.full_name

        # Теперь появляется ТОЛЬКО это сообщение
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


    # ========================================================
    # 2. ЕСЛИ ВОДИТЕЛЬ НЕ СОЗДАЁТ ЗАКАЗ —
    #    ЭТО ОБЫЧНАЯ ПЕРЕПИСКА
    # ========================================================

    if user.id not in waiting_for_order:
        return


    # ========================================================
    # 3. ВОДИТЕЛЬ НАПИСАЛ ТЕКСТ ЗАКАЗА
    # ========================================================

    waiting_message_id = waiting_for_order.pop(
        user.id
    )

    order_text = text
    creator_name = user.full_name

    # Удаляем написанный водителем исходный текст.
    # Потом он появится в красивом сообщении заказа.
    try:
        await message.delete()
    except Exception as e:
        logger.warning(
            "Could not delete original order message: %s",
            e,
        )

    # Удаляем промежуточное:
    # "Αναμονή πληροφοριών από..."
    try:
        await context.bot.delete_message(
            chat_id=GROUP_ID,
            message_id=waiting_message_id,
        )
    except Exception as e:
        logger.warning(
            "Could not delete waiting message: %s",
            e,
        )

    # Создаём новый заказ
    order_id = next_order_id
    next_order_id += 1

    orders[order_id] = {
        "creator_id": user.id,
        "creator_name": creator_name,
        "text": order_text,
        "status": "open",
        "accepted_by": None,
    }

    # Синяя кнопка под готовым заказом
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

    # В группе остаётся только этот полноценный заказ
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            f"{order_text}\n\n"
            f"👤 Από: {creator_name}"
        ),
        reply_markup=accept_keyboard,
    )


# ============================================================
# ВЗЯТЬ ЗАКАЗ
# ============================================================

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
    except (ValueError, IndexError, AttributeError):
        await query.answer()
        return

    async with accept_lock:

        order = orders.get(order_id)

        # Заказ отсутствует
        if not order:
            await query.answer(
                "Η διαδρομή δεν είναι πλέον διαθέσιμη.",
                show_alert=True,
            )
            return

        # Автор не может взять свой заказ
        if user.id == order["creator_id"]:
            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )
            return

        # Уже кто-то взял
        if order["status"] != "open":
            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )
            return

        # Первый нажавший получает заказ
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

    accepted_driver = user.full_name

    # Меняем ТО ЖЕ сообщение.
    # Синяя кнопка исчезает.
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

    # Пытаемся уведомить автора лично.
    # Telegram разрешит это, если автор раньше
    # открывал личный чат с ботом.
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


# ============================================================
# ОШИБКИ
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram error:",
        exc_info=context.error,
    )


# ============================================================
# ЗАПУСК
# ============================================================

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

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
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

    app.add_error_handler(
        error_handler
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
