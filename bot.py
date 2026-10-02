import os
import logging
import asyncio
import html

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


# =========================================================
# ПАМЯТЬ БОТА
# =========================================================

# user_id -> message_id сообщения ожидания
waiting_for_order = {}

# order_id -> данные заказа
orders = {}

next_order_id = 1

accept_lock = asyncio.Lock()


# =========================================================
# КНОПКА "НОВЫЙ ЗАКАЗ"
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Νέα διαδρομή...",
    )


# =========================================================
# ПРОВЕРКА АДМИНИСТРАТОРА
# =========================================================

async def is_admin(context, user_id):

    try:
        member = await context.bot.get_chat_member(
            chat_id=GROUP_ID,
            user_id=user_id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception as e:
        logger.warning(
            "Admin check failed: %s",
            e,
        )

        return False


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return


    # =====================================================
    # ЛИЧНЫЙ ЧАТ
    # =====================================================

    if update.effective_chat.id != GROUP_ID:

        await update.message.reply_text(
            "🚕 Cyprus Drivers Order\n\n"
            "Χρησιμοποίησε την ομάδα "
            "«Κούρσες δωρεάν» για τις διαδρομές."
        )

        return


    # =====================================================
    # /START В ГРУППЕ
    # =====================================================

    # Удаляем команду /start
    try:
        await update.message.delete()
    except Exception:
        pass


    # Показываем клавиатуру
    await context.bot.send_message(
        chat_id=GROUP_ID,
        text=(
            "🚕 Για νέα διαδρομή πάτησε "
            "το κουμπί παρακάτω."
        ),
        reply_markup=main_keyboard(),
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


    text = (message.text or "").strip()


    # =====================================================
    # 1. НАЖАЛ "🚕 Νέα διαδρομή"
    # =====================================================

    if text == NEW_ORDER_BUTTON:

        # Если у этого водителя осталось
        # старое ожидание — убираем.
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
            or "Οδηγός"
        )


        # Показываем ожидание информации
        waiting_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"⏳ Αναμονή πληροφοριών από "
                f"{driver_name}..."
            ),
            reply_markup=main_keyboard(),
        )


        # Запоминаем:
        # этому водителю разрешено одно сообщение
        waiting_for_order[user.id] = (
            waiting_message.message_id
        )


        # Удаляем сообщение,
        # которое появилось от нажатия Reply-кнопки
        try:
            await message.delete()
        except Exception:
            pass


        return


    # =====================================================
    # 2. ВОДИТЕЛЬ СЕЙЧАС СОЗДАЁТ ЗАКАЗ
    # =====================================================

    if user.id in waiting_for_order:

        if not text:
            return


        waiting_message_id = (
            waiting_for_order[user.id]
        )


        creator_name = (
            user.full_name
            or user.first_name
            or "Οδηγός"
        )


        order_id = next_order_id
        next_order_id += 1


        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Πάρε τη διαδρομή",
                        callback_data=f"take:{order_id}",
                    )
                ]
            ]
        )


        # Сначала публикуем готовый заказ
        try:

            order_message = await context.bot.send_message(
                chat_id=GROUP_ID,
                text=(
                    "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                    f"{text}\n\n"
                    f"👤 Από: {creator_name}"
                ),
                reply_markup=keyboard,
            )


        except Exception as e:

            logger.exception(
                "Order publish failed: %s",
                e,
            )

            return


        # Сохраняем заказ
        orders[order_id] = {
            "creator_id": user.id,
            "creator_name": creator_name,
            "text": text,
            "status": "open",
            "message_id": order_message.message_id,
        }


        # Разрешение на сообщение использовано
        waiting_for_order.pop(
            user.id,
            None,
        )


        # Удаляем исходный текст водителя
        try:
            await message.delete()
        except Exception:
            pass


        # Удаляем сообщение ожидания
        try:
            await context.bot.delete_message(
                chat_id=GROUP_ID,
                message_id=waiting_message_id,
            )
        except Exception:
            pass


        return


    # =====================================================
    # 3. ОБЫЧНОЕ СООБЩЕНИЕ
    # =====================================================

    admin = await is_admin(
        context,
        user.id,
    )


    # Администратор может писать свободно
    if admin:
        return


    # =====================================================
    # ОБЫЧНЫЙ ВОДИТЕЛЬ
    # =====================================================

    # Если водитель не нажал "Νέα διαδρομή",
    # удаляем его сообщение
    try:
        await message.delete()
    except Exception:
        pass


    # И показываем ему кнопку снова
    try:

        helper_message = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "Για να στείλεις διαδρομή, "
                "πάτησε «🚕 Νέα διαδρομή»."
            ),
            reply_markup=main_keyboard(),
        )


        # Оставляем подсказку несколько секунд,
        # чтобы Telegram получил клавиатуру
        await asyncio.sleep(8)


        try:
            await helper_message.delete()
        except Exception:
            pass


    except Exception as e:

        logger.warning(
            "Keyboard helper failed: %s",
            e,
        )


# =========================================================
# ВОДИТЕЛЬ БЕРЁТ ЗАКАЗ
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


    # =====================================================
    # ЗАЩИТА ОТ ДВОЙНОГО ПРИНЯТИЯ
    # =====================================================

    async with accept_lock:

        order = orders.get(order_id)


        if not order:

            await query.answer(
                "Η διαδρομή δεν είναι πλέον διαθέσιμη.",
                show_alert=True,
            )

            return


        # Нельзя забрать свой заказ
        if user.id == order["creator_id"]:

            await query.answer(
                "Δεν μπορείς να πάρεις "
                "τη δική σου διαδρομή.",
                show_alert=True,
            )

            return


        # Если уже забрали
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


    # =====================================================
    # ДАННЫЕ ВОДИТЕЛЯ
    # =====================================================

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


    # Экранируем текст для HTML
    safe_driver_name = html.escape(driver_name)
    safe_creator_name = html.escape(
        order["creator_name"]
    )
    safe_order_text = html.escape(
        order["text"]
    )


    # Кликабельное имя водителя
    driver_link = (
        f'<a href="tg://user?id={user.id}">'
        f'{safe_driver_name}</a>'
    )


    # =====================================================
    # ОБНОВЛЯЕМ ЗАКАЗ
    # =====================================================

    accepted_text = (
        "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
        f"{safe_order_text}\n\n"
        f"👤 Από: {safe_creator_name}\n"
        f"🚕 Την πήρε: {driver_link}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>"
    )


    if username:
        accepted_text += (
            f"\n💬 Telegram: "
            f"@{html.escape(user.username)}"
        )


    await query.edit_message_text(
        text=accepted_text,
        parse_mode="HTML",
    )


    # =====================================================
    # ЛИЧНОЕ УВЕДОМЛЕНИЕ АВТОРУ
    # =====================================================

    try:

        private_text = (
            "✅ Η διαδρομή σου δόθηκε.\n\n"
            f"🚕 Οδηγός: {driver_name}\n"
            f"🆔 Telegram ID: {user.id}"
        )


        if username:

            private_text += (
                f"\n💬 Telegram: {username}"
            )


        await context.bot.send_message(
            chat_id=order["creator_id"],
            text=private_text,
        )


    except Exception:

        # Telegram не позволяет боту первым
        # написать человеку, если тот раньше
        # не открывал личный чат с ботом.
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
