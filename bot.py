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

# Driver group
DRIVERS_GROUP_ID = -1004449292276

# Public client group: CYPRUS TAXI
CLIENTS_GROUP_ID = -1004401199110

BOT_USERNAME = "CyprusDriversOrdersBot"

# Railway persistent volume
DB_PATH = os.getenv("DB_PATH", "/data/orders.db")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()

# Temporary conversation states.
# Orders themselves are stored permanently in SQLite.
user_state = {}


# =========================================================
# BUTTON TEXT
# =========================================================

BTN_NEW_DRIVER = "🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_MY_ORDERS = "📋 ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ"
BTN_TAKEN = "📦 ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ"
BTN_CANCEL = "❌ ΑΚΥΡΩΣΗ"

BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"

# Old button names for compatibility
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


async def is_admin_in_chat(chat_id, user_id, context):
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


async def is_admin(user_id, context):
    return await is_admin_in_chat(
        DRIVERS_GROUP_ID,
        user_id,
        context,
    )


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
# GROUP PERMANENT KEYBOARDS
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
# ORDER CARD
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
# REFRESH EXISTING GROUP CARD
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
# CREATE ORDER
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

    # ONLY THE FINISHED ORDER IS POSTED
    # TO THE DRIVER GROUP.
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
# START NEW ORDER PRIVATELY
# =========================================================

async def begin_new_order(
    update,
    context,
    role,
):
    user = update.effective_user

    user_state[user.id] = {
        "action": "new_details",
        "role": role,
    }

    if role == "driver":

        text = (
            "🚕 <b>ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Γράψε όλα τα στοιχεία σε ΕΝΑ μήνυμα.\n\n"
            "Παράδειγμα:\n\n"
            "Larnaca Airport → Limassol\n"
            "23:00\n"
            "2 άτομα\n"
            "€70\n\n"
            "👇 Στείλε τώρα τα στοιχεία."
        )

    else:

        text = (
            "🚕 <b>REQUEST A TAXI</b>\n\n"
            "Please send all your trip details "
            "in ONE message.\n\n"
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
        reply_markup=cancel_keyboard(),
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

    # Do not use /start inside groups
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

    # Deep link for driver
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

    # Deep link for client
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
            "Θέλεις να δώσεις μια διαδρομή "
            "σε άλλον οδηγό;\n\n"
            "👇 Πάτησε:\n"
            "<b>🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Μπορείς να δώσεις όσες "
            "διαδρομές θέλεις."
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
#
# Run /panel once in each group.
#
# Driver group:
# installs permanent driver button.
#
# Client group:
# installs public client welcome button
# + permanent bottom REQUEST A TAXI button.
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

    # =====================================================
    # DRIVER GROUP PANEL
    # =====================================================

    if chat_id == DRIVERS_GROUP_ID:

        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=(
                "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
                "Για να δώσεις νέα διαδρομή, "
                "πάτησε το κουμπί στο κάτω μέρος."
            ),
            parse_mode="HTML",
            reply_markup=driver_group_keyboard(),
            disable_notification=True,
        )

        return

    # =====================================================
    # CLIENT GROUP PANEL
    # =====================================================

    if chat_id == CLIENTS_GROUP_ID:

        # Public welcome message.
        # This button works even for a brand-new client
        # who has NEVER started the bot before.
        welcome_message = await context.bot.send_message(
            chat_id=CLIENTS_GROUP_ID,
            text=(
                "🚕 <b>CYPRUS TAXI</b>\n\n"
                "Need a taxi in Cyprus?\n\n"
                "Airport transfers • City rides • "
                "Long-distance trips\n\n"
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
                                f"https://t.me/"
                                f"{BOT_USERNAME}"
                                f"?start=client"
                            ),
                        )
                    ]
                ]
            ),
            disable_notification=True,
        )

        # Try to pin the welcome message.
        # If bot does not have pin permission,
        # nothing breaks.
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

        # Install persistent bottom keyboard.
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
# MY ORDERS
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
                "You have no active orders."
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

        short_details = (
            order["details"]
            .replace("\n", " ")
            .strip()
        )

        if len(short_details) > 30:
            short_details = (
                short_details[:30] + "…"
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"{status} #{order['id']} — "
                    f"{short_details}",
                    callback_data=(
                        f"manage:{order['id']}"
                    ),
                )
            ]
        )

    if role == "driver":

        text = (
            "📋 <b>ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ</b>\n\n"
            "👇 Διάλεξε τη διαδρομή που θέλεις:"
        )

    else:

        text = (
            "📋 <b>MY ORDERS</b>\n\n"
            "Choose an order:"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
        disable_notification=True,
    )


# =========================================================
# TAKEN ORDERS
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
            "Δεν έχεις πάρει κάποια "
            "ενεργή διαδρομή.",
            parse_mode="HTML",
            reply_markup=main_keyboard(
                "driver"
            ),
            disable_notification=True,
        )

        return

    buttons = []

    for order in orders:

        short_details = (
            order["details"]
            .replace("\n", " ")
            .strip()
        )

        if len(short_details) > 30:
            short_details = (
                short_details[:30] + "…"
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"🚖 #{order['id']} — "
                    f"{short_details}",
                    callback_data=(
                        f"taken:{order['id']}"
                    ),
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
# PRIVATE TEXT HANDLER
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

    role = await role_for(
        user.id,
        context,
    )

    # =====================================================
    # CANCEL
    # =====================================================

    if text in (
        BTN_CANCEL,
        OLD_CANCEL,
    ):

        state = user_state.get(user.id)

        if state:
            state_role = state.get(
                "role",
                role,
            )
        else:
            state_role = role

        user_state.pop(
            user.id,
            None,
        )

        msg = (
            "↩️ Ακυρώθηκε."
            if state_role == "driver"
            else
            "↩️ Cancelled."
        )

        await update.message.reply_text(
            msg,
            reply_markup=main_keyboard(
                state_role
            ),
            disable_notification=True,
        )

        return

    # =====================================================
    # NEW DRIVER ORDER
    # =====================================================

    if text in (
        BTN_NEW_DRIVER,
        OLD_NEW_DRIVER,
    ):

        if role != "driver":

            await update.message.reply_text(
                "⛔ Μόνο για οδηγούς.",
                disable_notification=True,
            )

            return

        await begin_new_order(
            update,
            context,
            "driver",
        )

        return

    # =====================================================
    # NEW CLIENT ORDER
    # =====================================================

    if text == BTN_NEW_CLIENT:

        await begin_new_order(
            update,
            context,
            "client",
        )

        return

    # =====================================================
    # MY ORDERS
    # =====================================================

    if text in (
        BTN_MY_ORDERS,
        OLD_MY_ORDER,
        OLD_MY_ORDERS,
    ):

        await show_my_orders(
            update,
            context,
            user.id,
            role,
        )

        return

    # =====================================================
    # TAKEN ORDERS
    # =====================================================

    if text in (
        BTN_TAKEN,
        OLD_TAKEN,
        OLD_TAKEN_ORDERS,
    ):

        if role == "driver":

            await show_taken_orders(
                update,
                context,
                user.id,
            )

        return

    state = user_state.get(
        user.id
    )

    if not state:

        msg = (
            "👇 Χρησιμοποίησε τα κουμπιά παρακάτω."
            if role == "driver"
            else
            "👇 Please use the buttons below."
        )

        await update.message.reply_text(
            msg,
            reply_markup=main_keyboard(role),
            disable_notification=True,
        )

        return

    action = state["action"]
    state_role = state.get(
        "role",
        role,
    )

    # =====================================================
    # NEW DETAILS
    # =====================================================

    if action == "new_details":

        if len(text) < 3:

            await update.message.reply_text(
                (
                    "Γράψε τα στοιχεία της διαδρομής."
                    if state_role == "driver"
                    else
                    "Please send the trip details."
                ),
                disable_notification=True,
            )

            return

        state["details"] = text

        if state_role == "client":

            state["action"] = "new_price"

            await update.message.reply_text(
                "💶 <b>HOW MUCH ARE YOU "
                "WILLING TO PAY?</b>\n\n"
                "Enter your offer in EUR.\n"
                "Example: <b>50</b>\n\n"
                "If you are not sure, "
                "tap below.",
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

        else:

            order_id = await create_order(
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
                "✅ <b>Η ΔΙΑΔΡΟΜΗ "
                "ΔΗΜΟΣΙΕΥΤΗΚΕ</b>\n\n"
                f"Αριθμός: #{order_id}\n\n"
                "Η διαδρομή εμφανίστηκε "
                "στην ομάδα οδηγών.\n\n"
                "Μπορείς να δώσεις αμέσως "
                "και άλλη διαδρομή.",
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

            value = float(cleaned)

            if value <= 0:
                raise ValueError

        except ValueError:

            await update.message.reply_text(
                "Please enter a valid amount.\n"
                "Example: 50",
                disable_notification=True,
            )

            return

        price = f"{value:g}"

        order_id = await create_order(
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
            "Your request has been sent "
            "to our drivers.\n\n"
            "You will be notified when "
            "a driver accepts your trip.",
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
        order = get_order(order_id)

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

            await update.message.reply_text(
                (
                    "Η διαδρομή δεν μπορεί "
                    "πλέον να αλλάξει."
                    if role == "driver"
                    else
                    "This order can no longer be edited."
                ),
                reply_markup=main_keyboard(role),
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

        if (
            updated["status"] == "taken"
            and updated["taker_id"]
        ):

            try:

                await context.bot.send_message(
                    chat_id=updated["taker_id"],
                    text=(
                        "⚠️ <b>ΑΛΛΑΓΗ ΔΙΑΔΡΟΜΗΣ</b>\n\n"
                        f"Η διαδρομή #{order_id} "
                        "άλλαξε.\n\n"
                        f"{html.escape(text)}"
                    ),
                    parse_mode="HTML",
                    reply_markup=(
                        accepted_driver_keyboard(
                            updated
                        )
                    ),
                    disable_notification=True,
                )

            except Exception:

                logger.exception(
                    "Could not notify taker"
                )

        await update.message.reply_text(
            (
                "✅ Η διαδρομή ενημερώθηκε."
                if role == "driver"
                else
                "✅ Order updated."
            ),
            reply_markup=main_keyboard(role),
            disable_notification=True,
        )

        return

    # =====================================================
    # EDIT PRICE
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

            value = float(cleaned)

            if value <= 0:
                raise ValueError

        except ValueError:

            await update.message.reply_text(
                "Please enter a valid amount.\n"
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
                "This order can no longer be edited.",
                reply_markup=main_keyboard(role),
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
            "✅ Price updated.",
            reply_markup=main_keyboard(role),
            disable_notification=True,
        )

        return


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
    # SKIP CLIENT PRICE
    # =====================================================

    if data == "skip_new_price":

        state = user_state.get(
            user.id
        )

        if (
            not state
            or state.get("action") != "new_price"
        ):

            await query.answer(
                "This step has expired.",
                show_alert=True,
            )

            return

        await query.answer()

        order_id = await create_order(
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
            "💶 Offer: Negotiable\n\n"
            "Your request has been sent "
            "to our drivers.\n\n"
            "You will be notified when "
            "a driver accepts your trip.",
            parse_mode="HTML",
        )

        await context.bot.send_message(
            chat_id=user.id,
            text="Use the buttons below.",
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
    order_id = int(parts[1])

    order = get_order(
        order_id
    )

    if not order:

        await query.answer(
            "Η διαδρομή δεν βρέθηκε.",
            show_alert=True,
        )

        return

    # =====================================================
    # TAKE ORDER
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

        if (
            order["creator_role"] == "driver"
            and order["creator_id"] == user.id
        ):

            await query.answer(
                "Δεν μπορείς να πάρεις "
                "τη δική σου διαδρομή.",
                show_alert=True,
            )

            return

        async with db_lock:

            with db() as conn:

                current = conn.execute(
                    """
                    SELECT *
                    FROM orders
                    WHERE id = ?
                    """,
                    (order_id,),
                ).fetchone()

                if (
                    not current
                    or current["status"] != "open"
                ):

                    await query.answer(
                        "Η διαδρομή δεν είναι "
                        "πλέον διαθέσιμη.",
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

        await refresh_group_card(
            context,
            order_id,
        )

        updated = get_order(
            order_id
        )

        # Message to driver
        try:

            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "✅ <b>ΠΗΡΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
                    f"🔢 Αριθμός: #{order_id}\n\n"
                    f"{html.escape(updated['details'])}\n\n"
                    f"👤 Από: "
                    f"{html.escape(updated['creator_name'])}"
                ),
                parse_mode="HTML",
                reply_markup=(
                    accepted_driver_keyboard(
                        updated
                    )
                ),
                disable_notification=True,
            )

        except Exception:

            logger.exception(
                "Could not message taker"
            )

        # Message to creator/client
        try:

            if updated["creator_role"] == "client":

                creator_text = (
                    "✅ <b>DRIVER FOUND!</b>\n\n"
                    f"🚕 Request: #{order_id}\n"
                    f"👤 Driver: "
                    f"{html.escape(person_name(user))}\n\n"
                    "Your driver has accepted "
                    "your trip."
                )

            else:

                creator_text = (
                    "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n\n"
                    f"🔢 Αριθμός: #{order_id}\n"
                    f"🚖 Οδηγός: "
                    f"{html.escape(person_name(user))}"
                )

            await context.bot.send_message(
                chat_id=updated["creator_id"],
                text=creator_text,
                parse_mode="HTML",
                reply_markup=(
                    accepted_creator_keyboard(
                        updated
                    )
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

            await query.answer(
                "Δεν είναι δική σου διαδρομή.",
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
            order_card(order),
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

            await query.answer(
                "Δεν μπορεί να αλλάξει.",
                show_alert=True,
            )

            return

        user_state[user.id] = {
            "action": "edit_details",
            "order_id": order_id,
        }

        await query.answer()

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "✏️ <b>CHANGE TRIP DETAILS</b>\n\n"
                f"Send the NEW details "
                f"for order #{order_id} "
                "in one message."
            )
            if order["creator_role"] == "client"
            else (
                "✏️ <b>ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ</b>\n\n"
                f"Στείλε τα ΝΕΑ στοιχεία "
                f"για τη διαδρομή #{order_id} "
                "σε ένα μήνυμα."
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
            disable_notification=True,
        )

        return

    # =====================================================
    # EDIT PRICE
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
                "Price cannot be changed.",
                show_alert=True,
            )

            return

        user_state[user.id] = {
            "action": "edit_price",
            "order_id": order_id,
        }

        await query.answer()

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "💶 <b>CHANGE PRICE</b>\n\n"
                f"Send the new price for "
                f"order #{order_id}.\n"
                "Example: 50"
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
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

            await query.answer(
                "Η διαδρομή έχει ήδη κλείσει.",
                show_alert=True,
            )

            return

        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=confirm_cancel_keyboard(
                order_id
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

            await query.answer(
                "Η διαδρομή έχει ήδη κλείσει.",
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
                    (order_id,),
                )

                conn.commit()

        await query.answer(
            "Order cancelled."
            if order["creator_role"] == "client"
            else
            "Η διαδρομή ακυρώθηκε."
        )

        await refresh_group_card(
            context,
            order_id,
        )

        if order["creator_role"] == "client":

            await query.edit_message_text(
                f"❌ Request #{order_id} cancelled."
            )

        else:

            await query.edit_message_text(
                f"❌ Η διαδρομή #{order_id} ακυρώθηκε."
            )

        if taker_id:

            try:

                await context.bot.send_message(
                    chat_id=taker_id,
                    text=(
                        "❌ <b>Η ΔΙΑΔΡΟΜΗ ΑΚΥΡΩΘΗΚΕ</b>\n\n"
                        f"Η διαδρομή #{order_id} "
                        "ακυρώθηκε από αυτόν "
                        "που την έδωσε."
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
    # SHOW TAKEN
    # =====================================================

    if action == "taken":

        await query.answer()

        await query.edit_message_text(
            "📦 <b>ΔΙΑΔΡΟΜΗ ΠΟΥ ΠΗΡΑ</b>\n\n"
            + order_card(order),
            parse_mode="HTML",
            reply_markup=(
                accepted_driver_keyboard(
                    order
                )
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

        await query.answer(
            "Η διαδρομή επέστρεψε."
        )

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

        try:

            if order["creator_role"] == "client":

                creator_text = (
                    "🔎 <b>LOOKING FOR ANOTHER DRIVER</b>\n\n"
                    f"Your driver released "
                    f"request #{order_id}.\n\n"
                    "Your request is available "
                    "to our drivers again."
                )

            else:

                creator_text = (
                    "↩️ <b>Η ΔΙΑΔΡΟΜΗ ΕΙΝΑΙ "
                    "ΞΑΝΑ ΔΙΑΘΕΣΙΜΗ</b>\n\n"
                    f"Ο οδηγός άφησε τη "
                    f"διαδρομή #{order_id}."
                )

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
# DRIVER GROUP HANDLER
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

    # Permanent driver button
    if text in (
        BTN_NEW_DRIVER,
        OLD_NEW_DRIVER,
    ):

        # Delete technical button message immediately.
        try:
            await update.message.delete()
        except Exception:
            pass

        if not await is_driver(
            user.id,
            context,
        ):
            return

        # Everything continues PRIVATELY.
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
                "Could not start private driver flow "
                "for driver %s",
                user.id,
            )

        return

    # Admins may write normally.
    if await is_admin_in_chat(
        DRIVERS_GROUP_ID,
        user.id,
        context,
    ):
        return

    # Delete ordinary driver messages.
    try:
        await update.message.delete()
    except Exception:
        pass


# =========================================================
# CLIENT GROUP HANDLER
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

    # =====================================================
    # PERMANENT REQUEST A TAXI BUTTON
    # =====================================================

    if text == BTN_NEW_CLIENT:

        # Delete technical button message immediately
        # so the public client group stays clean.
        try:
            await update.message.delete()
        except Exception:
            pass

        # From this point everything goes privately.
        try:

            await begin_new_order(
                update,
                context,
                "client",
            )

        except Exception:

            # Most common reason:
            # brand-new user has never started the bot.
            #
            # We deliberately do NOT post an error
            # into the public group.
            user_state.pop(
                user.id,
                None,
            )

            logger.info(
                "Could not privately message client %s. "
                "They probably need to open/start the bot first.",
                user.id,
            )

        return

    # =====================================================
    # ADMINS MAY WRITE NORMALLY
    # =====================================================

    if await is_admin_in_chat(
        CLIENTS_GROUP_ID,
        user.id,
        context,
    ):
        return

    # =====================================================
    # ORDINARY CLIENT MESSAGES ARE REMOVED
    # =====================================================

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

    if update.effective_chat.id not in (
        DRIVERS_GROUP_ID,
        CLIENTS_GROUP_ID,
    ):
        return

    if not update.effective_user:
        return

    # Admin commands are handled separately.
    if await is_admin_in_chat(
        update.effective_chat.id,
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

    # Private bot messages
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text,
        )
    )

    # Driver group
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

    # Client group
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

    # Clean non-admin commands from our groups
    app.add_handler(
        MessageHandler(
            (
                filters.Chat(
                    DRIVERS_GROUP_ID
                )
                |
                filters.Chat(
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
