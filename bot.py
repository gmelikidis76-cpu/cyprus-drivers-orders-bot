import os
import logging
import asyncio
import html

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
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

DRIVERS_GROUP_ID = -1004449292276
CLIENTS_GROUP_ID = -1004401199110

BOT_USERNAME = "CyprusDriversOrdersBot"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

private_waiting = {}
orders = {}
next_order_id = 1

accept_lock = asyncio.Lock()


# =========================================================
# КНОПКИ В ГРУППАХ
# =========================================================

def group_entry_keyboard(role):

    if role == "driver":

        text = "🚕 Νέα διαδρομή"
        start = "driver"

    else:

        text = "🚕 REQUEST A TAXI"
        start = "client"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text,
                    url=(
                        f"https://t.me/"
                        f"{BOT_USERNAME}"
                        f"?start={start}"
                    ),
                )
            ]
        ]
    )


# =========================================================
# КНОПКА SKIP PRICE
# =========================================================

def price_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🤝 SKIP / NOT SURE",
                    callback_data="skip_price",
                )
            ]
        ]
    )


# =========================================================
# ПРОВЕРКА ВОДИТЕЛЯ
# =========================================================

async def is_driver_member(
    context,
    user_id,
):

    try:

        member = await context.bot.get_chat_member(
            chat_id=DRIVERS_GROUP_ID,
            user_id=user_id,
        )

        return member.status not in (
            "left",
            "kicked",
        )

    except Exception as e:

        logger.warning(
            "Driver membership check failed: %s",
            e,
        )

        return False


# =========================================================
# ПРОВЕРКА АДМИНИСТРАТОРА
# =========================================================

async def is_driver_admin(
    context,
    user_id,
):

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
# УДАЛЕНИЕ СТАРОЙ НИЖНЕЙ КЛАВИАТУРЫ
# =========================================================

async def remove_old_reply_keyboard(
    context,
    chat_id,
):

    try:

        message = await context.bot.send_message(
            chat_id=chat_id,
            text="Updating menu…",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True,
        )

        await asyncio.sleep(1)

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as e:

        logger.warning(
            "Could not remove old keyboard: %s",
            e,
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

    if not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user


    # =====================================================
    # ГРУППА ВОДИТЕЛЕЙ
    # =====================================================

    if chat_id == DRIVERS_GROUP_ID:

        try:
            await update.message.delete()
        except Exception:
            pass

        # Удаляем старую ReplyKeyboard
        await remove_old_reply_keyboard(
            context,
            DRIVERS_GROUP_ID,
        )

        # Новая INLINE кнопка
        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                "Για να δημιουργήσεις νέα διαδρομή, "
                "πάτησε το κουμπί παρακάτω.\n\n"
                "Η δημιουργία γίνεται ιδιωτικά "
                "με το bot."
            ),
            reply_markup=(
                group_entry_keyboard(
                    "driver"
                )
            ),
            disable_notification=True,
        )

        return


    # =====================================================
    # КЛИЕНТСКАЯ ГРУППА
    # =====================================================

    if chat_id == CLIENTS_GROUP_ID:

        try:
            await update.message.delete()
        except Exception:
            pass

        # Удаляем старую ReplyKeyboard
        await remove_old_reply_keyboard(
            context,
            CLIENTS_GROUP_ID,
        )

        # Новая INLINE кнопка
        await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🚕 NEED A TAXI?\n\n"
                "Tap the button below "
                "to request a taxi."
            ),
            reply_markup=(
                group_entry_keyboard(
                    "client"
                )
            ),
            disable_notification=True,
        )

        return


    # =====================================================
    # ДАЛЬШЕ ТОЛЬКО ЛИЧНЫЙ ЧАТ
    # =====================================================

    if update.effective_chat.type != "private":
        return

    args = context.args or []


    # -----------------------------------------------------
    # КЛИЕНТ
    # -----------------------------------------------------

    if args and args[0] == "client":

        private_waiting[user.id] = {

            "role": "client",

            "stage": "details",

            "text": None,
        }

        await update.message.reply_text(
            "🚕 NEW TAXI REQUEST\n\n"
            "✍️ Please send your trip details "
            "in one message.\n\n"
            "For example:\n"
            "Pickup: Larnaca Airport\n"
            "Destination: Limassol\n"
            "Time: 14:30\n"
            "Passengers: 2"
        )

        return


    # -----------------------------------------------------
    # ВОДИТЕЛЬ
    # -----------------------------------------------------

    if args and args[0] == "driver":

        driver_ok = await is_driver_member(
            context,
            user.id,
        )

        if not driver_ok:

            await update.message.reply_text(
                "⛔ This option is available "
                "only to drivers."
            )

            return


        private_waiting[user.id] = {

            "role": "driver",

            "stage": "details",

            "text": None,
        }


        await update.message.reply_text(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            "✍️ Στείλε όλες τις πληροφορίες "
            "της διαδρομής σε ένα μήνυμα."
        )

        return


    # -----------------------------------------------------
    # ОБЫЧНЫЙ /START В ЛИЧКЕ
    # -----------------------------------------------------

    await update.message.reply_text(
        "🚕 Cyprus Taxi\n\n"
        "Please start from the Cyprus Taxi group."
    )


# =========================================================
# ЛИЧНЫЕ СООБЩЕНИЯ
# =========================================================

async def private_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if update.effective_chat.type != "private":
        return

    if not update.effective_user:
        return


    user = update.effective_user

    text = (
        update.message.text or ""
    ).strip()


    state = private_waiting.get(
        user.id
    )


    # -----------------------------------------------------
    # НЕТ АКТИВНОГО ОФОРМЛЕНИЯ
    # -----------------------------------------------------

    if not state:

        await update.message.reply_text(
            "🚕 Cyprus Taxi\n\n"
            "Please start from the "
            "Cyprus Taxi group."
        )

        return


    # =====================================================
    # ВОДИТЕЛЬ
    # =====================================================

    if state["role"] == "driver":

        if state["stage"] != "details":
            return

        if not text:
            return


        driver_ok = await is_driver_member(
            context,
            user.id,
        )


        if not driver_ok:

            private_waiting.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "⛔ This option is available "
                "only to drivers."
            )

            return


        try:

            await publish_driver_order(
                context=context,
                user=user,
                trip_text=text,
            )


        except Exception as e:

            logger.exception(
                "Driver order publish failed: %s",
                e,
            )

            await update.message.reply_text(
                "⚠️ Could not publish the order. "
                "Please try again."
            )

            return


        private_waiting.pop(
            user.id,
            None,
        )


        await update.message.reply_text(
            "✅ Η διαδρομή δημοσιεύτηκε "
            "στην ομάδα οδηγών."
        )

        return


    # =====================================================
    # КЛИЕНТ — МАРШРУТ
    # =====================================================

    if state["stage"] == "details":

        if not text:
            return


        state["text"] = text

        state["stage"] = "price"


        await update.message.reply_text(
            "💶 HOW MUCH ARE YOU WILLING TO PAY?\n\n"
            "Enter your offer in EUR.\n"
            "For example: 50\n\n"
            "If you are not sure, "
            "tap the button below.",
            reply_markup=price_keyboard(),
        )

        return


    # =====================================================
    # КЛИЕНТ — ЦЕНА
    # =====================================================

    if state["stage"] == "price":

        cleaned_price = (
            text
            .replace("€", "")
            .replace(",", ".")
            .strip()
        )


        try:

            price_number = float(
                cleaned_price
            )


            if price_number <= 0:

                raise ValueError


            if price_number.is_integer():

                price = str(
                    int(price_number)
                )

            else:

                price = (
                    f"{price_number:.2f}"
                    .rstrip("0")
                    .rstrip(".")
                )


        except ValueError:

            await update.message.reply_text(
                "⚠️ Please enter only "
                "the amount in EUR.\n\n"
                "Example: 50\n\n"
                "Or tap "
                "«🤝 SKIP / NOT SURE».",
                reply_markup=price_keyboard(),
            )

            return


        await finish_client_order(
            context=context,
            user=user,
            trip_text=state["text"],
            price=price,
        )

        return


# =========================================================
# SKIP PRICE
# =========================================================

async def skip_price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return


    user = query.from_user


    state = private_waiting.get(
        user.id
    )


    if (
        not state
        or state.get("role") != "client"
        or state.get("stage") != "price"
    ):

        await query.answer(
            "This request is no longer active.",
            show_alert=True,
        )

        return


    await query.answer()


    try:

        await query.edit_message_text(
            "💶 Price: To be discussed"
        )

    except Exception:
        pass


    await finish_client_order(
        context=context,
        user=user,
        trip_text=state["text"],
        price=None,
    )


# =========================================================
# ЗАВЕРШЕНИЕ КЛИЕНТСКОГО ЗАКАЗА
# =========================================================

async def finish_client_order(
    context,
    user,
    trip_text,
    price,
):

    try:

        order_id = await publish_client_order(
            context=context,
            user=user,
            trip_text=trip_text,
            price=price,
        )


    except Exception as e:

        logger.exception(
            "Client order publish failed: %s",
            e,
        )


        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ Sorry, we could not send "
                "your request to the drivers.\n"
                "Please try again."
            ),
        )

        return


    private_waiting.pop(
        user.id,
        None,
    )


    if price is None:

        price_text = (
            "💶 Price: To be discussed"
        )

    else:

        price_text = (
            f"💶 Your offer: €{price}"
        )


    await context.bot.send_message(
        chat_id=user.id,
        text=(
            "🔎 LOOKING FOR A DRIVER\n\n"
            f"{trip_text}\n\n"
            f"{price_text}\n"
            f"🔢 Request: #{order_id}\n\n"
            "⏳ Your request has been sent "
            "to our drivers."
        ),
    )


# =========================================================
# ПУБЛИКАЦИЯ ЗАКАЗА ВОДИТЕЛЯ
# =========================================================

async def publish_driver_order(
    context,
    user,
    trip_text,
):

    global next_order_id


    order_id = next_order_id

    next_order_id += 1


    creator_name = (
        user.full_name
        or user.first_name
        or "Οδηγός"
    )


    order_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
                    callback_data=(
                        f"take:{order_id}"
                    ),
                )
            ]
        ]
    )


    # =====================================================
    # ГОТОВЫЙ ЗАКАЗ ВОДИТЕЛЯ
    #
    # ТОЛЬКО ЗДЕСЬ ОБЫЧНОЕ УВЕДОМЛЕНИЕ
    # =====================================================

    order_message = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            f"{trip_text}\n\n"
            f"👤 Από: {creator_name}"
        ),
        reply_markup=order_keyboard,
        disable_notification=False,
    )


    orders[order_id] = {

        "type": "driver",

        "creator_id": user.id,

        "creator_name": creator_name,

        "text": trip_text,

        "price": None,

        "status": "open",

        "driver_message_id": (
            order_message.message_id
        ),
    }


    return order_id


# =========================================================
# ПУБЛИКАЦИЯ КЛИЕНТСКОГО ЗАКАЗА
# =========================================================

async def publish_client_order(
    context,
    user,
    trip_text,
    price,
):

    global next_order_id


    order_id = next_order_id

    next_order_id += 1


    customer_name = (
        user.full_name
        or user.first_name
        or "Customer"
    )


    if price is None:

        driver_price_text = (
            "💶 Τιμή: Συζητήσιμη"
        )

    else:

        driver_price_text = (
            f"💶 Προσφορά πελάτη: €{price}"
        )


    order_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
                    callback_data=(
                        f"take:{order_id}"
                    ),
                )
            ]
        ]
    )


    # =====================================================
    # ГОТОВЫЙ КЛИЕНТСКИЙ ЗАКАЗ
    #
    # ТОЛЬКО ЭТО СООБЩЕНИЕ ИДЁТ В ГРУППУ
    # ВОДИТЕЛЕЙ КАК ОБЫЧНОЕ УВЕДОМЛЕНИЕ
    # =====================================================

    order_message = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ\n\n"
            f"{trip_text}\n\n"
            f"{driver_price_text}\n"
            f"👤 Πελάτης: {customer_name}\n"
            f"🔢 Αριθμός: #{order_id}"
        ),
        reply_markup=order_keyboard,
        disable_notification=False,
    )


    orders[order_id] = {

        "type": "client",

        "creator_id": user.id,

        "creator_name": customer_name,

        "client_username": user.username,

        "text": trip_text,

        "price": price,

        "status": "open",

        "driver_message_id": (
            order_message.message_id
        ),
    }


    return order_id


# =========================================================
# ПРИНЯТИЕ ЗАКАЗА
# =========================================================

async def take_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return


    user = query.from_user


    # -----------------------------------------------------
    # ТОЛЬКО ЧЛЕН ГРУППЫ ВОДИТЕЛЕЙ
    # -----------------------------------------------------

    driver_ok = await is_driver_member(
        context,
        user.id,
    )


    if not driver_ok:

        await query.answer(
            "Μόνο οι οδηγοί μπορούν "
            "να πάρουν διαδρομή.",
            show_alert=True,
        )

        return


    try:

        order_id = int(
            query.data.split(":")[1]
        )


    except (
        ValueError,
        IndexError,
        AttributeError,
    ):

        await query.answer()

        return


    # -----------------------------------------------------
    # ЗАЩИТА ОТ ДВОЙНОГО ПРИНЯТИЯ
    # -----------------------------------------------------

    async with accept_lock:


        order = orders.get(
            order_id
        )


        if not order:

            await query.answer(
                "Η διαδρομή δεν είναι "
                "πλέον διαθέσιμη.",
                show_alert=True,
            )

            return


        # Водитель не может взять
        # собственный водительский заказ
        if (
            order["type"] == "driver"
            and
            user.id == order["creator_id"]
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

        order["accepted_by"] = (
            user.id
        )


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
    # КАРТОЧКА ПРИНЯТОГО ЗАКАЗА
    # =====================================================

    if order["type"] == "client":


        if order["price"] is None:

            price_text = (
                "💶 Τιμή: Συζητήσιμη"
            )

        else:

            price_text = (
                "💶 Προσφορά πελάτη: "
                f"€{html.escape(str(order['price']))}"
            )


        accepted_text = (

            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"

            f"{safe_order_text}\n\n"

            f"{price_text}\n"

            f"👤 Πελάτης: "
            f"{safe_creator_name}\n"

            f"🚕 Την πήρε: "
            f"{driver_link}\n"

            f"🆔 Telegram ID: "
            f"<code>{user.id}</code>\n"

            f"🔢 Αριθμός: "
            f"#{order_id}"
        )


    else:

        accepted_text = (

            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"

            f"{safe_order_text}\n\n"

            f"👤 Από: "
            f"{safe_creator_name}\n"

            f"🚕 Την πήρε: "
            f"{driver_link}\n"

            f"🆔 Telegram ID: "
            f"<code>{user.id}</code>"
        )


    # -----------------------------------------------------
    # USERNAME + КНОПКА ПРОФИЛЯ
    # -----------------------------------------------------

    if user.username:

        accepted_text += (
            "\n💬 Telegram: "
            f"@{html.escape(user.username)}"
        )


        driver_profile_keyboard = (
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👤 ΑΝΟΙΓΜΑ "
                            "ΠΡΟΦΙΛ ΟΔΗΓΟΥ",
                            url=(
                                f"https://t.me/"
                                f"{user.username}"
                            ),
                        )
                    ]
                ]
            )
        )


    else:

        driver_profile_keyboard = None


    # =====================================================
    # РЕДАКТИРУЕМ СУЩЕСТВУЮЩУЮ КАРТОЧКУ
    #
    # НОВОГО СООБЩЕНИЯ В ГРУППУ НЕ СОЗДАЁТСЯ
    # =====================================================

    await query.edit_message_text(
        text=accepted_text,
        parse_mode="HTML",
        reply_markup=(
            driver_profile_keyboard
        ),
    )


    # =====================================================
    # ЕСЛИ ЗАКАЗ КЛИЕНТА
    # =====================================================

    if order["type"] == "client":


        if order["price"] is None:

            client_price_text = (
                "💶 Price: To be discussed"
            )

        else:

            client_price_text = (
                "💶 Your offer: "
                f"€{html.escape(str(order['price']))}"
            )


        client_text = (

            "✅ DRIVER FOUND\n\n"

            f"{safe_order_text}\n\n"

            f"{client_price_text}\n"

            f"🚕 Driver: "
            f"{driver_link}\n"

            f"🔢 Request: "
            f"#{order_id}"
        )


        if user.username:

            client_text += (
                "\n💬 Telegram: "
                f"@{html.escape(user.username)}"
            )


            client_driver_keyboard = (
                InlineKeyboardMarkup(
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
            )


        else:

            client_driver_keyboard = None


        # ---------------------------------------------
        # УВЕДОМЛЕНИЕ КЛИЕНТУ ИДЁТ ТОЛЬКО В ЛИЧКУ
        # ---------------------------------------------

        try:

            await context.bot.send_message(
                chat_id=(
                    order["creator_id"]
                ),
                text=client_text,
                parse_mode="HTML",
                reply_markup=(
                    client_driver_keyboard
                ),
            )


        except Exception as e:

            logger.warning(
                "Client notification failed: %s",
                e,
            )


    # =====================================================
    # ЕСЛИ ЗАКАЗ СОЗДАЛ ВОДИТЕЛЬ
    # =====================================================

    else:


        private_text = (

            "✅ Η διαδρομή σου δόθηκε.\n\n"

            f"🚕 Οδηγός: "
            f"{driver_name}\n"

            f"🆔 Telegram ID: "
            f"{user.id}"
        )


        if user.username:

            private_text += (
                "\n💬 Telegram: "
                f"@{user.username}"
            )


        try:

            await context.bot.send_message(
                chat_id=(
                    order["creator_id"]
                ),
                text=private_text,
            )


        except Exception as e:

            logger.warning(
                "Driver notification failed: %s",
                e,
            )


# =========================================================
# ОБЫЧНЫЕ СООБЩЕНИЯ В ГРУППЕ ВОДИТЕЛЕЙ
# =========================================================

async def driver_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not update.effective_user:
        return


    user = update.effective_user


    if user.is_bot:
        return


    # Администраторам разрешаем писать
    admin = await is_driver_admin(
        context,
        user.id,
    )


    if admin:
        return


    # Остальные сообщения удаляем
    try:

        await update.message.delete()

    except Exception:
        pass


# =========================================================
# ОБЫЧНЫЕ СООБЩЕНИЯ В КЛИЕНТСКОЙ ГРУППЕ
# =========================================================

async def client_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not update.effective_user:
        return


    if update.effective_user.is_bot:
        return


    # Клиентская группа используется
    # только как портал заказа
    try:

        await update.message.delete()

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


    # -----------------------------------------------------
    # /START
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )


    # -----------------------------------------------------
    # /ID
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "id",
            show_id,
        )
    )


    # -----------------------------------------------------
    # SKIP PRICE
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            skip_price,
            pattern=r"^skip_price$",
        )
    )


    # -----------------------------------------------------
    # ПРИНЯТЬ ЗАКАЗ
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            take_order,
            pattern=r"^take:\d+$",
        )
    )


    # -----------------------------------------------------
    # ЛИЧНЫЙ ЧАТ
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_message,
        )
    )


    # -----------------------------------------------------
    # ГРУППА ВОДИТЕЛЕЙ
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.Chat(
                DRIVERS_GROUP_ID
            )
            & filters.TEXT
            & ~filters.COMMAND,
            driver_group_text,
        )
    )


    # -----------------------------------------------------
    # КЛИЕНТСКАЯ ГРУППА
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.Chat(
                CLIENTS_GROUP_ID
            )
            & filters.TEXT
            & ~filters.COMMAND,
            client_group_text,
        )
    )


    # -----------------------------------------------------
    # ОШИБКИ
    # -----------------------------------------------------

    app.add_error_handler(
        error_handler
    )


    # -----------------------------------------------------
    # ЗАПУСК
    # -----------------------------------------------------

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
