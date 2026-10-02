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


# Кто сейчас вводит заказ:
# user_id -> message_id сообщения ожидания
waiting_for_order = {}

# Открытые/принятые заказы
orders = {}

next_order_id = 1

accept_lock = asyncio.Lock()


# =========================================================
# ПОСТОЯННАЯ КНОПКА "НОВЫЙ ЗАКАЗ"
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
# /ID
# Показывает ID текущего чата/группы
# =========================================================

async def show_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}"
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

    # Личный чат
    if update.effective_chat.id != GROUP_ID:
        await update.message.reply_text(
            "🚕 Cyprus Drivers Order\n\n"
            "Χρησιμοποίησε την ομάδα "
            "«Κούρσες δωρεάν» για τις διαδρομές."
        )
        return

    # В группе удаляем сам /start
    try:
        await update.message.delete()
    except Exception:
        pass

    # Показываем постоянную кнопку
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

        # Удаляем старое ожидание этого водителя,
        # если оно осталось
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


        # Теперь этому водителю разрешено
        # отправить одно сообщение с заказом
        waiting_for_order[user.id] = (
            waiting_message.message_id
        )


        # Удаляем сообщение,
        # которое Telegram отправил после
        # нажатия Reply-кнопки
        try:
            await message.delete()
        except Exception:
            pass

        return


    # =====================================================
    # 2. ВОДИТЕЛЬ ВВОДИТ ИНФОРМАЦИЮ О ЗАКАЗЕ
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


        # Большая кнопка на отдельной строке
        order_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
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
                reply_markup=order_keyboard,
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


        # Ввод заказа закончен
        waiting_for_order.pop(
            user.id,
            None,
        )


        # Удаляем исходное сообщение водителя
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


    # Администраторы могут писать свободно
    if admin:
        return


    # Обычный водитель без "Νέα διαδρομή"
    # писать не может
    try:
        await message.delete()
    except Exception:
        pass


    # Показываем ему кнопку ещё раз,
    # чтобы он не оказался без неё
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

        # Даём телефону получить клавиатуру
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


    # Защита: заказ получает только первый
    async with accept_lock:

        order = orders.get(order_id)


        if not order:
            await query.answer(
                "Η διαδρομή δεν είναι πλέον διαθέσιμη.",
                show_alert=True,
            )
            return


        # Свой заказ брать нельзя
        if user.id == order["creator_id"]:
            await query.answer(
                "Δεν μπορείς να πάρεις "
                "τη δική σου διαδρομή.",
                show_alert=True,
            )
            return


        # Заказ уже забрали
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


    # =====================================================
    # ДАННЫЕ ВОДИТЕЛЯ
    # =====================================================

    driver_name = (
        user.full_name
        or user.first_name
        or "Οδηγός"
    )


    safe_driver_name = html.escape(
        driver_name
    )

    safe_creator_name = html.escape(
        order["creator_name"]
    )

    safe_order_text = html.escape(
        order["text"]
    )


    # Кликабельное имя внутри текста
    driver_link = (
        f'<a href="tg://user?id={user.id}">'
        f'{safe_driver_name}</a>'
    )


    # =====================================================
    # ТЕКСТ ПОСЛЕ ПРИНЯТИЯ
    # =====================================================

    accepted_text = (
        "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
        f"{safe_order_text}\n\n"
        f"👤 Από: {safe_creator_name}\n"
        f"🚕 Την πήρε: {driver_link}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>"
    )


    if user.username:
        accepted_text += (
            "\n💬 Telegram: "
            f"@{html.escape(user.username)}"
        )


    # =====================================================
    # БОЛЬШАЯ КНОПКА "ОТКРЫТЬ ПРОФИЛЬ"
    # =====================================================

    profile_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👤 ΑΝΟΙΓΜΑ ΠΡΟΦΙΛ ΟΔΗΓΟΥ",
                    url=f"tg://user?id={user.id}",
                )
            ]
        ]
    )


    # Обновляем тот же заказ
    await query.edit_message_text(
        text=accepted_text,
        parse_mode="HTML",
        reply_markup=profile_keyboard,
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


        if user.username:
            private_text += (
                f"\n💬 Telegram: @{user.username}"
            )


        await context.bot.send_message(
            chat_id=order["creator_id"],
            text=private_text,
        )

    except Exception:
        # Если человек раньше не открывал бота
        # лично, Telegram может не разрешить
        # отправить ему личное сообщение
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


    # Команда для получения ID любой группы
    app.add_handler(
        CommandHandler(
            "id",
            show_id,
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
