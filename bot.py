import os
import sqlite3
import asyncio
import logging
import html
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    TypeHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ==================== SETTINGS ====================

TOKEN = os.getenv("BOT_TOKEN")

DRIVERS_GROUP_ID = -1004449292276
CLIENTS_GROUP_ID = -1004401199110

BOT_USERNAME = "CyprusDriversOrdersBot"
DB_PATH = os.getenv("DB_PATH", "/data/orders.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()
user_state = {}


# ==================== BUTTONS ====================

BTN_NEW_DRIVER = "🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_DRIVER_ORDERS = "📋 ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ"
BTN_TAKEN = "📦 ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ"
BTN_CANCEL_DRIVER = "❌ ΑΚΥΡΩΣΗ"

BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"
BTN_CLIENT_ORDERS = "📋 MY ORDERS"
BTN_CANCEL_CLIENT = "❌ CANCEL"

OLD_NEW_DRIVER = "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ"
OLD_MY_ORDER = "📋 MY ORDER"
OLD_MY_ORDERS = "📋 MY ORDERS"
OLD_TAKEN = "📦 MY TAKEN ORDER"
OLD_TAKEN_ORDERS = "📦 MY TAKEN ORDERS"
OLD_CANCEL = "❌ CANCEL"


# ==================== DATABASE ====================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table_name, column_name):
    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return any(row["name"] == column_name for row in rows)


def init_db():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(directory, exist_ok=True)

    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                creator_name TEXT NOT NULL,
                creator_username TEXT,
                creator_role TEXT NOT NULL,
                details TEXT NOT NULL,
                price TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                taker_id INTEGER,
                taker_name TEXT,
                taker_username TEXT,
                group_chat_id INTEGER,
                group_message_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )

        if not column_exists(conn, "orders", "voice_file_id"):
            conn.execute(
                "ALTER TABLE orders ADD COLUMN voice_file_id TEXT"
            )

        if not column_exists(
            conn,
            "orders",
            "voice_group_message_id",
        ):
            conn.execute(
                """
                ALTER TABLE orders
                ADD COLUMN voice_group_message_id INTEGER
                """
            )

        conn.commit()


def get_order(order_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()


def get_creator_active_orders(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE creator_id = ?
              AND status IN ('open', 'taken')
            ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()


def get_taken_orders(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE taker_id = ?
              AND status = 'taken'
            ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()


# ==================== HELPERS ====================

def person_name(user):
    return user.full_name or user.first_name or "Telegram user"


def contact_url(user_id, username=None):
    if username:
        return f"https://t.me/{username}"

    return f"tg://user?id={user_id}"


def is_voice_order(order):
    try:
        return bool(order["voice_file_id"])
    except Exception:
        return False


async def is_driver(user_id, context):
    try:
        member = await context.bot.get_chat_member(
            DRIVERS_GROUP_ID,
            user_id,
        )
        return member.status not in ("left", "kicked")

    except Exception:
        logger.exception("Could not check driver membership")
        return False


async def is_admin_in_chat(chat_id, user_id, context):
    try:
        member = await context.bot.get_chat_member(
            chat_id,
            user_id,
        )
        return member.status in ("administrator", "creator")

    except Exception:
        return False


async def role_for(user_id, context):
    if await is_driver(user_id, context):
        return "driver"

    return "client"


def can_manage_order(order, user_id):
    return bool(
        order
        and order["status"] in ("open", "taken")
        and (
            order["creator_id"] == user_id
            or (
                order["status"] == "taken"
                and order["taker_id"] == user_id
            )
        )
    )


def participant_role(order, user_id):
    if order["creator_id"] == user_id:
        return order["creator_role"]

    return "driver"


def contact_identity(user_id, name, username):
    url = html.escape(
        contact_url(user_id, username),
        quote=True,
    )
    label = html.escape(name or "Telegram user")

    if username:
        label += " — @" + html.escape(username)

    return f'<a href="{url}">{label}</a>'


def contacts_card(order, english=False):
    creator_label = "Order creator" if english else "Από"
    driver_label = "Driver" if english else "Οδηγός"

    text = f"👤 {creator_label}: " + contact_identity(
        order["creator_id"],
        order["creator_name"],
        order["creator_username"],
    )

    if order["taker_id"]:
        text += f"\n🚖 {driver_label}: " + contact_identity(
            order["taker_id"],
            order["taker_name"],
            order["taker_username"],
        )

    return text


# ==================== ACCESS CONTROL ====================

async def enforce_driver_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    chat = update.effective_chat
    query = update.callback_query

    if not user:
        return

    # Проверяем личные сообщения и все нажатия кнопок.
    if not query and (not chat or chat.type != "private"):
        return

    try:
        member = await context.bot.get_chat_member(
            DRIVERS_GROUP_ID,
            user.id,
        )

        blocked = member.status == "kicked" or (
            member.status == "restricted"
            and (
                not member.is_member
                or not member.can_send_messages
            )
        )

        if not blocked:
            return

        user_state.pop(user.id, None)

        message = (
            "⛔ Μόνο προβολή. Η πρόσβαση στις παραγγελίες "
            "έχει περιοριστεί από τον διαχειριστή.\n"
            "View only. Order actions are disabled by the administrator."
        )

    except Exception:
        logger.exception(
            "Could not check bot access for user %s",
            user.id,
        )

        # Если права проверить не удалось, действие не выполняем.
        message = (
            "⚠️ Δεν μπορώ να ελέγξω την πρόσβαση. "
            "Δοκίμασε ξανά αργότερα.\n"
            "Cannot verify access. Please try again later."
        )

    try:
        if query:
            await query.answer(message, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(message)

    except Exception:
        logger.exception(
            "Could not send access notice to user %s",
            user.id,
        )

    raise ApplicationHandlerStop


# ==================== KEYBOARDS ====================

def main_keyboard(role):
    if role == "driver":
        return ReplyKeyboardMarkup(
            [
                [BTN_NEW_DRIVER],
                [BTN_DRIVER_ORDERS],
                [BTN_TAKEN],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    return ReplyKeyboardMarkup(
        [
            [BTN_NEW_CLIENT],
            [BTN_CLIENT_ORDERS],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_keyboard(role):
    button = (
        BTN_CANCEL_CLIENT
        if role == "client"
        else BTN_CANCEL_DRIVER
    )

    return ReplyKeyboardMarkup(
        [[button]],
        resize_keyboard=True,
        is_persistent=True,
    )


def driver_group_keyboard():
    return ReplyKeyboardMarkup(
        [[BTN_NEW_DRIVER]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Νέα διαδρομή...",
    )


def client_group_keyboard():
    return ReplyKeyboardMarkup(
        [[BTN_NEW_CLIENT]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Request a taxi...",
    )


def open_order_keyboard(order_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
                callback_data=f"take:{order_id}",
            )
        ]]
    )


def management_rows(order, role):
    english = role == "client"
    order_id = order["id"]

    rows = [[
        InlineKeyboardButton(
            "✏️ CHANGE TRIP DETAILS"
            if english
            else "✏️ ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ",
            callback_data=f"edit_details:{order_id}",
        )
    ]]

    if order["creator_role"] == "client":
        rows.append([
            InlineKeyboardButton(
                "💶 CHANGE PRICE"
                if english
                else "💶 ΑΛΛΑΓΗ ΤΙΜΗΣ",
                callback_data=f"edit_price:{order_id}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "❌ CANCEL REQUEST"
            if english
            else "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
            callback_data=f"cancel_order:{order_id}",
        )
    ])

    return rows


def creator_manage_keyboard(order):
    return InlineKeyboardMarkup(
        management_rows(order, order["creator_role"])
    )


def accepted_driver_keyboard(order):
    label = (
        "💬 ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΠΕΛΑΤΗ"
        if order["creator_role"] == "client"
        else "💬 ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΟΔΗΓΟ"
    )

    rows = [[
        InlineKeyboardButton(
            label,
            url=contact_url(
                order["creator_id"],
                order["creator_username"],
            ),
        )
    ]]

    rows.extend(management_rows(order, "driver"))

    rows.append([
        InlineKeyboardButton(
            "↩️ ΑΦΗΣΕ ΤΗ ΔΙΑΔΡΟΜΗ",
            callback_data=f"giveup:{order['id']}",
        )
    ])

    return InlineKeyboardMarkup(rows)


def accepted_creator_keyboard(order):
    rows = []

    if order["taker_id"]:
        rows.append([
            InlineKeyboardButton(
                "💬 CONTACT DRIVER"
                if order["creator_role"] == "client"
                else "💬 ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΟΔΗΓΟ",
                url=contact_url(
                    order["taker_id"],
                    order["taker_username"],
                ),
            )
        ])

    rows.extend(
        management_rows(order, order["creator_role"])
    )

    return InlineKeyboardMarkup(rows)


def confirm_cancel_keyboard(order, user_id):
    english = participant_role(order, user_id) == "client"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ YES, CANCEL REQUEST"
                    if english
                    else "✅ ΝΑΙ, ΑΚΥΡΩΣΗ",
                    callback_data=f"confirm_cancel:{order['id']}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ BACK" if english else "⬅️ ΠΙΣΩ",
                    callback_data=f"manage:{order['id']}",
                )
            ],
        ]
    )


def confirm_giveup_keyboard(order_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ ΝΑΙ, ΑΦΗΣΕ ΤΗ",
                    callback_data=f"confirm_giveup:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ ΠΙΣΩ",
                    callback_data=f"taken:{order_id}",
                )
            ],
        ]
    )


# ==================== ORDER CARDS ====================

def order_card(order):
    def telegram_contact(user_id, name, username):
        url = html.escape(
            contact_url(user_id, username),
            quote=True,
        )

        label = html.escape(name or "Telegram user")

        if username:
            label += " — @" + html.escape(username)

        return f'<a href="{url}">{label}</a>'

    creator = telegram_contact(
        order["creator_id"],
        order["creator_name"],
        order["creator_username"],
    )

    order_id = order["id"]

    if order["creator_role"] == "client":
        details = html.escape(order["details"])

        price = (
            "€" + html.escape(order["price"])
            if order["price"]
            else "🤝 Συζητήσιμη"
        )

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ</b>\n\n"
            f"{details}\n\n"
            f"💶 Προσφορά: <b>{price}</b>\n"
            f"👤 Πελάτης: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    elif is_voice_order(order):
        text = (
            "🎤 <b>ΝΕΑ ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "🔊 Άκουσε το φωνητικό μήνυμα ακριβώς από πάνω.\n\n"
            f"👤 Από οδηγό: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

        if order["details"] != "VOICE ORDER":
            text += "\n\n" + html.escape(order["details"])

    else:
        details = html.escape(order["details"])

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΟΔΗΓΟΣ</b>\n\n"
            f"{details}\n\n"
            f"👤 Από οδηγό: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    if order["status"] == "taken":
        taker = telegram_contact(
            order["taker_id"],
            order["taker_name"],
            order["taker_username"],
        )

        text += (
            "\n\n✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n"
            f"🚖 Την πήρε: {taker}"
        )

    elif order["status"] == "cancelled":
        text += "\n\n❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>"

    return text


def private_order_card(order):
    order_id = order["id"]

    if order["creator_role"] == "client":
        details = html.escape(order["details"])

        price = (
            "€" + html.escape(order["price"])
            if order["price"]
            else "Negotiable"
        )

        if order["status"] == "open":
            status = "🔎 Looking for a driver"
        elif order["status"] == "taken":
            status = "✅ Driver found"
        else:
            status = "❌ Cancelled"

        text = (
            "🚕 <b>YOUR TAXI REQUEST</b>\n\n"
            f"{details}\n\n"
            f"💶 Your offer: <b>{price}</b>\n"
            f"🔢 Request: #{order_id}\n"
            f"{status}"
        )

        if (
            order["status"] == "taken"
            and order["taker_name"]
        ):
            text += (
                "\n🚖 Driver: "
                f"<b>{html.escape(order['taker_name'])}</b>"
            )

        if order["status"] == "taken":
            text += "\n\n" + contacts_card(order, english=True)

        if (
            is_voice_order(order)
            and order["details"] != "VOICE ORDER"
        ):
            text += "\n\n" + html.escape(order["details"])

        return text

    if is_voice_order(order):
        if order["status"] == "open":
            status = "🟢 ΔΙΑΘΕΣΙΜΗ"
        elif order["status"] == "taken":
            status = "✅ ΔΟΘΗΚΕ"
        else:
            status = "❌ ΑΚΥΡΩΘΗΚΕ"

        text = (
            "🎤 <b>ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            f"🔢 Αριθμός: #{order_id}\n"
            f"📌 Κατάσταση: {status}"
        )

        if (
            order["status"] == "taken"
            and order["taker_name"]
        ):
            text += (
                "\n🚖 Οδηγός: "
                f"<b>{html.escape(order['taker_name'])}</b>"
            )

        if order["status"] == "taken":
            text += "\n\n" + contacts_card(order)

        if order["details"] != "VOICE ORDER":
            text += "\n\n" + html.escape(order["details"])

        return text

    return order_card(order)


async def refresh_group_card(context, order_id):
    order = get_order(order_id)

    if not order or not order["group_message_id"]:
        return

    markup = (
        open_order_keyboard(order_id)
        if order["status"] == "open"
        else None
    )

    try:
        await context.bot.edit_message_text(
            chat_id=order["group_chat_id"],
            message_id=order["group_message_id"],
            text=order_card(order),
            parse_mode="HTML",
            reply_markup=markup,
        )

    except Exception:
        logger.exception("Could not refresh group order")


# ==================== CREATE ORDERS ====================

async def create_text_order(
    context,
    user,
    role,
    details,
    price=None,
):
    async with db_lock:
        with db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders (
                    creator_id,
                    creator_name,
                    creator_username,
                    creator_role,
                    details,
                    price,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    user.id,
                    person_name(user),
                    user.username,
                    role,
                    details,
                    price,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            order_id = cursor.lastrowid
            conn.commit()

    order = get_order(order_id)

    message = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=order_card(order),
        parse_mode="HTML",
        reply_markup=open_order_keyboard(order_id),
        disable_notification=False,
    )

    async with db_lock:
        with db() as conn:
            conn.execute(
                """
                UPDATE orders
                SET group_chat_id = ?,
                    group_message_id = ?
                WHERE id = ?
                """,
                (
                    DRIVERS_GROUP_ID,
                    message.message_id,
                    order_id,
                ),
            )
            conn.commit()

    return order_id


async def create_voice_order(context, user, voice_file_id):
    async with db_lock:
        with db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders (
                    creator_id,
                    creator_name,
                    creator_username,
                    creator_role,
                    details,
                    price,
                    status,
                    voice_file_id,
                    created_at
                )
                VALUES (
                    ?, ?, ?, 'driver', ?, NULL, 'open', ?, ?
                )
                """,
                (
                    user.id,
                    person_name(user),
                    user.username,
                    "VOICE ORDER",
                    voice_file_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            order_id = cursor.lastrowid
            conn.commit()

    voice_message = await context.bot.send_voice(
        chat_id=DRIVERS_GROUP_ID,
        voice=voice_file_id,
        caption=(
            f"🎤 Φωνητική διαδρομή #{order_id}\n"
            f"👤 Από: {person_name(user)}"
        ),
        disable_notification=False,
    )

    order = get_order(order_id)

    card_message = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=order_card(order),
        parse_mode="HTML",
        reply_markup=open_order_keyboard(order_id),
        reply_to_message_id=voice_message.message_id,
        disable_notification=True,
    )

    async with db_lock:
        with db() as conn:
            conn.execute(
                """
                UPDATE orders
                SET group_chat_id = ?,
                    group_message_id = ?,
                    voice_group_message_id = ?
                WHERE id = ?
                """,
                (
                    DRIVERS_GROUP_ID,
                    card_message.message_id,
                    voice_message.message_id,
                    order_id,
                ),
            )
            conn.commit()

    return order_id


async def begin_new_order(update, context, role):
    user = update.effective_user

    user_state[user.id] = {
        "action": "new_details",
        "role": role,
    }

    if role == "driver":
        text = (
            "🚕 <b>ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Μπορείς να στείλεις τη διαδρομή με "
            "<b>2 τρόπους:</b>\n\n"
            "⌨️ <b>ΚΕΙΜΕΝΟ</b>\n"
            "Γράψε όλα τα στοιχεία σε ένα μήνυμα.\n\n"
            "ή\n\n"
            "🎤 <b>ΦΩΝΗΤΙΚΟ ΜΗΝΥΜΑ</b>\n"
            "Πάτησε το μικρόφωνο και πες τη διαδρομή.\n\n"
            "Το φωνητικό θα σταλεί όπως είναι "
            "στην ομάδα οδηγών.\n\n"
            "👇 Στείλε τώρα κείμενο ή φωνητικό."
        )
    else:
        text = (
            "🚕 <b>REQUEST A TAXI</b>\n\n"
            "Please send all your trip details "
            "in ONE text message.\n\n"
            "Example:\n\n"
            "Larnaca Airport → Limassol\n"
            "18:30\n"
            "2 passengers\n\n"
            "👇 Send your trip details now."
        )

    await context.bot.send_message(
        chat_id=user.id,
        text=text,
        parse_mode="HTML",
        reply_markup=cancel_keyboard(role),
        disable_notification=True,
    )


# ==================== COMMANDS ====================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_chat:
        return

    if update.effective_chat.type != "private":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        return

    payload = ""

    if context.args:
        payload = context.args[0].strip().lower()

    if payload == "driver":
        if not await is_driver(update.effective_user.id, context):
            await update.message.reply_text(
                "⛔ Η λειτουργία αυτή είναι μόνο για οδηγούς.",
                disable_notification=True,
            )
            return

        user_state.pop(update.effective_user.id, None)
        await begin_new_order(update, context, "driver")
        return

    if payload == "client":
        user_state.pop(update.effective_user.id, None)
        await begin_new_order(update, context, "client")
        return

    role = await role_for(update.effective_user.id, context)
    user_state.pop(update.effective_user.id, None)

    if role == "driver":
        text = (
            "🚕 <b>ΜΕΝΟΥ ΟΔΗΓΟΥ</b>\n\n"
            "Για να δώσεις νέα διαδρομή πάτησε:\n\n"
            "<b>🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Μπορείς να τη στείλεις με κείμενο "
            "ή 🎤 φωνητικό."
        )
    else:
        text = (
            "🚕 <b>CYPRUS TAXI</b>\n\n"
            "Need a taxi in Cyprus?\n\n"
            "👇 Tap <b>🚕 REQUEST A TAXI</b>\n"
            "and send us your trip details."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(role),
        disable_notification=True,
    )


async def panel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in (DRIVERS_GROUP_ID, CLIENTS_GROUP_ID):
        return

    if not await is_admin_in_chat(chat_id, user_id, context):
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        return

    try:
        await update.effective_message.delete()
    except Exception:
        pass

    if chat_id == DRIVERS_GROUP_ID:
        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
                "Για να δώσεις νέα διαδρομή, "
                "πάτησε το κουμπί στο κάτω μέρος.\n\n"
                "Μπορείς να στείλεις κείμενο "
                "ή 🎤 φωνητικό."
            ),
            parse_mode="HTML",
            reply_markup=driver_group_keyboard(),
            disable_notification=True,
        )
        return

    welcome_message = await context.bot.send_message(
        chat_id=CLIENTS_GROUP_ID,
        text=(
            "🚕 <b>CYPRUS TAXI</b>\n\n"
            "Need a taxi in Cyprus?\n\n"
            "✈️ Airport transfers\n"
            "🏙 City rides\n"
            "🛣 Long-distance trips\n\n"
            "👇 <b>Tap the button below to request a taxi.</b>\n\n"
            "Your request will be sent privately "
            "to available drivers."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "🚕 OPEN BOT & REQUEST TAXI",
                    url=f"https://t.me/{BOT_USERNAME}?start=client",
                )
            ]]
        ),
        disable_notification=True,
    )

    try:
        await context.bot.pin_chat_message(
            chat_id=CLIENTS_GROUP_ID,
            message_id=welcome_message.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.info("Could not pin client welcome message")

    await context.bot.send_message(
        chat_id=CLIENTS_GROUP_ID,
        text=(
            "🚕 <b>REQUEST A TAXI</b>\n\n"
            "You can also use the permanent button below."
        ),
        parse_mode="HTML",
        reply_markup=client_group_keyboard(),
        disable_notification=True,
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_chat:
        await update.effective_message.reply_text(
            f"Chat ID: {update.effective_chat.id}"
        )


# ==================== ORDER LISTS ====================

async def show_my_orders(update, context, user_id, role):
    orders = get_creator_active_orders(user_id)

    if not orders:
        if role == "driver":
            text = (
                "📋 <b>ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ</b>\n\n"
                "Δεν έχεις ενεργές διαδρομές."
            )
        else:
            text = (
                "📋 <b>MY ORDERS</b>\n\n"
                "You have no active taxi requests."
            )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(role),
            disable_notification=True,
        )
        return

    buttons = []

    for order in orders:
        status = "✅" if order["status"] == "taken" else "🟢"

        if is_voice_order(order):
            short_details = "🎤 Φωνητική διαδρομή"
        else:
            short_details = (
                order["details"].replace("\n", " ").strip()
            )
            if len(short_details) > 30:
                short_details = short_details[:30] + "…"

        buttons.append([
            InlineKeyboardButton(
                f"{status} #{order['id']} — {short_details}",
                callback_data=f"manage:{order['id']}",
            )
        ])

    if role == "driver":
        title = (
            "📋 <b>ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ</b>\n\n"
            "👇 Διάλεξε τη διαδρομή:"
        )
    else:
        title = (
            "📋 <b>MY ORDERS</b>\n\n"
            "👇 Select the request you want to manage:"
        )

    await update.message.reply_text(
        title,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_notification=True,
    )


async def show_taken_orders(update, context, user_id):
    orders = get_taken_orders(user_id)

    if not orders:
        await update.message.reply_text(
            "📦 <b>ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ</b>\n\n"
            "Δεν έχεις πάρει κάποια ενεργή διαδρομή.",
            parse_mode="HTML",
            reply_markup=main_keyboard("driver"),
            disable_notification=True,
        )
        return

    buttons = []

    for order in orders:
        if is_voice_order(order):
            short_details = "🎤 Φωνητική διαδρομή"
        else:
            short_details = (
                order["details"].replace("\n", " ").strip()
            )
            if len(short_details) > 30:
                short_details = short_details[:30] + "…"

        buttons.append([
            InlineKeyboardButton(
                f"🚖 #{order['id']} — {short_details}",
                callback_data=f"taken:{order['id']}",
            )
        ])

    await update.message.reply_text(
        "📦 <b>ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ</b>\n\n"
        "👇 Διάλεξε διαδρομή:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_notification=True,
    )


# ==================== NOTIFICATIONS ====================

async def notify_order_change(
    context,
    order,
    actor_id,
    cancelled=False,
):
    recipients = {
        order["creator_id"],
        order["taker_id"],
    } - {None, actor_id}

    for recipient in recipients:
        english = participant_role(order, recipient) == "client"

        if cancelled:
            title = (
                "❌ REQUEST CANCELLED"
                if english
                else "❌ Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ"
            )
        else:
            title = (
                "⚠️ REQUEST UPDATED"
                if english
                else "⚠️ ΑΛΛΑΓΗ ΔΙΑΔΡΟΜΗΣ"
            )

        if recipient == order["creator_id"]:
            card = private_order_card(order)
        else:
            card = order_card(order)

        if cancelled:
            markup = None
        elif recipient == order["creator_id"]:
            markup = accepted_creator_keyboard(order)
        else:
            markup = accepted_driver_keyboard(order)

        try:
            await context.bot.send_message(
                chat_id=recipient,
                text=f"<b>{title}</b>\n\n{card}",
                parse_mode="HTML",
                reply_markup=markup,
                disable_notification=False,
            )
        except Exception:
            logger.exception(
                "Could not notify order participant %s",
                recipient,
            )


# ==================== PRIVATE TEXT ====================

async def private_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    text = (update.message.text or "").strip()
    state = user_state.get(user.id)

    if state and state.get("role"):
        role = state["role"]
    else:
        role = await role_for(user.id, context)

    if text in (
        BTN_CANCEL_DRIVER,
        BTN_CANCEL_CLIENT,
        OLD_CANCEL,
    ):
        state_role = state.get("role", role) if state else role
        user_state.pop(user.id, None)

        message = (
            "↩️ Request cancelled."
            if state_role == "client"
            else "↩️ Ακυρώθηκε."
        )

        await update.message.reply_text(
            message,
            reply_markup=main_keyboard(state_role),
            disable_notification=True,
        )
        return

    if text in (BTN_NEW_DRIVER, OLD_NEW_DRIVER):
        if not await is_driver(user.id, context):
            await update.message.reply_text(
                "⛔ Η λειτουργία αυτή είναι μόνο για οδηγούς.",
                disable_notification=True,
            )
            return

        await begin_new_order(update, context, "driver")
        return

    if text == BTN_NEW_CLIENT:
        await begin_new_order(update, context, "client")
        return

    if (
        text in (BTN_CLIENT_ORDERS, OLD_MY_ORDER, OLD_MY_ORDERS)
        and role == "client"
    ):
        await show_my_orders(
            update,
            context,
            user.id,
            "client",
        )
        return

    if (
        text in (BTN_DRIVER_ORDERS, OLD_MY_ORDER, OLD_MY_ORDERS)
        and role == "driver"
    ):
        await show_my_orders(
            update,
            context,
            user.id,
            "driver",
        )
        return

    if text in (BTN_TAKEN, OLD_TAKEN, OLD_TAKEN_ORDERS):
        if await is_driver(user.id, context):
            await show_taken_orders(update, context, user.id)
        return

    state = user_state.get(user.id)

    if not state:
        role = await role_for(user.id, context)

        message = (
            "👇 Χρησιμοποίησε τα κουμπιά παρακάτω."
            if role == "driver"
            else "👇 Please use the buttons below."
        )

        await update.message.reply_text(
            message,
            reply_markup=main_keyboard(role),
            disable_notification=True,
        )
        return

    action = state["action"]
    state_role = state.get("role", role)

    if action == "new_details":
        if len(text) < 3:
            if state_role == "client":
                message = "Please send your trip details."
            else:
                message = (
                    "Γράψε τα στοιχεία της διαδρομής "
                    "ή στείλε 🎤 φωνητικό."
                )

            await update.message.reply_text(
                message,
                disable_notification=True,
            )
            return

        state["details"] = text

        if state_role == "client":
            state["action"] = "new_price"

            await update.message.reply_text(
                "💶 <b>HOW MUCH ARE YOU WILLING TO PAY?</b>\n\n"
                "Enter your offer in EUR.\n"
                "Example: <b>50</b>\n\n"
                "If you are not sure, tap the button below.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "🤝 SKIP / NOT SURE",
                            callback_data="skip_new_price",
                        )
                    ]]
                ),
                disable_notification=True,
            )
        else:
            order_id = await create_text_order(
                context,
                user,
                "driver",
                text,
            )

            user_state.pop(user.id, None)

            await update.message.reply_text(
                "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΗΜΟΣΙΕΥΤΗΚΕ</b>\n\n"
                f"🔢 Αριθμός: #{order_id}\n\n"
                "Η διαδρομή εμφανίστηκε στην ομάδα οδηγών.",
                parse_mode="HTML",
                reply_markup=main_keyboard("driver"),
                disable_notification=True,
            )

        return

    if action == "new_price":
        cleaned = (
            text.replace("€", "").replace(",", ".").strip()
        )

        try:
            value = float(cleaned)
            if value <= 0:
                raise ValueError

        except ValueError:
            await update.message.reply_text(
                "Please enter a valid amount in EUR.\n"
                "Example: 50",
                disable_notification=True,
            )
            return

        price = f"{value:g}"

        order_id = await create_text_order(
            context,
            user,
            "client",
            state["details"],
            price,
        )

        user_state.pop(user.id, None)

        await update.message.reply_text(
            "🔎 <b>LOOKING FOR A DRIVER</b>\n\n"
            f"Request: #{order_id}\n"
            f"💶 Your offer: €{price}\n\n"
            "Your request has been sent to our drivers.\n\n"
            "You will be notified when a driver accepts your trip.",
            parse_mode="HTML",
            reply_markup=main_keyboard("client"),
            disable_notification=True,
        )
        return

    if action in ("edit_details", "edit_price"):
        order_id = state["order_id"]
        value = text

        if action == "edit_price":
            import math

            try:
                number = float(
                    text.replace("€", "")
                    .replace(",", ".")
                    .strip()
                )

                if not math.isfinite(number) or number <= 0:
                    raise ValueError

                value = f"{number:g}"

            except ValueError:
                await update.message.reply_text(
                    "Please enter a valid amount in EUR."
                    if state_role == "client"
                    else "Στείλε έγκυρο ποσό σε EUR."
                )
                return

        async with db_lock:
            with db() as conn:
                current = conn.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()

                allowed = (
                    can_manage_order(current, user.id)
                    and (
                        action != "edit_price"
                        or current["creator_role"] == "client"
                    )
                )

                if allowed:
                    column = (
                        "details"
                        if action == "edit_details"
                        else "price"
                    )

                    conn.execute(
                        f"UPDATE orders SET {column} = ? WHERE id = ?",
                        (value, order_id),
                    )

                    conn.commit()

                    updated = conn.execute(
                        "SELECT * FROM orders WHERE id = ?",
                        (order_id,),
                    ).fetchone()

        user_state.pop(user.id, None)

        if not allowed:
            await update.message.reply_text(
                "This request can no longer be edited."
                if state_role == "client"
                else "Η διαδρομή δεν μπορεί πλέον να αλλάξει.",
                reply_markup=main_keyboard(state_role),
            )
            return

        await refresh_group_card(context, order_id)
        await notify_order_change(context, updated, user.id)

        if (
            updated["taker_id"] == user.id
            and updated["creator_id"] != user.id
        ):
            markup = accepted_driver_keyboard(updated)
        elif updated["status"] == "taken":
            markup = accepted_creator_keyboard(updated)
        else:
            markup = creator_manage_keyboard(updated)

        await update.message.reply_text(
            "✅ Updated."
            if state_role == "client"
            else "✅ Η διαδρομή ενημερώθηκε.",
            reply_markup=main_keyboard(state_role),
        )

        card = (
            private_order_card(updated)
            if updated["creator_id"] == user.id
            else order_card(updated)
        )

        await update.message.reply_text(
            card,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return


# ==================== PRIVATE VOICE ====================

async def private_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    if not update.message.voice:
        return

    user = update.effective_user

    if not await is_driver(user.id, context):
        await update.message.reply_text(
            "Voice requests are not available for clients.",
            reply_markup=main_keyboard("client"),
            disable_notification=True,
        )
        return

    state = user_state.get(user.id)

    if (
        not state
        or state.get("action") != "new_details"
        or state.get("role") != "driver"
    ):
        await update.message.reply_text(
            "🎤 Για να στείλεις φωνητική διαδρομή, "
            "πάτησε πρώτα:\n\n"
            "<b>🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard("driver"),
            disable_notification=True,
        )
        return

    try:
        order_id = await create_voice_order(
            context,
            user,
            update.message.voice.file_id,
        )

    except Exception:
        logger.exception("Could not create voice order")

        await update.message.reply_text(
            "❌ Δεν μπόρεσα να δημοσιεύσω το φωνητικό. "
            "Δοκίμασε ξανά.",
            disable_notification=True,
        )
        return

    user_state.pop(user.id, None)

    await update.message.reply_text(
        "✅ <b>Η ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ ΔΗΜΟΣΙΕΥΤΗΚΕ</b>\n\n"
        f"🎤 Αριθμός: #{order_id}\n\n"
        "Το φωνητικό εμφανίστηκε στην ομάδα οδηγών.",
        parse_mode="HTML",
        reply_markup=main_keyboard("driver"),
        disable_notification=True,
    )


# ==================== INLINE BUTTONS ====================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not update.effective_user:
        return

    user = update.effective_user
    data = query.data or ""

    if data == "skip_new_price":
        state = user_state.get(user.id)

        if (
            not state
            or state.get("action") != "new_price"
            or state.get("role") != "client"
        ):
            await query.answer(
                "This step has expired.",
                show_alert=True,
            )
            return

        await query.answer()

        order_id = await create_text_order(
            context,
            user,
            "client",
            state["details"],
            None,
        )

        user_state.pop(user.id, None)

        await query.edit_message_text(
            "🔎 <b>LOOKING FOR A DRIVER</b>\n\n"
            f"Request: #{order_id}\n"
            "💶 Your offer: Negotiable\n\n"
            "Your request has been sent to our drivers.\n\n"
            "You will be notified when a driver accepts your trip.",
            parse_mode="HTML",
        )

        await context.bot.send_message(
            chat_id=user.id,
            text="👇 You can use the buttons below.",
            reply_markup=main_keyboard("client"),
            disable_notification=True,
        )
        return

    parts = data.split(":", 1)

    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer()
        return

    action = parts[0]
    order_id = int(parts[1])
    order = get_order(order_id)

    if not order:
        await query.answer(
            "Order not found.",
            show_alert=True,
        )
        return

    # Водитель принимает заказ.
    if action == "take":
        if not await is_driver(user.id, context):
            await query.answer(
                "Μόνο για οδηγούς.",
                show_alert=True,
            )
            return

        if (
            order["creator_role"] == "driver"
            and order["creator_id"] == user.id
        ):
            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )
            return

        async with db_lock:
            with db() as conn:
                current = conn.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()

                if not current or current["status"] != "open":
                    await query.answer(
                        "Η διαδρομή δεν είναι πλέον διαθέσιμη.",
                        show_alert=True,
                    )
                    return

                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET status = 'taken',
                        taker_id = ?,
                        taker_name = ?,
                        taker_username = ?
                    WHERE id = ?
                      AND status = 'open'
                    """,
                    (
                        user.id,
                        person_name(user),
                        user.username,
                        order_id,
                    ),
                )

                if cursor.rowcount != 1:
                    conn.rollback()

                    await query.answer(
                        "Την πήρε ήδη άλλος οδηγός.",
                        show_alert=True,
                    )
                    return

                conn.commit()

        await query.answer("✅ Πήρες τη διαδρομή!")
        await refresh_group_card(context, order_id)

        updated = get_order(order_id)

        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "✅ <b>ΠΗΡΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
                    + order_card(updated)
                ),
                parse_mode="HTML",
                reply_markup=accepted_driver_keyboard(updated),
                disable_notification=True,
            )

        except Exception:
            logger.exception("Could not message taker")

        try:
            await context.bot.send_message(
                chat_id=updated["creator_id"],
                text=private_order_card(updated),
                parse_mode="HTML",
                reply_markup=accepted_creator_keyboard(updated),
                disable_notification=True,
            )

        except Exception:
            logger.exception("Could not message creator")

        return

    # Управление заказом для обеих сторон.
    if action in {
        "manage",
        "edit_details",
        "edit_price",
        "cancel_order",
        "confirm_cancel",
    }:
        if not can_manage_order(order, user.id):
            await query.answer(
                "This order is closed or is not yours.",
                show_alert=True,
            )
            return

        role = participant_role(order, user.id)
        english = role == "client"

        if action == "manage":
            await query.answer()

            if user.id != order["creator_id"]:
                markup = accepted_driver_keyboard(order)
            elif order["status"] == "taken":
                markup = accepted_creator_keyboard(order)
            else:
                markup = creator_manage_keyboard(order)

            card = (
                private_order_card(order)
                if user.id == order["creator_id"]
                else order_card(order)
            )

            await query.edit_message_text(
                card,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if action in ("edit_details", "edit_price"):
            if (
                action == "edit_price"
                and order["creator_role"] != "client"
            ):
                await query.answer(
                    "The price cannot be changed.",
                    show_alert=True,
                )
                return

            user_state[user.id] = {
                "action": action,
                "order_id": order_id,
                "role": role,
            }

            await query.answer()

            if action == "edit_price":
                prompt = (
                    "Send the new price in EUR. Example: 50"
                    if english
                    else "Στείλε τη νέα τιμή σε EUR. Παράδειγμα: 50"
                )
            else:
                prompt = (
                    "Send the new trip details in one message."
                    if english
                    else "Στείλε τα νέα στοιχεία σε ένα μήνυμα."
                )

                if is_voice_order(order):
                    prompt += (
                        "\nΤο αρχικό φωνητικό παραμένει. "
                        "Αλλάζει μόνο η περιγραφή."
                    )

            await context.bot.send_message(
                chat_id=user.id,
                text=prompt,
                reply_markup=cancel_keyboard(role),
            )
            return

        if action == "cancel_order":
            await query.answer()

            await query.edit_message_reply_markup(
                reply_markup=confirm_cancel_keyboard(
                    order,
                    user.id,
                )
            )
            return

        if action == "confirm_cancel":
            async with db_lock:
                with db() as conn:
                    current = conn.execute(
                        "SELECT * FROM orders WHERE id = ?",
                        (order_id,),
                    ).fetchone()

                    allowed = can_manage_order(current, user.id)

                    if allowed:
                        conn.execute(
                            """
                            UPDATE orders
                            SET status = 'cancelled'
                            WHERE id = ?
                            """,
                            (order_id,),
                        )

                        conn.commit()

                        updated = conn.execute(
                            "SELECT * FROM orders WHERE id = ?",
                            (order_id,),
                        ).fetchone()

            if not allowed:
                await query.answer(
                    "The order status has changed.",
                    show_alert=True,
                )
                return

            await query.answer(
                "Request cancelled."
                if english
                else "Η διαδρομή ακυρώθηκε."
            )

            await refresh_group_card(context, order_id)

            await query.edit_message_text(
                (
                    "❌ REQUEST CANCELLED"
                    if english
                    else "❌ Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ"
                )
                + f"\n#{order_id}"
            )

            await notify_order_change(
                context,
                updated,
                user.id,
                cancelled=True,
            )
            return

    # Действия только для принявшего водителя.
    if action in ("taken", "giveup", "confirm_giveup"):
        if (
            order["status"] != "taken"
            or order["taker_id"] != user.id
        ):
            await query.answer(
                "Η διαδρομή δεν είναι πλέον δική σου.",
                show_alert=True,
            )
            return

    if action == "taken":
        await query.answer()

        await query.edit_message_text(
            "📦 <b>ΔΙΑΔΡΟΜΗ ΠΟΥ ΠΗΡΑ</b>\n\n"
            + order_card(order),
            parse_mode="HTML",
            reply_markup=accepted_driver_keyboard(order),
        )
        return

    if action == "giveup":
        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=confirm_giveup_keyboard(order_id)
        )
        return

    if action == "confirm_giveup":
        async with db_lock:
            with db() as conn:
                current = conn.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()

                if (
                    not current
                    or current["status"] != "taken"
                    or current["taker_id"] != user.id
                ):
                    await query.answer(
                        "Η κατάσταση άλλαξε.",
                        show_alert=True,
                    )
                    return

                conn.execute(
                    """
                    UPDATE orders
                    SET status = 'open',
                        taker_id = NULL,
                        taker_name = NULL,
                        taker_username = NULL
                    WHERE id = ?
                    """,
                    (order_id,),
                )

                conn.commit()

        await query.answer("Η διαδρομή επέστρεψε.")
        await refresh_group_card(context, order_id)

        await query.edit_message_text(
            "↩️ <b>ΑΦΗΣΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            f"Η διαδρομή #{order_id} είναι ξανά διαθέσιμη.",
            parse_mode="HTML",
        )

        if order["creator_role"] == "client":
            creator_text = (
                "🔎 <b>LOOKING FOR ANOTHER DRIVER</b>\n\n"
                f"The driver released your request #{order_id}.\n\n"
                "Your request is available to our drivers again."
            )
        else:
            creator_text = (
                "↩️ <b>Η ΔΙΑΔΡΟΜΗ ΕΙΝΑΙ ΞΑΝΑ ΔΙΑΘΕΣΙΜΗ</b>\n\n"
                f"Ο οδηγός άφησε τη διαδρομή #{order_id}.\n\n"
                "Η διαδρομή είναι ξανά διαθέσιμη στους οδηγούς."
            )

        try:
            await context.bot.send_message(
                chat_id=order["creator_id"],
                text=creator_text,
                parse_mode="HTML",
                disable_notification=True,
            )

        except Exception:
            logger.exception("Could not notify creator")

        return

    await query.answer()


# ==================== GROUP MESSAGES ====================

async def driver_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    text = (update.message.text or "").strip()

    if text in (BTN_NEW_DRIVER, OLD_NEW_DRIVER):
        try:
            await update.message.delete()
        except Exception:
            pass

        if not await is_driver(user.id, context):
            return

        try:
            await begin_new_order(update, context, "driver")

        except Exception:
            user_state.pop(user.id, None)
            logger.exception(
                "Could not start private driver flow"
            )

        return

    if await is_admin_in_chat(
        DRIVERS_GROUP_ID,
        user.id,
        context,
    ):
        return

    try:
        await update.message.delete()
    except Exception:
        pass


async def client_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    text = (update.message.text or "").strip()

    if text == BTN_NEW_CLIENT:
        try:
            await update.message.delete()
        except Exception:
            pass

        try:
            await begin_new_order(update, context, "client")

        except Exception:
            user_state.pop(user.id, None)
            logger.info(
                "Client must start bot privately first: %s",
                user.id,
            )

        return

    if await is_admin_in_chat(
        CLIENTS_GROUP_ID,
        user.id,
        context,
    ):
        return

    try:
        await update.message.delete()
    except Exception:
        pass


async def group_commands_cleanup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id

    if chat_id not in (DRIVERS_GROUP_ID, CLIENTS_GROUP_ID):
        return

    if await is_admin_in_chat(
        chat_id,
        update.effective_user.id,
        context,
    ):
        return

    try:
        await update.effective_message.delete()
    except Exception:
        pass


# ==================== ERRORS ====================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled exception",
        exc_info=context.error,
    )


# ==================== START BOT ====================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # Проверка доступа выполняется раньше остальных обработчиков.
    app.add_handler(
        TypeHandler(Update, enforce_driver_access),
        group=-1,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("id", id_command))

    app.add_handler(CallbackQueryHandler(callbacks))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.VOICE,
            private_voice,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Chat(DRIVERS_GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            driver_group_text,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Chat(CLIENTS_GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            client_group_text,
        )
    )

    app.add_handler(
        MessageHandler(
            (
                filters.Chat(DRIVERS_GROUP_ID)
                | filters.Chat(CLIENTS_GROUP_ID)
            )
            & filters.COMMAND,
            group_commands_cleanup,
        )
    )

    app.add_error_handler(error_handler)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
