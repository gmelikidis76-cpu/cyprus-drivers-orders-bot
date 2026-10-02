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
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

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

# Only temporary conversation steps are kept here.
# Orders themselves are stored permanently in SQLite.
user_state = {}


# =========================================================
# BUTTONS
# =========================================================

# DRIVER — GREEK
BTN_NEW_DRIVER = "🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_DRIVER_ORDERS = "📋 ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ"
BTN_TAKEN = "📦 ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ"
BTN_CANCEL_DRIVER = "❌ ΑΚΥΡΩΣΗ"

# CLIENT — ENGLISH
BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"
BTN_CLIENT_ORDERS = "📋 MY ORDERS"
BTN_CANCEL_CLIENT = "❌ CANCEL"

# Old buttons kept for compatibility
OLD_NEW_DRIVER = "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ"
OLD_MY_ORDER = "📋 MY ORDER"
OLD_MY_ORDERS = "📋 MY ORDERS"
OLD_TAKEN = "📦 MY TAKEN ORDER"
OLD_TAKEN_ORDERS = "📦 MY TAKEN ORDERS"
OLD_CANCEL = "❌ CANCEL"


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table_name, column_name):
    rows = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return any(
        row["name"] == column_name
        for row in rows
    )


def init_db():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(
            directory,
            exist_ok=True,
        )

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

        # -------------------------------------------------
        # New columns for driver voice orders.
        # Existing database/orders remain untouched.
        # -------------------------------------------------

        if not column_exists(
            conn,
            "orders",
            "voice_file_id",
        ):
            conn.execute(
                """
                ALTER TABLE orders
                ADD COLUMN voice_file_id TEXT
                """
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
            """
            SELECT *
            FROM orders
            WHERE id = ?
            """,
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


# =========================================================
# HELPERS
# =========================================================

def person_name(user):
    return (
        user.full_name
        or user.first_name
        or "Telegram user"
    )


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

        return member.status not in (
            "left",
            "kicked",
        )

    except Exception:
        logger.exception(
            "Could not check driver membership"
        )
        return False


async def is_admin_in_chat(
    chat_id,
    user_id,
    context,
):
    try:
        member = await context.bot.get_chat_member(
            chat_id,
            user_id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception:
        return False


async def role_for(user_id, context):
    if await is_driver(
        user_id,
        context,
    ):
        return "driver"

    return "client"


# =========================================================
# PRIVATE KEYBOARDS
# =========================================================

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
    if role == "client":
        button = BTN_CANCEL_CLIENT
    else:
        button = BTN_CANCEL_DRIVER

    return ReplyKeyboardMarkup(
        [[button]],
        resize_keyboard=True,
        is_persistent=True,
    )


# =========================================================
# GROUP KEYBOARDS
# =========================================================

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


# =========================================================
# ORDER BUTTONS
# =========================================================

def open_order_keyboard(order_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
                    callback_data=f"take:{order_id}",
                )
            ]
        ]
    )


def creator_manage_keyboard(order):
    order_id = order["id"]

    # CLIENT — ENGLISH
    if order["creator_role"] == "client":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ CHANGE TRIP DETAILS",
                        callback_data=f"edit_details:{order_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💶 CHANGE PRICE",
                        callback_data=f"edit_price:{order_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ CANCEL REQUEST",
                        callback_data=f"cancel_order:{order_id}",
                    )
                ],
            ]
        )

    # DRIVER VOICE ORDER
    # Voice itself cannot be edited like text.
    if is_voice_order(order):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
                        callback_data=f"cancel_order:{order_id}",
                    )
                ]
            ]
        )

    # DRIVER TEXT ORDER
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ",
                    callback_data=f"edit_details:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
                    callback_data=f"cancel_order:{order_id}",
                )
            ],
        ]
    )


def accepted_driver_keyboard(order):
    if order["creator_role"] == "client":
        contact_label = "💬 ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΠΕΛΑΤΗ"
    else:
        contact_label = "💬 ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΟΔΗΓΟ"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    contact_label,
                    url=contact_url(
                        order["creator_id"],
                        order["creator_username"],
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "↩️ ΑΦΗΣΕ ΤΗ ΔΙΑΔΡΟΜΗ",
                    callback_data=f"giveup:{order['id']}",
                )
            ],
        ]
    )


def accepted_creator_keyboard(order):
    order_id = order["id"]
    rows = []

    # CLIENT — ENGLISH
    if order["creator_role"] == "client":

        if order["taker_id"]:
            rows.append(
                [
                    InlineKeyboardButton(
                        "💬 CONTACT DRIVER",
                        url=contact_url(
                            order["taker_id"],
                            order["taker_username"],
                        ),
                    )
                ]
            )

        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ CHANGE TRIP DETAILS",
                    callback_data=f"edit_details:{order_id}",
                )
            ]
        )

        rows.append(
            [
                InlineKeyboardButton(
                    "💶 CHANGE PRICE",
                    callback_data=f"edit_price:{order_id}",
                )
            ]
        )

        rows.append(
            [
                InlineKeyboardButton(
                    "❌ CANCEL REQUEST",
                    callback_data=f"cancel_order:{order_id}",
                )
            ]
        )

        return InlineKeyboardMarkup(rows)

    # DRIVER — GREEK
    if order["taker_id"]:
        rows.append(
            [
                InlineKeyboardButton(
                    "💬 ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΟΔΗΓΟ",
                    url=contact_url(
                        order["taker_id"],
                        order["taker_username"],
                    ),
                )
            ]
        )

    if not is_voice_order(order):
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ",
                    callback_data=f"edit_details:{order_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
                callback_data=f"cancel_order:{order_id}",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def confirm_cancel_keyboard(order):
    order_id = order["id"]

    if order["creator_role"] == "client":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ YES, CANCEL REQUEST",
                        callback_data=f"confirm_cancel:{order_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ BACK",
                        callback_data=f"manage:{order_id}",
                    )
                ],
            ]
        )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ ΝΑΙ, ΑΚΥΡΩΣΗ",
                    callback_data=f"confirm_cancel:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ ΠΙΣΩ",
                    callback_data=f"manage:{order_id}",
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


# =========================================================
# GROUP ORDER CARD — ALWAYS GREEK FOR DRIVERS
# =========================================================

def order_card(order):
    creator = html.escape(
        order["creator_name"]
    )

    order_id = order["id"]

    # CLIENT ORDER
    if order["creator_role"] == "client":

        details = html.escape(
            order["details"]
        )

        if order["price"]:
            price_line = (
                f"€{html.escape(order['price'])}"
            )
        else:
            price_line = "🤝 Συζητήσιμη"

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ</b>\n\n"
            f"{details}\n\n"
            f"💶 Προσφορά: <b>{price_line}</b>\n"
            f"👤 Πελάτης: <b>{creator}</b>\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    # DRIVER VOICE ORDER
    elif is_voice_order(order):

        text = (
            "🎤 <b>ΝΕΑ ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "🔊 Άκουσε το φωνητικό μήνυμα ακριβώς από πάνω.\n\n"
            f"👤 Από οδηγό: <b>{creator}</b>\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    # DRIVER TEXT ORDER
    else:

        details = html.escape(
            order["details"]
        )

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΟΔΗΓΟΣ</b>\n\n"
            f"{details}\n\n"
            f"👤 Από οδηγό: <b>{creator}</b>\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    if order["status"] == "taken":

        taker = html.escape(
            order["taker_name"] or "Driver"
        )

        text += (
            "\n\n"
            "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n"
            f"🚖 Την πήρε: <b>{taker}</b>"
        )

    elif order["status"] == "cancelled":

        text += (
            "\n\n"
            "❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>"
        )

    return text


# =========================================================
# PRIVATE ORDER CARD
# =========================================================

def private_order_card(order):
    order_id = order["id"]

    # CLIENT — ENGLISH
    if order["creator_role"] == "client":

        details = html.escape(
            order["details"]
        )

        if order["price"]:
            price = (
                f"€{html.escape(order['price'])}"
            )
        else:
            price = "Negotiable"

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
                "\n"
                f"🚖 Driver: "
                f"<b>{html.escape(order['taker_name'])}</b>"
            )

        return text

    # DRIVER VOICE
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
                "\n"
                f"🚖 Οδηγός: "
                f"<b>{html.escape(order['taker_name'])}</b>"
            )

        return text

    # DRIVER TEXT
    return order_card(order)


# =========================================================
# REFRESH EXISTING GROUP CARD
# =========================================================

async def refresh_group_card(
    context,
    order_id,
):
    order = get_order(
        order_id
    )

    if not order:
        return

    if not order["group_message_id"]:
        return

    if order["status"] == "open":
        markup = open_order_keyboard(
            order_id
        )
    else:
        markup = None

    try:
        await context.bot.edit_message_text(
            chat_id=order["group_chat_id"],
            message_id=order["group_message_id"],
            text=order_card(order),
            parse_mode="HTML",
            reply_markup=markup,
        )

    except Exception:
        logger.exception(
            "Could not refresh group order"
        )


# =========================================================
# CREATE TEXT ORDER
# =========================================================

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
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                ),
            )

            order_id = cursor.lastrowid

            conn.commit()

    order = get_order(
        order_id
    )

    # ONLY completed order creates a new notification
    # in driver group.
    message = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=order_card(order),
        parse_mode="HTML",
        reply_markup=open_order_keyboard(
            order_id
        ),
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


# =========================================================
# CREATE DRIVER VOICE ORDER
# =========================================================

async def create_voice_order(
    context,
    user,
    voice_file_id,
):
    # Store order first.
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
                VALUES (?, ?, ?, 'driver', ?, NULL, 'open', ?, ?)
                """,
                (
                    user.id,
                    person_name(user),
                    user.username,
                    "VOICE ORDER",
                    voice_file_id,
                    datetime.now(
                        timezone.utc
                    ).isoformat(),
                ),
            )

            order_id = cursor.lastrowid

            conn.commit()

    # -----------------------------------------------------
    # 1. Original voice appears in driver group.
    # It is NOT converted to text.
    # -----------------------------------------------------

    voice_message = await context.bot.send_voice(
        chat_id=DRIVERS_GROUP_ID,
        voice=voice_file_id,
        caption=(
            f"🎤 Φωνητική διαδρομή #{order_id}\n"
            f"👤 Από: {person_name(user)}"
        ),
        disable_notification=False,
    )

    order = get_order(
        order_id
    )

    # -----------------------------------------------------
    # 2. Immediately after voice: order card + TAKE button.
    #
    # Card is sent silently because the voice itself already
    # produced the order notification.
    # -----------------------------------------------------

    card_message = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=order_card(order),
        parse_mode="HTML",
        reply_markup=open_order_keyboard(
            order_id
        ),
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


# =========================================================
# BEGIN NEW ORDER
# =========================================================

async def begin_new_order(
    update,
    context,
    role,
):
    user = update.effective_user

    # Multiple active orders are allowed.
    user_state[user.id] = {
        "action": "new_details",
        "role": role,
    }

    # DRIVER — TEXT OR VOICE
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

    # CLIENT — TEXT ONLY / ENGLISH
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


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.effective_chat:
        return

    if update.effective_chat.type != "private":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        return

    payload = ""

    if context.args:
        payload = (
            context.args[0]
            .strip()
            .lower()
        )

    # DRIVER DEEP LINK
    if payload == "driver":

        allowed = await is_driver(
            update.effective_user.id,
            context,
        )

        if not allowed:
            await update.message.reply_text(
                "⛔ Η λειτουργία αυτή είναι μόνο για οδηγούς.",
                disable_notification=True,
            )
            return

        user_state.pop(
            update.effective_user.id,
            None,
        )

        await begin_new_order(
            update,
            context,
            "driver",
        )

        return

    # CLIENT DEEP LINK
    if payload == "client":

        user_state.pop(
            update.effective_user.id,
            None,
        )

        await begin_new_order(
            update,
            context,
            "client",
        )

        return

    role = await role_for(
        update.effective_user.id,
        context,
    )

    user_state.pop(
        update.effective_user.id,
        None,
    )

    if role == "driver":

        text = (
            "🚕 <b>ΜΕΝΟΥ ΟΔΗΓΟΥ</b>\n\n"
            "Για να δώσεις νέα διαδρομή "
            "πάτησε:\n\n"
            "<b>🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Μπορείς να τη στείλεις με "
            "κείμενο ή 🎤 φωνητικό."
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


# =========================================================
# /PANEL
# =========================================================

async def panel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in (
        DRIVERS_GROUP_ID,
        CLIENTS_GROUP_ID,
    ):
        return

    if not await is_admin_in_chat(
        chat_id,
        user_id,
        context,
    ):
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        return

    try:
        await update.effective_message.delete()
    except Exception:
        pass

    # DRIVER GROUP
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

    # CLIENT GROUP
    if chat_id == CLIENTS_GROUP_ID:

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
                [
                    [
                        InlineKeyboardButton(
                            "🚕 OPEN BOT & REQUEST TAXI",
                            url=(
                                f"https://t.me/{BOT_USERNAME}"
                                "?start=client"
                            ),
                        )
                    ]
                ]
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
            logger.info(
                "Could not pin client welcome message"
            )

        await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🚕 <b>REQUEST A TAXI</b>\n\n"
                "You can also use the permanent "
                "button below."
            ),
            parse_mode="HTML",
            reply_markup=client_group_keyboard(),
            disable_notification=True,
        )

        return


# =========================================================
# /ID
# =========================================================

async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_chat:
        await update.effective_message.reply_text(
            f"Chat ID: {update.effective_chat.id}"
        )


# =========================================================
# SHOW CREATOR ORDERS
# =========================================================

async def show_my_orders(
    update,
    context,
    user_id,
    role,
):
    orders = get_creator_active_orders(
        user_id
    )

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

        status = (
            "✅"
            if order["status"] == "taken"
            else "🟢"
        )

        if is_voice_order(order):
            short_details = "🎤 Φωνητική διαδρομή"
        else:
            short_details = (
                order["details"]
                .replace("\n", " ")
                .strip()
            )

            if len(short_details) > 30:
                short_details = (
                    short_details[:30]
                    + "…"
                )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"{status} #{order['id']} — {short_details}",
                    callback_data=f"manage:{order['id']}",
                )
            ]
        )

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
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
        disable_notification=True,
    )


# =========================================================
# SHOW TAKEN ORDERS — DRIVER
# =========================================================

async def show_taken_orders(
    update,
    context,
    user_id,
):
    orders = get_taken_orders(
        user_id
    )

    if not orders:

        await update.message.reply_text(
            "📦 <b>ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ</b>\n\n"
            "Δεν έχεις πάρει κάποια ενεργή διαδρομή.",
            parse_mode="HTML",
            reply_markup=main_keyboard(
                "driver"
            ),
            disable_notification=True,
        )

        return

    buttons = []

    for order in orders:

        if is_voice_order(order):
            short_details = "🎤 Φωνητική διαδρομή"
        else:
            short_details = (
                order["details"]
                .replace("\n", " ")
                .strip()
            )

            if len(short_details) > 30:
                short_details = (
                    short_details[:30]
                    + "…"
                )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"🚖 #{order['id']} — {short_details}",
                    callback_data=f"taken:{order['id']}",
                )
            ]
        )

    await update.message.reply_text(
        "📦 <b>ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ</b>\n\n"
        "👇 Διάλεξε διαδρομή:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
        disable_notification=True,
    )


# =========================================================
# PRIVATE TEXT
# =========================================================

async def private_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user

    text = (
        update.message.text
        or ""
    ).strip()

    state = user_state.get(
        user.id
    )

    if state and state.get("role"):
        role = state["role"]
    else:
        role = await role_for(
            user.id,
            context,
        )

    # -----------------------------------------------------
    # CANCEL CURRENT STEP
    # -----------------------------------------------------

    if text in (
        BTN_CANCEL_DRIVER,
        BTN_CANCEL_CLIENT,
        OLD_CANCEL,
    ):

        state_role = (
            state.get("role", role)
            if state
            else role
        )

        user_state.pop(
            user.id,
            None,
        )

        if state_role == "client":
            message = "↩️ Request cancelled."
        else:
            message = "↩️ Ακυρώθηκε."

        await update.message.reply_text(
            message,
            reply_markup=main_keyboard(
                state_role
            ),
            disable_notification=True,
        )

        return

    # -----------------------------------------------------
    # NEW DRIVER ORDER
    # -----------------------------------------------------

    if text in (
        BTN_NEW_DRIVER,
        OLD_NEW_DRIVER,
    ):

        if not await is_driver(
            user.id,
            context,
        ):
            await update.message.reply_text(
                "⛔ Η λειτουργία αυτή είναι μόνο για οδηγούς.",
                disable_notification=True,
            )
            return

        await begin_new_order(
            update,
            context,
            "driver",
        )

        return

    # -----------------------------------------------------
    # NEW CLIENT ORDER
    # -----------------------------------------------------

    if text == BTN_NEW_CLIENT:

        await begin_new_order(
            update,
            context,
            "client",
        )

        return

    # -----------------------------------------------------
    # CLIENT MY ORDERS
    # -----------------------------------------------------

    if text in (
        BTN_CLIENT_ORDERS,
        OLD_MY_ORDER,
        OLD_MY_ORDERS,
    ) and role == "client":

        await show_my_orders(
            update,
            context,
            user.id,
            "client",
        )

        return

    # -----------------------------------------------------
    # DRIVER MY ORDERS
    # -----------------------------------------------------

    if text in (
        BTN_DRIVER_ORDERS,
        OLD_MY_ORDER,
        OLD_MY_ORDERS,
    ) and role == "driver":

        await show_my_orders(
            update,
            context,
            user.id,
            "driver",
        )

        return

    # -----------------------------------------------------
    # DRIVER TAKEN ORDERS
    # -----------------------------------------------------

    if text in (
        BTN_TAKEN,
        OLD_TAKEN,
        OLD_TAKEN_ORDERS,
    ):

        if await is_driver(
            user.id,
            context,
        ):
            await show_taken_orders(
                update,
                context,
                user.id,
            )

        return

    # -----------------------------------------------------
    # NO ACTIVE STEP
    # -----------------------------------------------------

    state = user_state.get(
        user.id
    )

    if not state:

        role = await role_for(
            user.id,
            context,
        )

        if role == "driver":
            message = (
                "👇 Χρησιμοποίησε τα κουμπιά παρακάτω."
            )
        else:
            message = (
                "👇 Please use the buttons below."
            )

        await update.message.reply_text(
            message,
            reply_markup=main_keyboard(
                role
            ),
            disable_notification=True,
        )

        return

    action = state["action"]

    state_role = state.get(
        "role",
        role,
    )

    # =====================================================
    # NEW ORDER DETAILS
    # =====================================================

    if action == "new_details":

        if len(text) < 3:

            if state_role == "client":
                message = (
                    "Please send your trip details."
                )
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

        # CLIENT → PRICE STEP
        if state_role == "client":

            state["action"] = "new_price"

            await update.message.reply_text(
                "💶 <b>HOW MUCH ARE YOU WILLING TO PAY?</b>\n\n"
                "Enter your offer in EUR.\n"
                "Example: <b>50</b>\n\n"
                "If you are not sure, tap the button below.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🤝 SKIP / NOT SURE",
                                callback_data="skip_new_price",
                            )
                        ]
                    ]
                ),
                disable_notification=True,
            )

        # DRIVER TEXT ORDER
        else:

            order_id = await create_text_order(
                context,
                user,
                "driver",
                text,
            )

            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΗΜΟΣΙΕΥΤΗΚΕ</b>\n\n"
                f"🔢 Αριθμός: #{order_id}\n\n"
                "Η διαδρομή εμφανίστηκε στην ομάδα οδηγών.",
                parse_mode="HTML",
                reply_markup=main_keyboard(
                    "driver"
                ),
                disable_notification=True,
            )

        return

    # =====================================================
    # CLIENT PRICE
    # =====================================================

    if action == "new_price":

        cleaned = (
            text
            .replace("€", "")
            .replace(",", ".")
            .strip()
        )

        try:
            value = float(
                cleaned
            )

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

        user_state.pop(
            user.id,
            None,
        )

        await update.message.reply_text(
            "🔎 <b>LOOKING FOR A DRIVER</b>\n\n"
            f"Request: #{order_id}\n"
            f"💶 Your offer: €{price}\n\n"
            "Your request has been sent to our drivers.\n\n"
            "You will be notified when a driver "
            "accepts your trip.",
            parse_mode="HTML",
            reply_markup=main_keyboard(
                "client"
            ),
            disable_notification=True,
        )

        return

    # =====================================================
    # EDIT DETAILS
    # =====================================================

    if action == "edit_details":

        order_id = state["order_id"]

        order = get_order(
            order_id
        )

        if (
            not order
            or order["creator_id"] != user.id
            or order["status"] not in (
                "open",
                "taken",
            )
        ):

            user_state.pop(
                user.id,
                None,
            )

            if state_role == "client":
                message = (
                    "This request can no longer be edited."
                )
            else:
                message = (
                    "Η διαδρομή δεν μπορεί πλέον να αλλάξει."
                )

            await update.message.reply_text(
                message,
                reply_markup=main_keyboard(
                    state_role
                ),
                disable_notification=True,
            )

            return

        # Voice order is not edited as text.
        if is_voice_order(order):

            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "🎤 Η φωνητική διαδρομή δεν αλλάζει ως κείμενο.\n"
                "Ακύρωσέ την και δημιούργησε νέα αν χρειάζεται.",
                reply_markup=main_keyboard(
                    "driver"
                ),
                disable_notification=True,
            )

            return

        async with db_lock:
            with db() as conn:

                conn.execute(
                    """
                    UPDATE orders
                    SET details = ?
                    WHERE id = ?
                    """,
                    (
                        text,
                        order_id,
                    ),
                )

                conn.commit()

        await refresh_group_card(
            context,
            order_id,
        )

        updated = get_order(
            order_id
        )

        user_state.pop(
            user.id,
            None,
        )

        # Notify accepted driver in Greek.
        if (
            updated["status"] == "taken"
            and updated["taker_id"]
        ):
            try:
                await context.bot.send_message(
                    chat_id=updated["taker_id"],
                    text=(
                        "⚠️ <b>ΑΛΛΑΓΗ ΔΙΑΔΡΟΜΗΣ</b>\n\n"
                        f"Η διαδρομή #{order_id} άλλαξε.\n\n"
                        f"{html.escape(text)}"
                    ),
                    parse_mode="HTML",
                    reply_markup=accepted_driver_keyboard(
                        updated
                    ),
                    disable_notification=True,
                )

            except Exception:
                logger.exception(
                    "Could not notify accepted driver"
                )

        if updated["creator_role"] == "client":

            message = (
                "✅ Your trip details have been updated."
            )

            keyboard_role = "client"

        else:

            message = (
                "✅ Η διαδρομή ενημερώθηκε."
            )

            keyboard_role = "driver"

        await update.message.reply_text(
            message,
            reply_markup=main_keyboard(
                keyboard_role
            ),
            disable_notification=True,
        )

        return

    # =====================================================
    # EDIT CLIENT PRICE
    # =====================================================

    if action == "edit_price":

        order_id = state["order_id"]

        cleaned = (
            text
            .replace("€", "")
            .replace(",", ".")
            .strip()
        )

        try:
            value = float(
                cleaned
            )

            if value <= 0:
                raise ValueError

        except ValueError:

            await update.message.reply_text(
                "Please enter a valid amount in EUR.\n"
                "Example: 50",
                disable_notification=True,
            )

            return

        order = get_order(
            order_id
        )

        if (
            not order
            or order["creator_id"] != user.id
            or order["creator_role"] != "client"
            or order["status"] not in (
                "open",
                "taken",
            )
        ):

            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "This request can no longer be edited.",
                reply_markup=main_keyboard(
                    "client"
                ),
                disable_notification=True,
            )

            return

        price = f"{value:g}"

        async with db_lock:
            with db() as conn:

                conn.execute(
                    """
                    UPDATE orders
                    SET price = ?
                    WHERE id = ?
                    """,
                    (
                        price,
                        order_id,
                    ),
                )

                conn.commit()

        await refresh_group_card(
            context,
            order_id,
        )

        updated = get_order(
            order_id
        )

        user_state.pop(
            user.id,
            None,
        )

        if (
            updated["status"] == "taken"
            and updated["taker_id"]
        ):
            try:
                await context.bot.send_message(
                    chat_id=updated["taker_id"],
                    text=(
                        "⚠️ <b>ΑΛΛΑΓΗ ΤΙΜΗΣ</b>\n\n"
                        f"Διαδρομή #{order_id}\n"
                        f"Νέα τιμή: €{price}"
                    ),
                    parse_mode="HTML",
                    disable_notification=True,
                )

            except Exception:
                logger.exception(
                    "Could not notify driver"
                )

        await update.message.reply_text(
            "✅ Your offer has been updated.",
            reply_markup=main_keyboard(
                "client"
            ),
            disable_notification=True,
        )

        return


# =========================================================
# PRIVATE VOICE — DRIVER ONLY
# =========================================================

async def private_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    if not update.message.voice:
        return

    user = update.effective_user

    # Only real members of driver group may create
    # voice orders.
    if not await is_driver(
        user.id,
        context,
    ):
        await update.message.reply_text(
            "Voice requests are not available for clients.",
            reply_markup=main_keyboard(
                "client"
            ),
            disable_notification=True,
        )
        return

    state = user_state.get(
        user.id
    )

    # Voice is accepted only after driver started
    # NEW ORDER flow.
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
            reply_markup=main_keyboard(
                "driver"
            ),
            disable_notification=True,
        )

        return

    voice_file_id = (
        update.message.voice.file_id
    )

    try:
        order_id = await create_voice_order(
            context,
            user,
            voice_file_id,
        )

    except Exception:
        logger.exception(
            "Could not create voice order"
        )

        await update.message.reply_text(
            "❌ Δεν μπόρεσα να δημοσιεύσω "
            "το φωνητικό. Δοκίμασε ξανά.",
            disable_notification=True,
        )

        return

    user_state.pop(
        user.id,
        None,
    )

    await update.message.reply_text(
        "✅ <b>Η ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ ΔΗΜΟΣΙΕΥΤΗΚΕ</b>\n\n"
        f"🎤 Αριθμός: #{order_id}\n\n"
        "Το φωνητικό εμφανίστηκε στην ομάδα οδηγών.",
        parse_mode="HTML",
        reply_markup=main_keyboard(
            "driver"
        ),
        disable_notification=True,
    )


# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    if not update.effective_user:
        return

    user = update.effective_user
    data = query.data or ""

    # =====================================================
    # CLIENT SKIPS PRICE
    # =====================================================

    if data == "skip_new_price":

        state = user_state.get(
            user.id
        )

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

        user_state.pop(
            user.id,
            None,
        )

        await query.edit_message_text(
            "🔎 <b>LOOKING FOR A DRIVER</b>\n\n"
            f"Request: #{order_id}\n"
            "💶 Your offer: Negotiable\n\n"
            "Your request has been sent to our drivers.\n\n"
            "You will be notified when a driver "
            "accepts your trip.",
            parse_mode="HTML",
        )

        await context.bot.send_message(
            chat_id=user.id,
            text="👇 You can use the buttons below.",
            reply_markup=main_keyboard(
                "client"
            ),
            disable_notification=True,
        )

        return

    parts = data.split(
        ":",
        1,
    )

    if (
        len(parts) != 2
        or not parts[1].isdigit()
    ):
        await query.answer()
        return

    action = parts[0]
    order_id = int(
        parts[1]
    )

    order = get_order(
        order_id
    )

    if not order:

        await query.answer(
            "Order not found.",
            show_alert=True,
        )

        return

    # =====================================================
    # DRIVER TAKES ORDER
    # =====================================================

    if action == "take":

        if not await is_driver(
            user.id,
            context,
        ):

            await query.answer(
                "Μόνο για οδηγούς.",
                show_alert=True,
            )

            return

        # Driver cannot take own driver-created order.
        if (
            order["creator_role"] == "driver"
            and order["creator_id"] == user.id
        ):

            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )

            return

        # Atomic first-driver-wins update.
        async with db_lock:
            with db() as conn:

                current = conn.execute(
                    """
                    SELECT *
                    FROM orders
                    WHERE id = ?
                    """,
                    (
                        order_id,
                    ),
                ).fetchone()

                if (
                    not current
                    or current["status"] != "open"
                ):

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

        await query.answer(
            "✅ Πήρες τη διαδρομή!"
        )

        # Same group card is edited.
        await refresh_group_card(
            context,
            order_id,
        )

        updated = get_order(
            order_id
        )

        # -------------------------------------------------
        # DRIVER WHO TOOK ORDER
        # Greek + creator identity + contact button.
        # -------------------------------------------------

        try:

            if is_voice_order(updated):

                driver_text = (
                    "✅ <b>ΠΗΡΕΣ ΤΗ ΦΩΝΗΤΙΚΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
                    f"🔢 Αριθμός: #{order_id}\n"
                    f"👤 Από: "
                    f"<b>{html.escape(updated['creator_name'])}</b>\n\n"
                    "🎤 Το φωνητικό βρίσκεται "
                    "στην ομάδα οδηγών."
                )

            else:

                driver_text = (
                    "✅ <b>ΠΗΡΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
                    f"🔢 Αριθμός: #{order_id}\n\n"
                    f"{html.escape(updated['details'])}\n\n"
                    f"👤 Από: "
                    f"<b>{html.escape(updated['creator_name'])}</b>"
                )

                if (
                    updated["creator_role"] == "client"
                    and updated["price"]
                ):
                    driver_text += (
                        "\n"
                        f"💶 Προσφορά: "
                        f"<b>€{html.escape(updated['price'])}</b>"
                    )

            await context.bot.send_message(
                chat_id=user.id,
                text=driver_text,
                parse_mode="HTML",
                reply_markup=accepted_driver_keyboard(
                    updated
                ),
                disable_notification=True,
            )

        except Exception:
            logger.exception(
                "Could not message taker"
            )

        # -------------------------------------------------
        # CREATOR GETS WHO ACCEPTED + CONTACT BUTTON
        # -------------------------------------------------

        try:

            # CLIENT — ENGLISH
            if updated["creator_role"] == "client":

                creator_text = (
                    "✅ <b>DRIVER FOUND!</b>\n\n"
                    f"🚕 Request: #{order_id}\n"
                    f"👤 Driver: "
                    f"<b>{html.escape(person_name(user))}</b>\n\n"
                    "Your driver has accepted your trip.\n\n"
                    "👇 You can contact the driver "
                    "using the button below."
                )

            # DRIVER CREATOR — GREEK
            else:

                creator_text = (
                    "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n\n"
                    f"🔢 Αριθμός: #{order_id}\n"
                    f"🚖 Την πήρε ο οδηγός: "
                    f"<b>{html.escape(person_name(user))}</b>\n\n"
                    "👇 Μπορείς να επικοινωνήσεις "
                    "μαζί του από το κουμπί παρακάτω."
                )

            await context.bot.send_message(
                chat_id=updated["creator_id"],
                text=creator_text,
                parse_mode="HTML",
                reply_markup=accepted_creator_keyboard(
                    updated
                ),
                disable_notification=True,
            )

        except Exception:
            logger.exception(
                "Could not message creator"
            )

        return

    # =====================================================
    # CREATOR SECURITY
    # =====================================================

    if action in {
        "manage",
        "edit_details",
        "edit_price",
        "cancel_order",
        "confirm_cancel",
    }:

        if order["creator_id"] != user.id:

            if order["creator_role"] == "client":
                message = (
                    "This is not your request."
                )
            else:
                message = (
                    "Δεν είναι δική σου διαδρομή."
                )

            await query.answer(
                message,
                show_alert=True,
            )

            return

    # =====================================================
    # MANAGE
    # =====================================================

    if action == "manage":

        await query.answer()

        if order["status"] == "taken":
            markup = accepted_creator_keyboard(
                order
            )
        else:
            markup = creator_manage_keyboard(
                order
            )

        await query.edit_message_text(
            private_order_card(
                order
            ),
            parse_mode="HTML",
            reply_markup=markup,
        )

        return

    # =====================================================
    # EDIT DETAILS
    # =====================================================

    if action == "edit_details":

        if order["status"] not in (
            "open",
            "taken",
        ):

            if order["creator_role"] == "client":
                message = (
                    "This request can no longer be edited."
                )
            else:
                message = (
                    "Δεν μπορεί να αλλάξει."
                )

            await query.answer(
                message,
                show_alert=True,
            )

            return

        if is_voice_order(order):

            await query.answer(
                "Η φωνητική διαδρομή δεν αλλάζει ως κείμενο.",
                show_alert=True,
            )

            return

        user_state[user.id] = {
            "action": "edit_details",
            "order_id": order_id,
            "role": order["creator_role"],
        }

        await query.answer()

        if order["creator_role"] == "client":

            message = (
                "✏️ <b>CHANGE TRIP DETAILS</b>\n\n"
                f"Send the NEW details for "
                f"request #{order_id} in one message."
            )

            role = "client"

        else:

            message = (
                "✏️ <b>ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ</b>\n\n"
                f"Στείλε τα ΝΕΑ στοιχεία "
                f"για τη διαδρομή #{order_id} "
                "σε ένα μήνυμα."
            )

            role = "driver"

        await context.bot.send_message(
            chat_id=user.id,
            text=message,
            parse_mode="HTML",
            reply_markup=cancel_keyboard(
                role
            ),
            disable_notification=True,
        )

        return

    # =====================================================
    # EDIT PRICE — CLIENT
    # =====================================================

    if action == "edit_price":

        if (
            order["creator_role"] != "client"
            or order["status"] not in (
                "open",
                "taken",
            )
        ):

            await query.answer(
                "The price cannot be changed.",
                show_alert=True,
            )

            return

        user_state[user.id] = {
            "action": "edit_price",
            "order_id": order_id,
            "role": "client",
        }

        await query.answer()

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "💶 <b>CHANGE PRICE</b>\n\n"
                f"Send your new offer for "
                f"request #{order_id}.\n\n"
                "Example: <b>50</b>"
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard(
                "client"
            ),
            disable_notification=True,
        )

        return

    # =====================================================
    # ASK CANCEL
    # =====================================================

    if action == "cancel_order":

        if order["status"] not in (
            "open",
            "taken",
        ):

            if order["creator_role"] == "client":
                message = (
                    "This request is already closed."
                )
            else:
                message = (
                    "Η διαδρομή έχει ήδη κλείσει."
                )

            await query.answer(
                message,
                show_alert=True,
            )

            return

        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=confirm_cancel_keyboard(
                order
            )
        )

        return

    # =====================================================
    # CONFIRM CANCEL
    # =====================================================

    if action == "confirm_cancel":

        if order["status"] not in (
            "open",
            "taken",
        ):

            if order["creator_role"] == "client":
                message = (
                    "This request is already closed."
                )
            else:
                message = (
                    "Η διαδρομή έχει ήδη κλείσει."
                )

            await query.answer(
                message,
                show_alert=True,
            )

            return

        taker_id = order["taker_id"]

        async with db_lock:
            with db() as conn:

                conn.execute(
                    """
                    UPDATE orders
                    SET status = 'cancelled'
                    WHERE id = ?
                    """,
                    (
                        order_id,
                    ),
                )

                conn.commit()

        await refresh_group_card(
            context,
            order_id,
        )

        # CLIENT
        if order["creator_role"] == "client":

            await query.answer(
                "Request cancelled."
            )

            await query.edit_message_text(
                "❌ <b>REQUEST CANCELLED</b>\n\n"
                f"Taxi request #{order_id} has been cancelled.",
                parse_mode="HTML",
            )

        # DRIVER
        else:

            await query.answer(
                "Η διαδρομή ακυρώθηκε."
            )

            await query.edit_message_text(
                "❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>\n\n"
                f"Διαδρομή #{order_id}.",
                parse_mode="HTML",
            )

        # Accepted driver always receives Greek.
        if taker_id:

            try:
                await context.bot.send_message(
                    chat_id=taker_id,
                    text=(
                        "❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>\n\n"
                        f"Η διαδρομή #{order_id} "
                        "ακυρώθηκε από αυτόν που την έδωσε."
                    ),
                    parse_mode="HTML",
                    disable_notification=True,
                )

            except Exception:
                logger.exception(
                    "Could not notify taker"
                )

        return

    # =====================================================
    # TAKEN ORDER SECURITY
    # =====================================================

    if action in (
        "taken",
        "giveup",
        "confirm_giveup",
    ):

        if (
            order["status"] != "taken"
            or order["taker_id"] != user.id
        ):

            await query.answer(
                "Η διαδρομή δεν είναι πλέον δική σου.",
                show_alert=True,
            )

            return

    # =====================================================
    # SHOW TAKEN ORDER
    # =====================================================

    if action == "taken":

        await query.answer()

        if is_voice_order(order):

            text = (
                "📦 <b>ΔΙΑΔΡΟΜΗ ΠΟΥ ΠΗΡΑ</b>\n\n"
                f"🎤 Φωνητική διαδρομή #{order_id}\n"
                f"👤 Από: "
                f"<b>{html.escape(order['creator_name'])}</b>\n\n"
                "Το αρχικό φωνητικό βρίσκεται "
                "στην ομάδα οδηγών."
            )

        else:

            text = (
                "📦 <b>ΔΙΑΔΡΟΜΗ ΠΟΥ ΠΗΡΑ</b>\n\n"
                + order_card(order)
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=accepted_driver_keyboard(
                order
            ),
        )

        return

    # =====================================================
    # ASK GIVE UP
    # =====================================================

    if action == "giveup":

        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=confirm_giveup_keyboard(
                order_id
            )
        )

        return

    # =====================================================
    # CONFIRM GIVE UP
    # =====================================================

    if action == "confirm_giveup":

        async with db_lock:
            with db() as conn:

                current = conn.execute(
                    """
                    SELECT *
                    FROM orders
                    WHERE id = ?
                    """,
                    (
                        order_id,
                    ),
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
                    (
                        order_id,
                    ),
                )

                conn.commit()

        await query.answer(
            "Η διαδρομή επέστρεψε."
        )

        # Same card becomes available again.
        # No new group notification.
        await refresh_group_card(
            context,
            order_id,
        )

        await query.edit_message_text(
            "↩️ <b>ΑΦΗΣΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            f"Η διαδρομή #{order_id} "
            "είναι ξανά διαθέσιμη.",
            parse_mode="HTML",
        )

        # CLIENT CREATOR — ENGLISH
        if order["creator_role"] == "client":

            creator_text = (
                "🔎 <b>LOOKING FOR ANOTHER DRIVER</b>\n\n"
                f"The driver released your request "
                f"#{order_id}.\n\n"
                "Your request is available "
                "to our drivers again."
            )

        # DRIVER CREATOR — GREEK
        else:

            creator_text = (
                "↩️ <b>Η ΔΙΑΔΡΟΜΗ ΕΙΝΑΙ "
                "ΞΑΝΑ ΔΙΑΘΕΣΙΜΗ</b>\n\n"
                f"Ο οδηγός άφησε τη διαδρομή "
                f"#{order_id}.\n\n"
                "Η διαδρομή είναι ξανά διαθέσιμη "
                "στους οδηγούς."
            )

        try:
            await context.bot.send_message(
                chat_id=order["creator_id"],
                text=creator_text,
                parse_mode="HTML",
                disable_notification=True,
            )

        except Exception:
            logger.exception(
                "Could not notify creator"
            )

        return

    await query.answer()


# =========================================================
# DRIVER GROUP
# =========================================================

async def driver_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user

    text = (
        update.message.text
        or ""
    ).strip()

    # Persistent button:
    # delete technical group message and continue privately.
    if text in (
        BTN_NEW_DRIVER,
        OLD_NEW_DRIVER,
    ):

        try:
            await update.message.delete()
        except Exception:
            pass

        if not await is_driver(
            user.id,
            context,
        ):
            return

        try:
            await begin_new_order(
                update,
                context,
                "driver",
            )

        except Exception:
            user_state.pop(
                user.id,
                None,
            )

            logger.exception(
                "Could not start private driver flow"
            )

        return

    # Admins can write normally.
    if await is_admin_in_chat(
        DRIVERS_GROUP_ID,
        user.id,
        context,
    ):
        return

    # Other ordinary messages are removed.
    try:
        await update.message.delete()
    except Exception:
        pass


# =========================================================
# CLIENT GROUP
# =========================================================

async def client_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user

    text = (
        update.message.text
        or ""
    ).strip()

    if text == BTN_NEW_CLIENT:

        # Delete technical group message.
        try:
            await update.message.delete()
        except Exception:
            pass

        # Continue privately if user already started bot.
        try:
            await begin_new_order(
                update,
                context,
                "client",
            )

        except Exception:

            user_state.pop(
                user.id,
                None,
            )

            # New clients should use pinned deep-link button.
            logger.info(
                "Client must start bot privately first: %s",
                user.id,
            )

        return

    # Admins can write normally.
    if await is_admin_in_chat(
        CLIENTS_GROUP_ID,
        user.id,
        context,
    ):
        return

    # Keep public client group clean.
    try:
        await update.message.delete()
    except Exception:
        pass


# =========================================================
# GROUP COMMAND CLEANUP
# =========================================================

async def group_commands_cleanup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_chat:
        return

    if not update.effective_user:
        return

    chat_id = update.effective_chat.id

    if chat_id not in (
        DRIVERS_GROUP_ID,
        CLIENTS_GROUP_ID,
    ):
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


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled exception",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set"
        )

    init_db()

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "panel",
            panel_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "id",
            id_command,
        )
    )

    # Inline buttons
    app.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # Private text
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    # Private voice — driver voice orders
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.VOICE,
            private_voice,
        )
    )

    # Driver group text
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

    # Client group text
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

    # Remove commands from ordinary members in groups
    app.add_handler(
        MessageHandler(
            (
                filters.Chat(
                    DRIVERS_GROUP_ID
                )
                | filters.Chat(
                    CLIENTS_GROUP_ID
                )
            )
            & filters.COMMAND,
            group_commands_cleanup,
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
