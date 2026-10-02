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

DRIVERS_GROUP_ID = -1004449292276
CLIENTS_GROUP_ID = -1004401199110

BOT_USERNAME = "CyprusDriversOrdersBot"

DRIVER_NEW_ORDER_BUTTON = "🚕 Νέα διαδρομή"
CLIENT_NEW_ORDER_BUTTON = "🚕 REQUEST A TAXI"
CLIENT_SKIP_PRICE_BUTTON = "🤝 SKIP / NOT SURE"


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# СОСТОЯНИЕ
# =========================================================

# Личные диалоги оформления:
#
# user_id -> {
#     "role": "client" / "driver",
#     "stage": "ready" / "details" / "price",
#     "text": ...
# }
private_waiting = {}

# Текущие заказы
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
# КЛАВИАТУРА КЛИЕНТОВ
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
# КЛАВИАТУРА ЦЕНЫ
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
# КНОПКА НАЧАЛА ОФОРМЛЕНИЯ В ЛИЧНОМ ЧАТЕ
# =========================================================

def private_start_keyboard(role):

    if role == "driver":
        label = "🚕 CREATE DRIVER ORDER"
        data = "private_driver_start"

    else:
        label = "🚕 START TAXI REQUEST"
        data = "private_client_start"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=data,
                )
            ]
        ]
    )


# =========================================================
# КНОПКА ПЕРЕХОДА ИЗ ГРУППЫ В ЛИЧНЫЙ ЧАТ
# =========================================================

def open_bot_keyboard(role):

    if role == "driver":
        start_param = "driver"
        label = "🚕 CREATE ORDER PRIVATELY"

    else:
        start_param = "client"
        label = "🚕 REQUEST TAXI PRIVATELY"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label,
                    url=(
                        f"https://t.me/"
                        f"{BOT_USERNAME}"
                        f"?start={start_param}"
                    ),
                )
            ]
        ]
    )


# =========================================================
# ПРОВЕРКА АДМИНИСТРАТОРА ВОДИТЕЛЕЙ
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
# ПРОВЕРКА ЧТО ПОЛЬЗОВАТЕЛЬ ЕСТЬ В ГРУППЕ ВОДИТЕЛЕЙ
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


    # -----------------------------------------------------
    # ГРУППА ВОДИТЕЛЕЙ
    # -----------------------------------------------------

    if chat_id == DRIVERS_GROUP_ID:

        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 Για να δημιουργήσεις νέα διαδρομή, "
                "πάτησε το κουμπί παρακάτω.\n\n"
                "Η δημιουργία γίνεται ιδιωτικά με το bot."
            ),
            reply_markup=driver_keyboard(),
            disable_notification=True,
        )

        return


    # -----------------------------------------------------
    # КЛИЕНТСКАЯ ГРУППА
    # -----------------------------------------------------

    if chat_id == CLIENTS_GROUP_ID:

        try:
            await update.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🚕 NEED A TAXI?\n\n"
                "Tap the button below "
                "to request a taxi."
            ),
            reply_markup=client_keyboard(),
            disable_notification=True,
        )

        return


    # -----------------------------------------------------
    # ЛИЧНЫЙ ЧАТ
    # -----------------------------------------------------

    args = context.args or []


    # Клиент пришёл из клиентской группы
    if args and args[0] == "client":

        private_waiting[
            update.effective_user.id
        ] = {
            "role": "client",
            "stage": "ready",
            "text": None,
        }

        await update.message.reply_text(
            "🚕 CYPRUS TAXI\n\n"
            "Tap the button below "
            "to start your taxi request.",
            reply_markup=(
                private_start_keyboard(
                    "client"
                )
            ),
        )

        return


    # Водитель пришёл из группы водителей
    if args and args[0] == "driver":

        driver_ok = await is_driver_member(
            context,
            update.effective_user.id,
        )

        if not driver_ok:

            await update.message.reply_text(
                "⛔ This option is only "
                "available to drivers."
            )

            return

        private_waiting[
            update.effective_user.id
        ] = {
            "role": "driver",
            "stage": "ready",
            "text": None,
        }

        await update.message.reply_text(
            "🚕 DRIVER ORDER\n\n"
            "Tap the button below "
            "to create a new order.",
            reply_markup=(
                private_start_keyboard(
                    "driver"
                )
            ),
        )

        return


    # Обычный /start в личке
    await update.message.reply_text(
        "🚕 Cyprus Taxi\n\n"
        "Please open the Cyprus Taxi group "
        "to request a taxi."
    )


# =========================================================
# ГРУППА ВОДИТЕЛЕЙ
# =========================================================

async def driver_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message

    if not message:
        return

    if (
        update.effective_chat.id
        != DRIVERS_GROUP_ID
    ):
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = (
        message.text or ""
    ).strip()


    # -----------------------------------------------------
    # ВОДИТЕЛЬ НАЖАЛ "Νέα διαδρομή"
    # -----------------------------------------------------

    if text == DRIVER_NEW_ORDER_BUTTON:

        # Сразу удаляем сообщение-кнопку
        try:
            await message.delete()
        except Exception:
            pass

        # Показываем тихое сообщение
        # с кнопкой перехода в личный чат
        helper = await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "Δημιούργησε τη διαδρομή "
                "ιδιωτικά με το bot."
            ),
            reply_markup=(
                open_bot_keyboard(
                    "driver"
                )
            ),
            disable_notification=True,
        )

        await asyncio.sleep(12)

        try:
            await helper.delete()
        except Exception:
            pass

        return


    # -----------------------------------------------------
    # АДМИНИСТРАТОР МОЖЕТ ПИСАТЬ
    # -----------------------------------------------------

    admin = await is_driver_admin(
        context,
        user.id,
    )

    if admin:
        return


    # -----------------------------------------------------
    # ОБЫЧНЫЕ СООБЩЕНИЯ ВОДИТЕЛЕЙ УДАЛЯЕМ
    # -----------------------------------------------------

    try:
        await message.delete()
    except Exception:
        pass

    try:

        helper = await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "Για νέα διαδρομή πάτησε "
                "«🚕 Νέα διαδρομή»."
            ),
            reply_markup=driver_keyboard(),
            disable_notification=True,
        )

        await asyncio.sleep(8)

        try:
            await helper.delete()
        except Exception:
            pass

    except Exception as e:

        logger.warning(
            "Driver helper failed: %s",
            e,
        )


# =========================================================
# КЛИЕНТСКАЯ ГРУППА
# =========================================================

async def client_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.message

    if not message:
        return

    if (
        update.effective_chat.id
        != CLIENTS_GROUP_ID
    ):
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = (
        message.text or ""
    ).strip()


    # -----------------------------------------------------
    # REQUEST A TAXI
    # -----------------------------------------------------

    if text == CLIENT_NEW_ORDER_BUTTON:

        # Удаляем сообщение от кнопки
        try:
            await message.delete()
        except Exception:
            pass

        # Тихо показываем кнопку
        # перехода в личный чат
        helper = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "Continue your taxi request "
                "privately with the bot."
            ),
            reply_markup=(
                open_bot_keyboard(
                    "client"
                )
            ),
            disable_notification=True,
        )

        await asyncio.sleep(12)

        try:
            await helper.delete()
        except Exception:
            pass

        return


    # -----------------------------------------------------
    # ДРУГИЕ СООБЩЕНИЯ КЛИЕНТОВ
    # -----------------------------------------------------

    try:
        await message.delete()
    except Exception:
        pass

    try:

        helper = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                f"👤 {user.first_name}\n\n"
                "To request a taxi, tap "
                "«🚕 REQUEST A TAXI» below."
            ),
            reply_markup=client_keyboard(),
            disable_notification=True,
        )

        await asyncio.sleep(8)

        try:
            await helper.delete()
        except Exception:
            pass

    except Exception as e:

        logger.warning(
            "Client helper failed: %s",
            e,
        )


# =========================================================
# КНОПКИ В ЛИЧНОМ ЧАТЕ
# =========================================================

async def private_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    user = query.from_user

    data = query.data


    # -----------------------------------------------------
    # НАЧАТЬ КЛИЕНТСКИЙ ЗАКАЗ
    # -----------------------------------------------------

    if data == "private_client_start":

        private_waiting[user.id] = {
            "role": "client",
            "stage": "details",
            "text": None,
        }

        await query.answer()

        await query.edit_message_text(
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
    # НАЧАТЬ ВОДИТЕЛЬСКИЙ ЗАКАЗ
    # -----------------------------------------------------

    if data == "private_driver_start":

        driver_ok = await is_driver_member(
            context,
            user.id,
        )

        if not driver_ok:

            await query.answer(
                "This option is only "
                "available to drivers.",
                show_alert=True,
            )

            return

        private_waiting[user.id] = {
            "role": "driver",
            "stage": "details",
            "text": None,
        }

        await query.answer()

        await query.edit_message_text(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            "✍️ Στείλε όλες τις πληροφορίες "
            "της διαδρομής σε ένα μήνυμα."
        )

        return


# =========================================================
# ЛИЧНЫЕ СООБЩЕНИЯ
# =========================================================

async def private_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if (
        update.effective_chat.type
        != "private"
    ):
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

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
            "Please start your request "
            "from the Cyprus Taxi group."
        )

        return


    # =====================================================
    # ВОДИТЕЛЬ
    # =====================================================

    if state["role"] == "driver":

        if (
            state["stage"] != "details"
            or not text
        ):
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
                "⛔ This option is only "
                "available to drivers."
            )

            return


        # Публикуем готовый заказ
        await publish_driver_order(
            context=context,
            user=user,
            trip_text=text,
        )

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
    # КЛИЕНТ
    # =====================================================


    # -----------------------------------------------------
    # КЛИЕНТ ВВОДИТ МАРШРУТ
    # -----------------------------------------------------

    if state["stage"] == "details":

        if not text:
            return

        state["text"] = text

        state["stage"] = "price"

        await update.message.reply_text(
            "💶 HOW MUCH ARE YOU WILLING TO PAY?\n\n"
            "Enter your offer in EUR.\n"
            "For example: 50\n\n"
            "If you are not sure, tap "
            "«🤝 SKIP / NOT SURE».",
            reply_markup=price_keyboard(),
        )

        return


    # -----------------------------------------------------
    # КЛИЕНТ ВВОДИТ ЦЕНУ
    # -----------------------------------------------------

    if state["stage"] == "price":

        trip_text = state["text"]


        # SKIP
        if text == CLIENT_SKIP_PRICE_BUTTON:

            price = None


        # Цена
        else:

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


                # Убираем .0
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


        # Публикуем готовый заказ
        await publish_client_order(
            context=context,
            user=user,
            trip_text=trip_text,
            price=price,
        )


        private_waiting.pop(
            user.id,
            None,
        )


        # Подтверждение клиенту
        if price is None:

            price_confirmation = (
                "💶 Price: To be discussed"
            )

        else:

            price_confirmation = (
                f"💶 Your offer: €{price}"
            )


        await update.message.reply_text(
            "🔎 LOOKING FOR A DRIVER\n\n"
            f"{trip_text}\n\n"
            f"{price_confirmation}\n\n"
            "⏳ Your request has been sent "
            "to our drivers.",
            reply_markup=client_keyboard(),
        )

        return


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
    # ГОТОВЫЙ ЗАКАЗ
    #
    # ВАЖНО:
    # disable_notification=False
    #
    # ЭТО СООБЩЕНИЕ ДОЛЖНО ДАТЬ ЗВУК
    # =====================================================

    order_message = (
        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                f"{trip_text}\n\n"
                f"👤 Από: {creator_name}"
            ),
            reply_markup=order_keyboard,
            disable_notification=False,
        )
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

        "client_message_id": None,
    }


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


    customer_name = (
        user.full_name
        or user.first_name
        or "Customer"
    )


    order_id = next_order_id

    next_order_id += 1


    # -----------------------------------------------------
    # ЦЕНА
    # -----------------------------------------------------

    if price is None:

        driver_price_text = (
            "💶 Τιμή: Συζητήσιμη"
        )

    else:

        driver_price_text = (
            f"💶 Προσφορά πελάτη: €{price}"
        )


    driver_order_keyboard = (
        InlineKeyboardMarkup(
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
    )


    # =====================================================
    # ГОТОВЫЙ ЗАКАЗ КЛИЕНТА
    #
    # ЭТО ЕДИНСТВЕННОЕ НОВОЕ СООБЩЕНИЕ
    # В ГРУППЕ ВОДИТЕЛЕЙ.
    #
    # ОНО ДОЛЖНО ПРИЙТИ СО ЗВУКОМ.
    # =====================================================

    driver_order_message = (
        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ\n\n"
                f"{trip_text}\n\n"
                f"{driver_price_text}\n"
                f"👤 Πελάτης: {customer_name}\n"
                f"🔢 Αριθμός: #{order_id}"
            ),
            reply_markup=(
                driver_order_keyboard
            ),
            disable_notification=False,
        )
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
            driver_order_message.message_id
        ),

        "client_message_id": None,
    }


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
            user.id
            == order["creator_id"]
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
    # USERNAME
    # -----------------------------------------------------

    if user.username:

        accepted_text += (
            "\n💬 Telegram: "
            f"@{html.escape(user.username)}"
        )


    # -----------------------------------------------------
    # КНОПКА ПРОФИЛЯ
    # -----------------------------------------------------

    if user.username:

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


    # -----------------------------------------------------
    # ОБНОВЛЯЕМ СУЩЕСТВУЮЩИЙ ЗАКАЗ
    #
    # Новое сообщение не создаётся.
    # -----------------------------------------------------

    await query.edit_message_text(
        text=accepted_text,
        parse_mode="HTML",
        reply_markup=(
            driver_profile_keyboard
        ),
    )


    # =====================================================
    # ЕСЛИ ЭТО ЗАКАЗ КЛИЕНТА
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


        # Кнопка профиля водителя
        if user.username:

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


        # -------------------------------------------------
        # СООБЩАЕМ КЛИЕНТУ ЛИЧНО
        # -------------------------------------------------

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
                "Private client notification "
                "failed: %s",
                e,
            )


    # =====================================================
    # ЕСЛИ ЭТО ЗАКАЗ ВОДИТЕЛЯ
    # =====================================================

    else:

        try:

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


            await context.bot.send_message(
                chat_id=(
                    order["creator_id"]
                ),
                text=private_text,
            )


        except Exception as e:

            logger.warning(
                "Private driver notification "
                "failed: %s",
                e,
            )


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
    # КОМАНДЫ
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # КНОПКИ ЛИЧНОГО ОФОРМЛЕНИЯ
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            private_button,
            pattern=(
                r"^private_"
                r"(client|driver)_start$"
            ),
        )
    )


    # -----------------------------------------------------
    # КНОПКА ПРИНЯТИЯ ЗАКАЗА
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            take_order,
            pattern=r"^take:\d+$",
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
            driver_group_message,
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
            client_group_message,
        )
    )


    # -----------------------------------------------------
    # ЛИЧНЫЙ ЧАТ С БОТОМ
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
