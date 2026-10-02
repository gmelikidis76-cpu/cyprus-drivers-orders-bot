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
# ΡΥΘΜΙΣΕΙΣ
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")

DRIVERS_GROUP_ID = -1004449292276
BOT_USERNAME = "CyprusDriversOrdersBot"
DB_PATH = os.getenv("DB_PATH", "/data/orders.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()

# Μόνο προσωρινά βήματα συνομιλίας.
# Οι διαδρομές αποθηκεύονται μόνιμα στη βάση.
user_state = {}


# =========================================================
# ΚΟΥΜΠΙΑ
# =========================================================

BTN_NEW_DRIVER = "🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_MY_ORDERS = "📋 ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ"
BTN_TAKEN = "📦 ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ"
BTN_CANCEL = "❌ ΑΚΥΡΩΣΗ"

# Client button
BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"

# Παλιές ονομασίες για να συνεχίσουν να λειτουργούν
# παλιά πληκτρολόγια μέχρι να ανανεωθούν.
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


async def is_admin(user_id, context):
    try:
        member = await context.bot.get_chat_member(
            DRIVERS_GROUP_ID,
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
# ΠΛΗΚΤΡΟΛΟΓΙΟ ΠΡΟΣΩΠΙΚΟΥ BOT
# =========================================================

def main_keyboard(role):
    if role == "driver":
        return ReplyKeyboardMarkup(
            [
                [BTN_NEW_DRIVER],
                [BTN_MY_ORDERS],
                [BTN_TAKEN],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    return ReplyKeyboardMarkup(
        [
            [BTN_NEW_CLIENT],
            [BTN_MY_ORDERS],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        [[BTN_CANCEL]],
        resize_keyboard=True,
        is_persistent=True,
    )


# =========================================================
# НОВАЯ ПОСТОЯННАЯ КНОПКА В ГРУППЕ ВОДИТЕЛЕЙ
# =========================================================

def driver_group_keyboard():
    return ReplyKeyboardMarkup(
        [[BTN_NEW_DRIVER]],
        resize_keyboard=True,
        is_persistent=True,
        selective=False,
        input_field_placeholder="Νέα διαδρομή...",
    )


# =========================================================
# ΠΑΝΕΛ ΟΜΑΔΑΣ
# =========================================================

def driver_panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚕🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ 🚕🚕",
                    url=(
                        f"https://t.me/{BOT_USERNAME}"
                        "?start=driver"
                    ),
                )
            ]
        ]
    )


# =========================================================
# ΚΟΥΜΠΙΑ ΔΙΑΔΡΟΜΗΣ
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
    rows = [
        [
            InlineKeyboardButton(
                "✏️ ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ",
                callback_data=f"edit_details:{order['id']}",
            )
        ]
    ]

    if order["creator_role"] == "client":
        rows.append(
            [
                InlineKeyboardButton(
                    "💶 ΑΛΛΑΓΗ ΤΙΜΗΣ",
                    callback_data=f"edit_price:{order['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
                callback_data=f"cancel_order:{order['id']}",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


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
    rows = []

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

    rows.append(
        [
            InlineKeyboardButton(
                "✏️ ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ",
                callback_data=f"edit_details:{order['id']}",
            )
        ]
    )

    if order["creator_role"] == "client":
        rows.append(
            [
                InlineKeyboardButton(
                    "💶 ΑΛΛΑΓΗ ΤΙΜΗΣ",
                    callback_data=f"edit_price:{order['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
                callback_data=f"cancel_order:{order['id']}",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def confirm_cancel_keyboard(order_id):
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
# ΚΑΡΤΑ ΔΙΑΔΡΟΜΗΣ
# =========================================================

def order_card(order):
    details = html.escape(
        order["details"]
    )

    creator = html.escape(
        order["creator_name"]
    )

    order_id = order["id"]

    if order["creator_role"] == "client":
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
            f"👤 Πελάτης: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    else:
        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΟΔΗΓΟΣ</b>\n\n"
            f"{details}\n\n"
            f"👤 Από: {creator}\n"
            f"🔢 Αριθμός: #{order_id}"
        )

    if order["status"] == "taken":
        taker = html.escape(
            order["taker_name"] or "Driver"
        )

        text += (
            "\n\n"
            "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n"
            f"🚖 Οδηγός: {taker}"
        )

    elif order["status"] == "cancelled":
        text += (
            "\n\n"
            "❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>"
        )

    return text


# =========================================================
# ΑΝΑΝΕΩΣΗ ΥΠΑΡΧΟΥΣΑΣ ΚΑΡΤΑΣ
# ΧΩΡΙΣ ΝΕΟ ΜΗΝΥΜΑ ΣΤΗΝ ΟΜΑΔΑ
# =========================================================

async def refresh_group_card(
    context,
    order_id,
):
    order = get_order(order_id)

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
# ΔΗΜΙΟΥΡΓΙΑ ΝΕΑΣ ΔΙΑΔΡΟΜΗΣ
# =========================================================

async def create_order(
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

    # ΜΟΝΟ Η ΕΤΟΙΜΗ ΔΙΑΔΡΟΜΗ
    # ΔΗΜΙΟΥΡΓΕΙ ΝΕΑ ΚΑΡΤΑ ΣΤΗΝ ΟΜΑΔΑ.

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
