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

# Закрытая группа водителей
DRIVERS_GROUP_ID = -1004449292276

# Клиентская группа
CLIENTS_GROUP_ID = -1004401199110

DRIVER_NEW_ORDER_BUTTON = "🚕 Νέα διαδρομή"
CLIENT_NEW_ORDER_BUTTON = "🚕 REQUEST A TAXI"
CLIENT_SKIP_PRICE_BUTTON = "🤝 SKIP / NOT SURE"


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# Водители, которые вводят заказ
driver_waiting = {}

# Клиенты:
# user_id -> {
#     "stage": "details" или "price",
#     "waiting_message_id": ...,
#     "text": ...
# }
client_waiting = {}

# Все текущие заказы
orders = {}

next_order_id = 1

accept_lock = asyncio.Lock()


# =========================================================
# КЛАВИАТУРА ВОДИТЕЛЕЙ
# =========================================================

def driver_keyboard():
    return ReplyKeyboardMarkup(
        [[DRIVER_NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Νέα διαδρομή...",
    )


# =========================================================
# ГЛАВНАЯ КЛАВИАТУРА КЛИЕНТОВ
# =========================================================

def client_keyboard():
    return ReplyKeyboardMarkup(
        [[CLIENT_NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Request a taxi...",
    )


# =========================================================
# КЛАВИАТУРА ВЫБОРА ЦЕНЫ
# =========================================================

def price_keyboard():
    return ReplyKeyboardMarkup(
        [[CLIENT_SKIP_PRICE_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Enter your offer in EUR...",
    )


# =========================================================
# ПРОВЕРКА АДМИНИСТРАТОРА ВОДИТЕЛЕЙ
# =========================================================

async def is_driver_admin(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            chat_id=DRIVERS_GROUP_ID,
            user_id=user_id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception as e:
        logger.warning(
            "Driver admin check failed: %s",
            e,
        )
        return False


# =========================================================
# /ID
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

    chat_id = update.effective_chat.id

    # Группа водителей
    if chat_id == DRIVERS_GROUP_ID:

        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 Για νέα διαδρομή πάτησε "
                "το κουμπί παρακάτω."
            ),
            reply_markup=driver_keyboard(),
        )
        return

    # Клиентская группа
    if chat_id == CLIENTS_GROUP_ID:

        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🚕 NEED A TAXI?\n\n"
                "Tap the button below to request a taxi."
            ),
            reply_markup=client_keyboard(),
        )
        return

    # Личный чат
    await update.message.reply_text(
        "🚕 Cyprus Taxi\n\n"
        "Please use the Cyprus Taxi group "
        "to request a taxi."
    )


# =========================================================
# СООБЩЕНИЯ В ГРУППЕ ВОДИТЕЛЕЙ
# =========================================================

async def driver_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global next_order_id

    message = update.message

    if not message:
        return

    if update.effective_chat.id != DRIVERS_GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = (message.text or "").strip()

    # -----------------------------------------------------
    # Водитель нажал "Νέα διαδρομή"
    # -----------------------------------------------------

    if text == DRIVER_NEW_ORDER_BUTTON:

        old_waiting_id = driver_waiting.pop(
            user.id,
            None,
        )

        if old_waiting_id:
            try:
                await context.bot.delete_message(
                    chat_id=DRIVERS_GROUP_ID,
                    message_id=old_waiting_id,
                )
            except Exception:
                pass

        driver_name = (
            user.first_name
            or user.full_name
            or "Οδηγός"
        )

        waiting_message = await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"⏳ Αναμονή πληροφοριών από "
                f"{driver_name}..."
            ),
            reply_markup=driver_keyboard(),
        )

        driver_waiting[user.id] = (
            waiting_message.message_id
        )

        try:
            await message.delete()
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # Водитель вводит свой заказ
    # -----------------------------------------------------

    if user.id in driver_waiting:

        if not text:
            return

        waiting_message_id = driver_waiting[user.id]

        creator_name = (
            user.full_name
            or user.first_name
            or "Οδηγός"
        )

        order_id = next_order_id
        next_order_id += 1

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

        try:
            order_message = await context.bot.send_message(
                chat_id=DRIVERS_GROUP_ID,
                text=(
                    "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                    f"{text}\n\n"
                    f"👤 Από: {creator_name}"
                ),
                reply_markup=order_keyboard,
            )

        except Exception as e:
            logger.exception(
                "Driver order publish failed: %s",
                e,
            )
            return

        orders[order_id] = {
            "type": "driver",
            "creator_id": user.id,
            "creator_name": creator_name,
            "text": text,
            "price": None,
            "status": "open",
            "driver_message_id": order_message.message_id,
            "client_message_id": None,
        }

        driver_waiting.pop(
            user.id,
            None,
        )

        try:
            await message.delete()
        except Exception:
            pass

        try:
            await context.bot.delete_message(
                chat_id=DRIVERS_GROUP_ID,
                message_id=waiting_message_id,
            )
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # Обычные сообщения
    # -----------------------------------------------------

    admin = await is_driver_admin(
        context,
        user.id,
    )

    if admin:
        return

    try:
        await message.delete()
    except Exception:
        pass

    try:
        helper_message = await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "Για να στείλεις διαδρομή, "
                "πάτησε «🚕 Νέα διαδρομή»."
            ),
            reply_markup=driver_keyboard(),
        )

        await asyncio.sleep(8)

        try:
            await helper_message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.warning(
            "Driver keyboard helper failed: %s",
            e,
        )


# =========================================================
# СОЗДАНИЕ КЛИЕНТСКОГО ЗАКАЗА ПОСЛЕ ЦЕНЫ
# =========================================================

async def publish_client_order(
    context,
    user,
    trip_text,
    price,
    price_waiting_message_id,
):
    global next_order_id

    customer_name = (
        user.full_name
        or user.first_name
        or "Customer"
    )

    order_id = next_order_id
    next_order_id += 1

    # Как цена показывается водителям
    if price is None:
        driver_price_text = "💶 Τιμή: Συζητήσιμη"
        client_price_text = "💶 Price: To be discussed"
    else:
        driver_price_text = (
            f"💶 Προσφορά πελάτη: €{price}"
        )
        client_price_text = (
            f"💶 Your offer: €{price}"
        )

    driver_order_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
                    callback_data=f"take:{order_id}",
                )
            ]
        ]
    )

    # -----------------------------------------------------
    # Отправляем водителям
    # -----------------------------------------------------

    try:
        driver_order_message = await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ\n\n"
                f"{trip_text}\n\n"
                f"{driver_price_text}\n"
                f"👤 Πελάτης: {customer_name}\n"
                f"🔢 Αριθμός: #{order_id}"
            ),
            reply_markup=driver_order_keyboard,
        )

    except Exception as e:
        logger.exception(
            "Client order forwarding failed: %s",
            e,
        )

        await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "⚠️ Sorry, we could not send "
                "your request to the drivers.\n"
                "Please try again."
            ),
            reply_markup=client_keyboard(),
        )

        client_waiting.pop(
            user.id,
            None,
        )

        return

    # -----------------------------------------------------
    # Карточка клиента
    # -----------------------------------------------------

    try:
        client_status_message = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🔎 LOOKING FOR A DRIVER\n\n"
                f"{trip_text}\n\n"
                f"{client_price_text}\n"
                f"🔢 Request: #{order_id}\n\n"
                "⏳ Your request has been sent "
                "to our drivers."
            ),
            reply_markup=client_keyboard(),
        )

    except Exception as e:
        logger.warning(
            "Client status message failed: %s",
            e,
        )

        client_status_message = None

    orders[order_id] = {
        "type": "client",
        "creator_id": user.id,
        "creator_name": customer_name,
        "client_username": user.username,
        "text": trip_text,
        "price": price,
        "status": "open",
        "driver_message_id": (
            driver_order_message.message_id
        ),
        "client_message_id": (
            client_status_message.message_id
            if client_status_message
            else None
        ),
    }

    client_waiting.pop(
        user.id,
        None,
    )

    # Удаляем вопрос о цене
    if price_waiting_message_id:
        try:
            await context.bot.delete_message(
                chat_id=CLIENTS_GROUP_ID,
                message_id=price_waiting_message_id,
            )
        except Exception:
            pass


# =========================================================
# СООБЩЕНИЯ В КЛИЕНТСКОЙ ГРУППЕ
# =========================================================

async def client_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message

    if not message:
        return

    if update.effective_chat.id != CLIENTS_GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = (message.text or "").strip()

    # -----------------------------------------------------
    # REQUEST A TAXI
    # -----------------------------------------------------

    if text == CLIENT_NEW_ORDER_BUTTON:

        old_state = client_waiting.pop(
            user.id,
            None,
        )

        if old_state:
            old_message_id = old_state.get(
                "waiting_message_id"
            )

            if old_message_id:
                try:
                    await context.bot.delete_message(
                        chat_id=CLIENTS_GROUP_ID,
                        message_id=old_message_id,
                    )
                except Exception:
                    pass

        customer_name = (
            user.first_name
            or user.full_name
            or "Customer"
        )

        waiting_message = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🚕 NEW TAXI REQUEST\n\n"
                f"👤 {customer_name}\n\n"
                "✍️ Please send your trip details "
                "in one message.\n\n"
                "For example:\n"
                "Pickup: Larnaca Airport\n"
                "Destination: Limassol\n"
                "Time: 14:30\n"
                "Passengers: 2"
            ),
            reply_markup=client_keyboard(),
        )

        client_waiting[user.id] = {
            "stage": "details",
            "waiting_message_id": (
                waiting_message.message_id
            ),
            "text": None,
        }

        try:
            await message.delete()
        except Exception:
            pass

        return

    # -----------------------------------------------------
    # КЛИЕНТ ВВОДИТ ДЕТАЛИ
    # -----------------------------------------------------

    state = client_waiting.get(user.id)

    if state and state["stage"] == "details":

        if not text:
            return

        old_waiting_message_id = state.get(
            "waiting_message_id"
        )

        trip_text = text

        # Удаляем исходный текст клиента
        try:
            await message.delete()
        except Exception:
            pass

        # Удаляем старую инструкцию
        if old_waiting_message_id:
            try:
                await context.bot.delete_message(
                    chat_id=CLIENTS_GROUP_ID,
                    message_id=old_waiting_message_id,
                )
            except Exception:
                pass

        # Теперь спрашиваем цену
        price_message = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "💶 HOW MUCH ARE YOU WILLING TO PAY?\n\n"
                "Enter your offer in EUR.\n"
                "For example: 50\n\n"
                "If you are not sure, tap "
                "«🤝 SKIP / NOT SURE»."
            ),
            reply_markup=price_keyboard(),
        )

        client_waiting[user.id] = {
            "stage": "price",
            "waiting_message_id": (
                price_message.message_id
            ),
            "text": trip_text,
        }

        return

    # -----------------------------------------------------
    # КЛИЕНТ ВЫБИРАЕТ SKIP / NOT SURE
    # -----------------------------------------------------

    if (
        state
        and state["stage"] == "price"
        and text == CLIENT_SKIP_PRICE_BUTTON
    ):

        trip_text = state["text"]

        price_waiting_message_id = state.get(
            "waiting_message_id"
        )

        try:
            await message.delete()
        except Exception:
            pass

        await publish_client_order(
            context=context,
            user=user,
            trip_text=trip_text,
            price=None,
            price_waiting_message_id=(
                price_waiting_message_id
            ),
        )

        return

    # -----------------------------------------------------
    # КЛИЕНТ ВВОДИТ ЦЕНУ
    # -----------------------------------------------------

    if state and state["stage"] == "price":

        # Разрешаем:
        # 50
        # €50
        # 50€
        cleaned_price = (
            text.replace("€", "")
            .replace(",", ".")
            .strip()
        )

        try:
            price_number = float(cleaned_price)

            if price_number <= 0:
                raise ValueError

            # Красивое отображение без .0
            if price_number.is_integer():
                price = str(int(price_number))
            else:
                price = (
                    f"{price_number:.2f}"
                    .rstrip("0")
                    .rstrip(".")
                )

        except ValueError:

            try:
                await message.delete()
            except Exception:
                pass

            error_message = await context.bot.send_message(
                chat_id=CLIENTS_GROUP_ID,
                text=(
                    "⚠️ Please enter only the amount "
                    "in EUR.\n\n"
                    "Example: 50\n\n"
                    "Or tap «🤝 SKIP / NOT SURE»."
                ),
                reply_markup=price_keyboard(),
            )

            # Обновляем ID подсказки,
            # чтобы не потерять клавиатуру
            old_price_message_id = state.get(
                "waiting_message_id"
            )

            if old_price_message_id:
                try:
                    await context.bot.delete_message(
                        chat_id=CLIENTS_GROUP_ID,
                        message_id=old_price_message_id,
                    )
                except Exception:
                    pass

            state["waiting_message_id"] = (
                error_message.message_id
            )

            return

        trip_text = state["text"]

        price_waiting_message_id = state.get(
            "waiting_message_id"
        )

        try:
            await message.delete()
        except Exception:
            pass

        await publish_client_order(
            context=context,
            user=user,
            trip_text=trip_text,
            price=price,
            price_waiting_message_id=(
                price_waiting_message_id
            ),
        )

        return

    # -----------------------------------------------------
    # ОБЫЧНОЕ СООБЩЕНИЕ КЛИЕНТА
    # -----------------------------------------------------

    try:
        await message.delete()
    except Exception:
        pass

    try:
        helper_message = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "To request a taxi, tap "
                "«🚕 REQUEST A TAXI» below."
            ),
            reply_markup=client_keyboard(),
        )

        await asyncio.sleep(8)

        try:
            await helper_message.delete()
        except Exception:
            pass

    except Exception as e:
        logger.warning(
            "Client keyboard helper failed: %s",
            e,
        )


# =========================================================
# ВОДИТЕЛЬ ПРИНИМАЕТ ЗАКАЗ
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

        # Водитель не может взять
        # собственный водительский заказ
        if (
            order["type"] == "driver"
            and user.id == order["creator_id"]
        ):
            await query.answer(
                "Δεν μπορείς να πάρεις "
                "τη δική σου διαδρομή.",
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

    safe_driver_name = html.escape(
        driver_name
    )

    safe_creator_name = html.escape(
        order["creator_name"]
    )

    safe_order_text = html.escape(
        order["text"]
    )

    driver_link = (
        f'<a href="tg://user?id={user.id}">'
        f'{safe_driver_name}</a>'
    )

    # =====================================================
    # КАРТОЧКА В ГРУППЕ ВОДИТЕЛЕЙ
    # =====================================================

    if order["type"] == "client":

        if order["price"] is None:
            price_text = (
                "💶 Τιμή: Συζητήσιμη"
            )
        else:
            price_text = (
                "💶 Προσφορά πελάτη: "
                f"€{html.escape(order['price'])}"
            )

        accepted_text = (
            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
            f"{safe_order_text}\n\n"
            f"{price_text}\n"
            f"👤 Πελάτης: {safe_creator_name}\n"
            f"🚕 Την πήρε: {driver_link}\n"
            f"🆔 Telegram ID: "
            f"<code>{user.id}</code>\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    else:

        accepted_text = (
            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
            f"{safe_order_text}\n\n"
            f"👤 Από: {safe_creator_name}\n"
            f"🚕 Την πήρε: {driver_link}\n"
            f"🆔 Telegram ID: "
            f"<code>{user.id}</code>"
        )

    if user.username:
        accepted_text += (
            "\n💬 Telegram: "
            f"@{html.escape(user.username)}"
        )

    # Кнопка профиля водителя
    if user.username:

        driver_profile_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "👤 ΑΝΟΙΓΜΑ ΠΡΟΦΙΛ ΟΔΗΓΟΥ",
                        url=f"https://t.me/{user.username}",
                    )
                ]
            ]
        )

    else:
        driver_profile_keyboard = None

    await query.edit_message_text(
        text=accepted_text,
        parse_mode="HTML",
        reply_markup=driver_profile_keyboard,
    )

    # =====================================================
    # КЛИЕНТСКИЙ ЗАКАЗ
    # =====================================================

    if order["type"] == "client":

        if order["price"] is None:
            client_price_text = (
                "💶 Price: To be discussed"
            )
        else:
            client_price_text = (
                "💶 Your offer: "
                f"€{html.escape(order['price'])}"
            )

        client_text = (
            "✅ DRIVER FOUND\n\n"
            f"{safe_order_text}\n\n"
            f"{client_price_text}\n"
            f"🚕 Driver: {driver_link}\n"
            f"🔢 Request: #{order_id}"
        )

        if user.username:
            client_text += (
                "\n💬 Telegram: "
                f"@{html.escape(user.username)}"
            )

        if user.username:

            client_driver_keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👤 OPEN DRIVER PROFILE",
                            url=(
                                f"https://t.me/"
                                f"{user.username}"
                            ),
                        )
                    ]
                ]
            )

        else:
            client_driver_keyboard = None

        # Обновляем карточку клиента
        if order.get("client_message_id"):

            try:
                await context.bot.edit_message_text(
                    chat_id=CLIENTS_GROUP_ID,
                    message_id=order[
                        "client_message_id"
                    ],
                    text=client_text,
                    parse_mode="HTML",
                    reply_markup=(
                        client_driver_keyboard
                    ),
                )

            except Exception as e:
                logger.warning(
                    "Client card update failed: %s",
                    e,
                )

        # Пытаемся написать клиенту лично
        try:
            private_client_text = (
                "✅ DRIVER FOUND\n\n"
                f"🚕 Driver: {driver_name}\n"
                f"{client_price_text}\n"
                f"🔢 Request: #{order_id}"
            )

            if user.username:
                private_client_text += (
                    f"\n💬 Telegram: "
                    f"@{user.username}"
                )

            await context.bot.send_message(
                chat_id=order["creator_id"],
                text=private_client_text,
                reply_markup=client_driver_keyboard,
            )

        except Exception:
            pass

    # =====================================================
    # ВОДИТЕЛЬСКИЙ ЗАКАЗ
    # =====================================================

    else:

        try:
            private_text = (
                "✅ Η διαδρομή σου δόθηκε.\n\n"
                f"🚕 Οδηγός: {driver_name}\n"
                f"🆔 Telegram ID: {user.id}"
            )

            if user.username:
                private_text += (
                    f"\n💬 Telegram: "
                    f"@{user.username}"
                )

            await context.bot.send_message(
                chat_id=order["creator_id"],
                text=private_text,
            )

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

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

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

    # Группа водителей
    app.add_handler(
        MessageHandler(
            filters.Chat(DRIVERS_GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            driver_group_message,
        )
    )

    # Клиентская группа
    app.add_handler(
        MessageHandler(
            filters.Chat(CLIENTS_GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            client_group_message,
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
