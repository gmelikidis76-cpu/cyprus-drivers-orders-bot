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

TOKEN = os.getenv("BOT_TOKEN")
DRIVERS_GROUP_ID = -1004449292276
DB_PATH = os.getenv("DB_PATH", "/data/orders.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()
user_state = {}

BTN_NEW_DRIVER = "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"
BTN_MY_ORDER = "📋 MY ORDER"
BTN_TAKEN = "📦 MY TAKEN ORDER"
BTN_CANCEL = "❌ CANCEL"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute("""
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
        """)
        conn.commit()


def person_name(user):
    return user.full_name or user.first_name or "Telegram user"


def main_keyboard(role):
    if role == "driver":
        return ReplyKeyboardMarkup(
            [
                [BTN_NEW_DRIVER],
                [BTN_MY_ORDER, BTN_TAKEN],
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


def cancel_keyboard(role):
    return ReplyKeyboardMarkup(
        [[BTN_CANCEL]],
        resize_keyboard=True,
        is_persistent=True,
    )


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


async def is_admin(user_id, context):
    try:
        member = await context.bot.get_chat_member(
            DRIVERS_GROUP_ID,
            user_id,
        )
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def role_for(user_id, context):
    if await is_driver(user_id, context):
        return "driver"
    return "client"


def open_order_keyboard(order_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🚕 ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ 🚕",
                callback_data=f"take:{order_id}",
            )
        ]]
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


def give_up_keyboard(order_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "↩️ GIVE UP ORDER",
                callback_data=f"giveup:{order_id}",
            )
        ]]
    )


def confirm_cancel_keyboard(order_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ YES, CANCEL",
                callback_data=f"confirm_cancel:{order_id}",
            ),
            InlineKeyboardButton(
                "↩️ BACK",
                callback_data=f"manage:{order_id}",
            ),
        ]]
    )


def confirm_giveup_keyboard(order_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ YES, GIVE UP",
                callback_data=f"confirm_giveup:{order_id}",
            ),
            InlineKeyboardButton(
                "↩️ BACK",
                callback_data=f"taken:{order_id}",
            ),
        ]]
    )


def order_card(order):
    details = html.escape(order["details"])
    creator = html.escape(order["creator_name"])
    number = order["id"]

    if order["creator_role"] == "client":
        price = order["price"]

        if price:
            price_line = f"€{html.escape(price)}"
        else:
            price_line = "🤝 Negotiable"

        body = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ — ΠΕΛΑΤΗΣ</b>\n\n"
            f"{details}\n\n"
            f"💶 Προσφορά πελάτη: <b>{price_line}</b>\n"
            f"👤 Πελάτης: {creator}\n"
            f"🔢 Αριθμός: #{number}"
        )

    else:
        body = (
            "🚕 <b>ΝΕΑ ΔΙΑΔΡΟΜΗ</b>\n\n"
            f"{details}\n\n"
            f"👤 Από: {creator}\n"
            f"🔢 Αριθμός: #{number}"
        )

    if order["status"] == "taken":
        taker = html.escape(order["taker_name"] or "Driver")

        body += (
            "\n\n"
            "✅ <b>Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ</b>\n"
            f"🚖 Οδηγός: {taker}"
        )

    elif order["status"] == "cancelled":
        body += "\n\n❌ <b>ΑΚΥΡΩΘΗΚΕ</b>"

    elif order["status"] == "closed":
        body += "\n\n🏁 <b>ΟΛΟΚΛΗΡΩΘΗΚΕ</b>"

    return body


def get_order(order_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()


def get_creator_active_order(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM orders
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
            SELECT * FROM orders
            WHERE taker_id = ?
            AND status = 'taken'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


async def refresh_group_card(context, order_id):
    order = get_order(order_id)

    if not order:
        return

    if not order["group_message_id"]:
        return

    if order["status"] == "open":
        markup = open_order_keyboard(order_id)
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
        logger.exception("Could not refresh group order card")


async def create_order(
    context,
    user,
    role,
    details,
    price=None,
):
    async with db_lock:
        with db() as conn:
            cur = conn.execute(
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

            order_id = cur.lastrowid
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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.effective_chat:
        return

    if update.effective_chat.type != "private":
        if update.effective_chat.id == DRIVERS_GROUP_ID:
            try:
                await update.effective_message.delete()
            except Exception:
                pass
        return

    payload = ""

    if context.args:
        payload = context.args[0].lower()

    if payload == "client":
        role = "client"

    elif payload == "driver":
        allowed = await is_driver(
            update.effective_user.id,
            context,
        )

        if not allowed:
            await update.message.reply_text(
                "⛔ Driver access is available only to members "
                "of the drivers group."
            )
            return

        role = "driver"

    else:
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
            "To create a trip, tap "
            "<b>ΝΕΑ ΔΙΑΔΡΟΜΗ</b>.\n\n"
            "New customer orders will appear "
            "in the drivers group."
        )

    else:
        text = (
            "🚕 <b>CYPRUS TAXI</b>\n\n"
            "To request a taxi, tap "
            "<b>REQUEST A TAXI</b>."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(role),
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_chat:
        await update.effective_message.reply_text(
            f"Chat ID: {update.effective_chat.id}"
        )


async def begin_new_order(
    update,
    context,
    role,
):
    user = update.effective_user

    active = get_creator_active_order(user.id)

    if active:
        await update.message.reply_text(
            f"⚠️ You already have active order "
            f"#{active['id']}.\n\n"
            "Open 📋 MY ORDER to edit or cancel it.",
            reply_markup=main_keyboard(role),
        )
        return

    user_state[user.id] = {
        "action": "new_details",
        "role": role,
    }

    prompt = (
        "🚕 <b>NEW TRIP</b>\n\n"
        "Send the trip details in ONE message.\n\n"
        "Example:\n"
        "Larnaca Airport → Limassol\n"
        "18:30, 2 passengers"
    )

    await update.message.reply_text(
        prompt,
        parse_mode="HTML",
        reply_markup=cancel_keyboard(role),
    )


async def show_my_order(
    update,
    context,
    user_id,
    role,
):
    order = get_creator_active_order(user_id)

    if not order:
        await update.message.reply_text(
            "📋 You have no active order.",
            reply_markup=main_keyboard(role),
        )
        return

    await update.message.reply_text(
        order_card(order),
        parse_mode="HTML",
        reply_markup=creator_manage_keyboard(order),
    )


async def show_taken_order(
    update,
    context,
    user_id,
):
    order = get_taken_order(user_id)

    if not order:
        await update.message.reply_text(
            "📦 You have no accepted order.",
            reply_markup=main_keyboard("driver"),
        )
        return

    await update.message.reply_text(
        "📦 <b>YOUR ACCEPTED ORDER</b>\n\n"
        + order_card(order),
        parse_mode="HTML",
        reply_markup=give_up_keyboard(order["id"]),
    )


async def private_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    user = update.effective_user
    text = (update.message.text or "").strip()

    role = await role_for(
        user.id,
        context,
    )

    if text == BTN_CANCEL:
        user_state.pop(user.id, None)

        await update.message.reply_text(
            "↩️ Cancelled.",
            reply_markup=main_keyboard(role),
        )
        return

    if text == BTN_NEW_CLIENT:
        await begin_new_order(
            update,
            context,
            "client",
        )
        return

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

    if text == BTN_MY_ORDER:
        await show_my_order(
            update,
            context,
            user.id,
            role,
        )
        return

    if text == BTN_TAKEN:
        if role == "driver":
            await show_taken_order(
                update,
                context,
                user.id,
            )
        return

    state = user_state.get(user.id)

    if not state:
        await update.message.reply_text(
            "Please use the buttons below.",
            reply_markup=main_keyboard(role),
        )
        return

    action = state["action"]

    if action == "new_details":
        if len(text) < 3:
            await update.message.reply_text(
                "Please send the trip details."
            )
            return

        state["details"] = text

        if state["role"] == "client":
            state["action"] = "new_price"

            await update.message.reply_text(
                "💶 <b>HOW MUCH ARE YOU WILLING TO PAY?</b>\n\n"
                "Send the amount in EUR, for example: <b>50</b>\n\n"
                "or tap <b>SKIP / NOT SURE</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "🤝 SKIP / NOT SURE",
                            callback_data="skip_new_price",
                        )
                    ]]
                ),
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
                f"✅ Trip #{order_id} published.",
                reply_markup=main_keyboard("driver"),
            )

        return

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
                "Please enter a valid amount, "
                "for example: 50"
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
            "✅ <b>REQUEST SENT</b>\n\n"
            f"Order #{order_id} is now visible to drivers.",
            parse_mode="HTML",
            reply_markup=main_keyboard("client"),
        )
        return

    if action == "edit_details":
        order_id = state["order_id"]
        order = get_order(order_id)

        if (
            not order
            or order["creator_id"] != user.id
            or order["status"] not in ("open", "taken")
        ):
            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "This order can no longer be edited."
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

        user_state.pop(
            user.id,
            None,
        )

        order = get_order(order_id)

        if (
            order["status"] == "taken"
            and order["taker_id"]
        ):
            try:
                await context.bot.send_message(
                    order["taker_id"],
                    f"⚠️ Order #{order_id} was changed "
                    "by its creator.\n\n"
                    f"{text}",
                )
            except Exception:
                pass

        await update.message.reply_text(
            "✅ Order details updated.",
            reply_markup=main_keyboard(role),
        )
        return

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
                "Please enter a valid amount, "
                "for example: 50"
            )
            return

        order = get_order(order_id)

        if (
            not order
            or order["creator_id"] != user.id
            or order["status"] not in ("open", "taken")
        ):
            user_state.pop(
                user.id,
                None,
            )

            await update.message.reply_text(
                "This order can no longer be edited."
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

        user_state.pop(
            user.id,
            None,
        )

        if (
            order["status"] == "taken"
            and order["taker_id"]
        ):
            try:
                await context.bot.send_message(
                    order["taker_id"],
                    f"⚠️ Price for order #{order_id} "
                    f"was changed to €{price}.",
                )
            except Exception:
                pass

        await update.message.reply_text(
            "✅ Price updated.",
            reply_markup=main_keyboard(role),
        )


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

    if data == "skip_new_price":
        state = user_state.get(user.id)

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
            "✅ <b>REQUEST SENT</b>\n\n"
            f"Order #{order_id} is now visible to drivers.",
            parse_mode="HTML",
        )

        await context.bot.send_message(
            user.id,
            "Use the buttons below for your order.",
            reply_markup=main_keyboard("client"),
        )
        return

    parts = data.split(":", 1)

    if (
        len(parts) != 2
        or not parts[1].isdigit()
    ):
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
            order["creator_id"] == user.id
            and order["creator_role"] == "driver"
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
                    or current["status"] != "open"
                ):
                    await query.answer(
                        "This trip is no longer available.",
                        show_alert=True,
                    )
                    return

                conn.execute(
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

                conn.commit()

        await query.answer(
            "✅ Trip accepted!"
        )

        await refresh_group_card(
            context,
            order_id,
        )

        order = get_order(order_id)

        creator_link = (
            f'<a href="tg://user?id={order["creator_id"]}">'
            f'{html.escape(order["creator_name"])}'
            "</a>"
        )

        try:
            await context.bot.send_message(
                user.id,
                "✅ <b>YOU ACCEPTED THE ORDER</b>\n\n"
                f"Order: #{order_id}\n"
                f"Customer/creator: {creator_link}\n\n"
                f"{html.escape(order['details'])}",
                parse_mode="HTML",
                reply_markup=give_up_keyboard(order_id),
            )
        except Exception:
            pass

        driver_link = (
            f'<a href="tg://user?id={user.id}">'
            f"{html.escape(person_name(user))}"
            "</a>"
        )

        try:
            await context.bot.send_message(
                order["creator_id"],
                "✅ <b>A DRIVER ACCEPTED YOUR ORDER</b>\n\n"
                f"Order: #{order_id}\n"
                f"Driver: {driver_link}",
                parse_mode="HTML",
            )
        except Exception:
            pass

        return

    if action in (
        "manage",
        "edit_details",
        "edit_price",
        "cancel_order",
        "confirm_cancel",
    ):
        if order["creator_id"] != user.id:
            await query.answer(
                "This is not your order.",
                show_alert=True,
            )
            return

    if action == "manage":
        await query.answer()

        await query.edit_message_text(
            order_card(order),
            parse_mode="HTML",
            reply_markup=creator_manage_keyboard(order),
        )
        return

    if action == "edit_details":
        if order["status"] not in (
            "open",
            "taken",
        ):
            await query.answer(
                "This order cannot be edited.",
                show_alert=True,
            )
            return

        user_state[user.id] = {
            "action": "edit_details",
            "order_id": order_id,
        }

        await query.answer()

        role = await role_for(
            user.id,
            context,
        )

        await context.bot.send_message(
            user.id,
            f"✏️ Send the NEW details for "
            f"order #{order_id} in one message.",
            reply_markup=cancel_keyboard(role),
        )
        return

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

        role = await role_for(
            user.id,
            context,
        )

        await context.bot.send_message(
            user.id,
            f"💶 Send the NEW price for "
            f"order #{order_id}. Example: 50",
            reply_markup=cancel_keyboard(role),
        )
        return

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
            reply_markup=confirm_cancel_keyboard(
                order_id
            )
        )
        return

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
                    taker_id,
                    f"❌ Order #{order_id} was cancelled "
                    "by its creator.",
                )
            except Exception:
                pass

        return

    if action in (
        "taken",
        "giveup",
        "confirm_giveup",
    ):
        if (
            order["taker_id"] != user.id
            or order["status"] != "taken"
        ):
            await query.answer(
                "This is no longer your accepted order.",
                show_alert=True,
            )
            return

    if action == "taken":
        await query.answer()

        await query.edit_message_text(
            "📦 <b>YOUR ACCEPTED ORDER</b>\n\n"
            + order_card(order),
            parse_mode="HTML",
            reply_markup=give_up_keyboard(
                order_id
            ),
        )
        return

    if action == "giveup":
        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=confirm_giveup_keyboard(
                order_id
            )
        )
        return

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

        await refresh_group_card(
            context,
            order_id,
        )

        await query.edit_message_text(
            f"↩️ Order #{order_id} returned "
            "to the drivers group."
        )

        try:
            await context.bot.send_message(
                order["creator_id"],
                f"↩️ The driver gave up order "
                f"#{order_id}.\n"
                "The order is available to drivers again.",
            )
        except Exception:
            pass

        return

    await query.answer()


async def driver_group_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if not update.message:
        return

    if await is_admin(
        update.effective_user.id,
        context,
    ):
        return

    try:
        await update.message.delete()
    except Exception:
        pass


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.exception(
        "Unhandled exception",
        exc_info=context.error,
    )


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
            filters.Chat(DRIVERS_GROUP_ID)
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
