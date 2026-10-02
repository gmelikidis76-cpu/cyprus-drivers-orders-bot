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
BOT_USERNAME = "CyprusDriversOrdersBot"
DB_PATH = os.getenv("DB_PATH", "/data/orders.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()

# Temporary state for conversations.
# Orders themselves are stored permanently in SQLite.
user_state = {}


# =========================================================
# BUTTON TEXT
# =========================================================

BTN_NEW_DRIVER = "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"
BTN_MY_ORDER = "📋 MY ORDER"
BTN_TAKEN = "📦 MY TAKEN ORDER"
BTN_CANCEL = "❌ CANCEL"


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


def get_creator_active_order(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE creator_id = ?
              AND status IN ('open', 'taken')
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def get_taken_order(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE taker_id = ?
              AND status = 'taken'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


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
# PRIVATE CHAT KEYBOARDS
# =========================================================

def main_keyboard(role):
    if role == "driver":
        return ReplyKeyboardMarkup(
            [
                [BTN_NEW_DRIVER],
                [
                    BTN_MY_ORDER,
                    BTN_TAKEN,
                ],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    return ReplyKeyboardMarkup(
        [
            [BTN_NEW_CLIENT],
            [BTN_MY_ORDER],
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
# BIG GROUP BUTTON
# =========================================================

def driver_panel_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚕🚕  ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ  🚕🚕",
                    url=(
                        f"https://t.me/{BOT_USERNAME}"
                        "?start=driver"
                    ),
                )
            ]
        ]
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
                "✏️ EDIT DETAILS",
                callback_data=f"edit_details:{order['id']}",
            )
        ]
    ]

    if order["creator_role"] == "client":
        rows.append(
            [
                InlineKeyboardButton(
                    "💶 CHANGE PRICE",
                    callback_data=f"edit_price:{order['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "❌ CANCEL ORDER",
                callback_data=f"cancel_order:{order['id']}",
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


def accepted_driver_keyboard(order):
    if order["creator_role"] == "client":
        contact_label = "💬 CONTACT CUSTOMER"
    else:
        contact_label = "💬 CONTACT CREATOR"

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
                    "↩️ GIVE UP ORDER",
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
                "✏️ EDIT DETAILS",
                callback_data=f"edit_details:{order['id']}",
            )
        ]
    )

    if order["creator_role"] == "client":
        rows.append(
            [
                InlineKeyboardButton(
                    "💶 CHANGE PRICE",
                    callback_data=f"edit_price:{order['id']}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "❌ CANCEL ORDER",
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
                    "✅ YES, CANCEL",
                    callback_data=f"confirm_cancel:{order_id}",
                ),
                InlineKeyboardButton(
                    "↩️ BACK",
                    callback_data=f"manage:{order_id}",
                ),
            ]
        ]
    )


def confirm_giveup_keyboard(order_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ YES, GIVE UP",
                    callback_data=f"confirm_giveup:{order_id}",
                ),
                InlineKeyboardButton(
                    "↩️ BACK",
                    callback_data=f"taken:{order_id}",
                ),
            ]
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
            price_line = "🤝 Negotiable"

        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ</b>\n\n"
            f"{details}\n\n"
            f"💶 Προσφορά πελάτη: "
            f"<b>{price_line}</b>\n"
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
            "❌ <b>ΑΚΥΡΩΘΗΚΕ</b>"
        )

    return text


# =========================================================
# UPDATE EXISTING GROUP CARD
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

    # Only the finished order is sent to the drivers group.
    # This is intentionally a normal notification.
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
# BEGIN NEW ORDER
# =========================================================

async def begin_new_order(
    update,
    context,
    role,
):
    user = update.effective_user

    active = get_creator_active_order(
        user.id
    )

    if active:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ <b>YOU ALREADY HAVE "
                "AN ACTIVE ORDER</b>\n\n"
                f"Order: #{active['id']}\n\n"
                "Use 📋 MY ORDER to change "
                "or cancel it before creating "
                "another one."
            ),
            parse_mode="HTML",
            reply_markup=main_keyboard(role),
        )
        return

    user_state[user.id] = {
        "action": "new_details",
        "role": role,
    }

    if role == "driver":
        text = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Γράψε όλα τα στοιχεία της διαδρομής "
            "σε ΕΝΑ μήνυμα.\n\n"
            "Παράδειγμα:\n"
            "Larnaca Airport → Limassol\n"
            "23:00\n"
            "2 passengers\n"
            "€70"
        )

    else:
        text = (
            "🚕 <b>REQUEST A TAXI</b>\n\n"
            "Send your trip details "
            "in ONE message.\n\n"
            "Example:\n"
            "Larnaca Airport → Limassol\n"
            "18:30\n"
            "2 passengers"
        )

    await context.bot.send_message(
        chat_id=user.id,
        text=text,
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
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

    # No normal /start workflow inside groups.
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

    # Driver pressed the big button in the drivers group.
    if payload == "driver":
        allowed = await is_driver(
            update.effective_user.id,
            context,
        )

        if not allowed:
            await update.message.reply_text(
                "⛔ Driver access is available "
                "only to members of the drivers group."
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

    # Client deep link.
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

    # Normal private /start.
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
            "🚕 <b>DRIVER MENU</b>\n\n"
            "To give a trip to another driver, "
            "tap <b>ΝΕΑ ΔΙΑΔΡΟΜΗ</b>.\n\n"
            "To take an available trip, "
            "use the drivers group."
        )

    else:
        text = (
            "🚕 <b>CYPRUS TAXI</b>\n\n"
            "Need a taxi?\n"
            "Tap <b>REQUEST A TAXI</b>."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(role),
    )


# =========================================================
# /PANEL
# ADMIN USES THIS ONCE IN THE DRIVERS GROUP
# =========================================================

async def panel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.effective_chat:
        return

    if (
        update.effective_chat.id
        != DRIVERS_GROUP_ID
    ):
        return

    if not await is_admin(
        update.effective_user.id,
        context,
    ):
        try:
            await update.effective_message.delete()
        except Exception:
            pass

        return

    # Remove the /panel command itself.
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    panel = await context.bot.send_message(
        chat_id=DRIVERS_GROUP_ID,
        text=(
            "🚕🚕🚕 "
            "<b>ΟΔΗΓΟΙ — ΝΕΑ ΔΙΑΔΡΟΜΗ</b> "
            "🚕🚕🚕\n\n"
            "Θέλεις να δώσεις μια νέα διαδρομή "
            "σε άλλον οδηγό;\n\n"
            "👇 <b>ΠΑΤΗΣΕ ΤΟ ΜΕΓΑΛΟ ΚΟΥΜΠΙ "
            "ΠΑΡΑΚΑΤΩ</b> 👇"
        ),
        parse_mode="HTML",
        reply_markup=driver_panel_keyboard(),

        # Creating the panel itself should not make
        # a notification sound for everyone.
        disable_notification=True,
    )

    # Try to pin the panel automatically.
    # Bot must have permission to pin messages.
    try:
        await context.bot.pin_chat_message(
            chat_id=DRIVERS_GROUP_ID,
            message_id=panel.message_id,
            disable_notification=True,
        )
    except Exception:
        logger.exception(
            "Could not automatically pin driver panel"
        )


# =========================================================
# /ID
# =========================================================

async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_chat:
        await update.effective_message.reply_text(
            f"Chat ID: "
            f"{update.effective_chat.id}"
        )


# =========================================================
# SHOW CREATOR'S ORDER
# =========================================================

async def show_my_order(
    update,
    context,
    user_id,
    role,
):
    order = get_creator_active_order(
        user_id
    )

    if not order:
        await update.message.reply_text(
            "📋 You have no active order.",
            reply_markup=main_keyboard(role),
        )
        return

    if order["status"] == "taken":
        markup = accepted_creator_keyboard(
            order
        )
    else:
        markup = creator_manage_keyboard(
            order
        )

    await update.message.reply_text(
        order_card(order),
        parse_mode="HTML",
        reply_markup=markup,
    )


# =========================================================
# SHOW TAKEN ORDER
# =========================================================

async def show_taken_order(
    update,
    context,
    user_id,
):
    order = get_taken_order(
        user_id
    )

    if not order:
        await update.message.reply_text(
            "📦 You have no accepted order.",
            reply_markup=main_keyboard(
                "driver"
            ),
        )
        return

    await update.message.reply_text(
        "📦 <b>YOUR ACCEPTED ORDER</b>\n\n"
        + order_card(order),
        parse_mode="HTML",
        reply_markup=accepted_driver_keyboard(
            order
        ),
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

    role = await role_for(
        user.id,
        context,
    )

    # Cancel current operation.
    if text == BTN_CANCEL:
        user_state.pop(
            user.id,
            None,
        )

        await update.message.reply_text(
            "↩️ Cancelled.",
            reply_markup=main_keyboard(role),
        )
        return

    # Client creates taxi request.
    if text == BTN_NEW_CLIENT:
        await begin_new_order(
            update,
            context,
            "client",
        )
        return

    # Driver gives trip to another driver.
    if text == BTN_NEW_DRIVER:
        if role != "driver":
            await update.message.reply_text(
                "⛔ Driver access only."
            )
            return

        await begin_new_order(
            update,
            context,
            "driver",
        )
        return

    # Creator manages active order.
    if text == BTN_MY_ORDER:
        await show_my_order(
            update,
            context,
            user.id,
            role,
        )
        return

    # Driver manages accepted order.
    if text == BTN_TAKEN:
        if role == "driver":
            await show_taken_order(
                update,
                context,
                user.id,
            )
        return

    state = user_state.get(
        user.id
    )

    if not state:
        await update.message.reply_text(
            "Please use the buttons below.",
            reply_markup=main_keyboard(role),
        )
        return

    action = state["action"]

    # =====================================================
    # NEW ORDER DETAILS
    # =====================================================

    if action == "new_details":
        if len(text) < 3:
            await update.message.reply_text(
                "Please send the trip details."
            )
            return

        state["details"] = text

        # Client must now enter price.
        if state["role"] == "client":
            state["action"] = "new_price"

            await update.message.reply_text(
                "💶 <b>HOW MUCH ARE YOU "
                "WILLING TO PAY?</b>\n\n"
                "Send your offer in EUR.\n"
                "Example: <b>50</b>\n\n"
                "If you are not sure, "
                "tap the button below.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🤝 SKIP / NOT SURE",
                                callback_data=(
                                    "skip_new_price"
                                ),
                            )
                        ]
                    ]
                ),
            )

        # Driver trip is ready immediately.
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
                f"Order #{order_id} is now "
                "visible in the drivers group.",
                parse_mode="HTML",
                reply_markup=main_keyboard(
                    "driver"
                ),
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
                "Example: 50"
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
            "to our drivers.",
            parse_mode="HTML",
            reply_markup=main_keyboard(
                "client"
            ),
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
            or order["status"]
            not in ("open", "taken")
        ):
            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "This order can no longer "
                "be edited.",
                reply_markup=main_keyboard(role),
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

        # If somebody already took it,
        # tell that driver privately.
        if (
            updated["status"] == "taken"
            and updated["taker_id"]
        ):
            try:
                await context.bot.send_message(
                    chat_id=updated["taker_id"],
                    text=(
                        "⚠️ <b>ORDER UPDATED</b>\n\n"
                        f"Order #{order_id} "
                        "was changed by its creator.\n\n"
                        f"{html.escape(text)}"
                    ),
                    parse_mode="HTML",
                    reply_markup=(
                        accepted_driver_keyboard(
                            updated
                        )
                    ),
                )

            except Exception:
                logger.exception(
                    "Could not notify taker "
                    "about order edit"
                )

        await update.message.reply_text(
            "✅ Order details updated.",
            reply_markup=main_keyboard(role),
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
                "Example: 50"
            )
            return

        order = get_order(
            order_id
        )

        if (
            not order
            or order["creator_id"] != user.id
            or order["creator_role"] != "client"
            or order["status"]
            not in ("open", "taken")
        ):
            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "This order can no longer "
                "be edited.",
                reply_markup=main_keyboard(role),
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
                        "⚠️ <b>PRICE CHANGED</b>\n\n"
                        f"Order #{order_id}\n"
                        f"New price: €{price}"
                    ),
                    parse_mode="HTML",
                    reply_markup=(
                        accepted_driver_keyboard(
                            updated
                        )
                    ),
                )

            except Exception:
                logger.exception(
                    "Could not notify driver "
                    "about price change"
                )

        await update.message.reply_text(
            "✅ Price updated.",
            reply_markup=main_keyboard(role),
        )

        return


# =========================================================
# CALLBACK BUTTONS
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
            or state.get("action")
            != "new_price"
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
            "to our drivers.",
            parse_mode="HTML",
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "Use the buttons below "
                "to manage your order."
            ),
            reply_markup=main_keyboard(
                "client"
            ),
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
            "Order not found.",
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
                "Drivers only.",
                show_alert=True,
            )
            return

        if (
            order["creator_role"] == "driver"
            and order["creator_id"] == user.id
        ):
            await query.answer(
                "You cannot take your own trip.",
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
                    or current["status"]
                    != "open"
                ):
                    await query.answer(
                        "This trip is no longer "
                        "available.",
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
                        "Another driver already "
                        "took this trip.",
                        show_alert=True,
                    )
                    return

                conn.commit()

        await query.answer(
            "✅ Trip accepted!"
        )

        await refresh_group_card(
            context,
            order_id,
        )

        updated = get_order(
            order_id
        )

        # -------------------------------------
        # PRIVATE MESSAGE TO DRIVER
        # -------------------------------------

        try:
            if (
                updated["creator_role"]
                == "client"
            ):
                creator_type = "CUSTOMER"
            else:
                creator_type = "CREATOR"

            username_text = ""

            if updated["creator_username"]:
                username_text = (
                    "\nUsername: @"
                    f"{html.escape(updated['creator_username'])}"
                )

            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "✅ <b>YOU ACCEPTED "
                    "THE ORDER</b>\n\n"
                    f"🔢 Order: #{order_id}\n"
                    f"👤 {creator_type}: "
                    f"{html.escape(updated['creator_name'])}"
                    f"{username_text}\n\n"
                    f"{html.escape(updated['details'])}"
                ),
                parse_mode="HTML",
                reply_markup=(
                    accepted_driver_keyboard(
                        updated
                    )
                ),
            )

        except Exception:
            logger.exception(
                "Could not send accepted "
                "order to driver"
            )

        # -------------------------------------
        # PRIVATE MESSAGE TO CREATOR
        # -------------------------------------

        try:
            driver_username = ""

            if user.username:
                driver_username = (
                    "\nUsername: @"
                    f"{html.escape(user.username)}"
                )

            if (
                updated["creator_role"]
                == "client"
            ):
                title = (
                    "🚖 <b>DRIVER FOUND</b>"
                )
            else:
                title = (
                    "✅ <b>YOUR TRIP "
                    "WAS TAKEN</b>"
                )

            await context.bot.send_message(
                chat_id=updated["creator_id"],
                text=(
                    f"{title}\n\n"
                    f"🔢 Order: #{order_id}\n"
                    f"🚖 Driver: "
                    f"{html.escape(person_name(user))}"
                    f"{driver_username}\n\n"
                    "Tap the button below "
                    "to contact the driver."
                ),
                parse_mode="HTML",
                reply_markup=(
                    accepted_creator_keyboard(
                        updated
                    )
                ),
            )

        except Exception:
            logger.exception(
                "Could not notify creator "
                "about accepted order"
            )

        return

    # =====================================================
    # CREATOR-ONLY ACTIONS
    # =====================================================

    creator_actions = {
        "manage",
        "edit_details",
        "edit_price",
        "cancel_order",
        "confirm_cancel",
    }

    if action in creator_actions:
        if (
            order["creator_id"]
            != user.id
        ):
            await query.answer(
                "This is not your order.",
                show_alert=True,
            )
            return

    # -----------------------------------------------------
    # MANAGE
    # -----------------------------------------------------

    if action == "manage":
        await query.answer()

        if order["status"] == "taken":
            markup = (
                accepted_creator_keyboard(
                    order
                )
            )
        else:
            markup = (
                creator_manage_keyboard(
                    order
                )
            )

        await query.edit_message_text(
            order_card(order),
            parse_mode="HTML",
            reply_markup=markup,
        )

        return

    # -----------------------------------------------------
    # EDIT DETAILS
    # -----------------------------------------------------

    if action == "edit_details":
        if order["status"] not in (
            "open",
            "taken",
        ):
            await query.answer(
                "This order cannot "
                "be edited.",
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
                "✏️ <b>EDIT ORDER</b>\n\n"
                f"Send the NEW details "
                f"for order #{order_id} "
                "in one message."
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )

        return

    # -----------------------------------------------------
    # EDIT PRICE
    # -----------------------------------------------------

    if action == "edit_price":
        if (
            order["creator_role"]
            != "client"
            or order["status"]
            not in ("open", "taken")
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
                f"Send the NEW price for "
                f"order #{order_id}.\n"
                "Example: 50"
            ),
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )

        return

    # -----------------------------------------------------
    # ASK TO CANCEL
    # -----------------------------------------------------

    if action == "cancel_order":
        if order["status"] not in (
            "open",
            "taken",
        ):
            await query.answer(
                "This order is already closed.",
                show_alert=True,
            )
            return

        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=(
                confirm_cancel_keyboard(
                    order_id
                )
            )
        )

        return

    # -----------------------------------------------------
    # CONFIRM CANCEL
    # -----------------------------------------------------

    if action == "confirm_cancel":
        if order["status"] not in (
            "open",
            "taken",
        ):
            await query.answer(
                "This order is already closed.",
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
        )

        await refresh_group_card(
            context,
            order_id,
        )

        await query.edit_message_text(
            f"❌ Order #{order_id} cancelled."
        )

        if taker_id:
            try:
                await context.bot.send_message(
                    chat_id=taker_id,
                    text=(
                        "❌ <b>ORDER CANCELLED</b>\n\n"
                        f"Order #{order_id} "
                        "was cancelled by "
                        "its creator."
                    ),
                    parse_mode="HTML",
                )

            except Exception:
                logger.exception(
                    "Could not notify driver "
                    "about cancellation"
                )

        return

    # =====================================================
    # DRIVER WHO TOOK ORDER
    # =====================================================

    if action in (
        "taken",
        "giveup",
        "confirm_giveup",
    ):
        if (
            order["status"] != "taken"
            or order["taker_id"]
            != user.id
        ):
            await query.answer(
                "This is no longer "
                "your accepted order.",
                show_alert=True,
            )
            return

    # -----------------------------------------------------
    # SHOW TAKEN
    # -----------------------------------------------------

    if action == "taken":
        await query.answer()

        await query.edit_message_text(
            "📦 <b>YOUR ACCEPTED ORDER</b>\n\n"
            + order_card(order),
            parse_mode="HTML",
            reply_markup=(
                accepted_driver_keyboard(
                    order
                )
            ),
        )

        return

    # -----------------------------------------------------
    # ASK GIVE UP
    # -----------------------------------------------------

    if action == "giveup":
        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=(
                confirm_giveup_keyboard(
                    order_id
                )
            )
        )

        return

    # -----------------------------------------------------
    # CONFIRM GIVE UP
    # -----------------------------------------------------

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
                    or current["status"]
                    != "taken"
                    or current["taker_id"]
                    != user.id
                ):
                    await query.answer(
                        "Order state changed.",
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
            "Order returned."
        )

        # Existing card is edited.
        # No new group notification.
        await refresh_group_card(
            context,
            order_id,
        )

        await query.edit_message_text(
            "↩️ <b>ORDER RETURNED</b>\n\n"
            f"Order #{order_id} is available "
            "to drivers again.",
            parse_mode="HTML",
        )

        try:
            await context.bot.send_message(
                chat_id=order["creator_id"],
                text=(
                    "↩️ <b>DRIVER GAVE UP "
                    "THE ORDER</b>\n\n"
                    f"Order #{order_id} is "
                    "available to drivers again."
                ),
                parse_mode="HTML",
            )

        except Exception:
            logger.exception(
                "Could not notify creator "
                "about give up"
            )

        return

    await query.answer()


# =========================================================
# DRIVER GROUP MESSAGE CONTROL
# =========================================================

async def driver_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    # Administrators may write normally.
    if await is_admin(
        update.effective_user.id,
        context,
    ):
        return

    # Backup protection:
    # ordinary drivers' text is removed.
    #
    # We will additionally disable normal posting
    # for ordinary members in Telegram group permissions.
    try:
        await update.message.delete()

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

    app.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

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
            filters.Chat(
                DRIVERS_GROUP_ID
            )
            & filters.TEXT
            & ~filters.COMMAND,
            driver_group_text,
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
