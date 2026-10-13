import os
import sqlite3
import asyncio
import logging
import html
import time
import secrets

from decimal import Decimal, InvalidOperation
from contextlib import suppress, contextmanager
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.error import (
    Forbidden,
    BadRequest,
    RetryAfter,
    TelegramError,
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

# Всего личных уведомлений, включая первое.
ALERT_COUNT = 6

# Минимальный интервал между повторами по одному заказу.
ALERT_INTERVAL = 10.0

# Ограничение скорости рассылки.
ALERT_SEND_DELAY = 0.1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

db_lock = asyncio.Lock()
user_state = {}


# ==================== BUTTONS ====================

BTN_NEW_DRIVER = "➕ ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ"
BTN_DRIVER_ORDERS = "📋 ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ"
BTN_TAKEN = "📦 ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ"
BTN_CANCEL_DRIVER = "❌ ΑΚΥΡΩΣΗ"

BTN_NEW_CLIENT = "🚕 REQUEST A TAXI"
BTN_CLIENT_ORDERS = "📋 MY ORDERS"
BTN_CANCEL_CLIENT = "❌ CANCEL"

LEGACY_DRIVER_BUTTONS = (
    "🚕 ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ",
    "🚕 Δώσε διαδρομή",
)

OLD_NEW_DRIVER = "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ"
OLD_MY_ORDER = "📋 MY ORDER"
OLD_MY_ORDERS = "📋 MY ORDERS"
OLD_TAKEN = "📦 MY TAKEN ORDER"
OLD_TAKEN_ORDERS = "📦 MY TAKEN ORDERS"
OLD_CANCEL = "❌ CANCEL"


# ==================== DATABASE ====================

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
                "ALTER TABLE orders "
                "ADD COLUMN voice_group_message_id INTEGER"
            )

        if not column_exists(conn, "orders", "alert_generation"):
            conn.execute(
                "ALTER TABLE orders ADD COLUMN "
                "alert_generation INTEGER NOT NULL DEFAULT 0"
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_drivers (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_alerts (
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL,
                stopped INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (order_id, user_id)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS alerts_due
            ON order_alerts(stopped, next_at)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

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


def driver_can_receive(member):
    if member.status in ("member", "administrator", "creator"):
        return True

    return (
        member.status == "restricted"
        and member.is_member
        and member.can_send_messages
    )


async def is_driver(user_id, context):
    try:
        member = await context.bot.get_chat_member(
            DRIVERS_GROUP_ID,
            user_id,
        )
        return driver_can_receive(member)

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


def button(
    text,
    *,
    callback_data=None,
    url=None,
    color=None,
):
    options = (
        {"callback_data": callback_data}
        if callback_data
        else {"url": url}
    )

    if color:
        options["api_kwargs"] = {"style": color}

    return InlineKeyboardButton(text, **options)


def parse_price(text):
    value = Decimal(
        text.replace("€", "")
        .replace(",", ".")
        .strip()
    )

    if not value.is_finite() or value <= 0 or value > 100000:
        raise ValueError

    if value != value.quantize(Decimal("0.01")):
        raise ValueError

    return (
        format(value.quantize(Decimal("0.01")), "f")
        .rstrip("0")
        .rstrip(".")
    )


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
            if (
                chat
                and chat.type == "private"
                and driver_can_receive(member)
            ):
                with db() as conn:
                    conn.execute(
                        """
                        INSERT INTO alert_drivers(user_id, enabled)
                        VALUES (?, 1)
                        ON CONFLICT(user_id)
                        DO UPDATE SET enabled = 1
                        """,
                        (user.id,),
                    )
            return

        user_state.pop(user.id, None)

        message = (
            "⛔ Μόνο προβολή. Η πρόσβαση στις παραγγελίες "
            "έχει περιοριστεί από τον διαχειριστή.\n"
            "View only. Order actions are disabled "
            "by the administrator."
        )

    except Exception:
        logger.exception(
            "Could not check bot access for user %s",
            user.id,
        )

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
    label = (
        BTN_CANCEL_CLIENT
        if role == "client"
        else BTN_CANCEL_DRIVER
    )

    return ReplyKeyboardMarkup(
        [[label]],
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
    order = get_order(order_id)
    generation = order["alert_generation"] if order else 0

    return InlineKeyboardMarkup([
        [
            button(
                "✅ ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ",
                callback_data=f"take:{order_id}",
                color="success",
            ),
            button(
                "❌ ΔΕΝ ΕΝΔΙΑΦΕΡΟΜΑΙ",
                callback_data=(
                    f"group_refuse:{order_id}:{generation}"
                ),
                color="danger",
            ),
        ],
        [
            button(
                "➕ ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ",
                url=f"https://t.me/{BOT_USERNAME}?start=driver",
                color="primary",
            )
        ],
    ])


def management_rows(order, role):
    english = role == "client"
    order_id = order["id"]

    return [
        [
            InlineKeyboardButton(
                "✏️ CHANGE TRIP DETAILS"
                if english
                else "✏️ ΑΛΛΑΓΗ ΣΤΟΙΧΕΙΩΝ",
                callback_data=f"edit_details:{order_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "💶 CHANGE PRICE"
                if english
                else "💶 ΑΛΛΑΓΗ ΤΙΜΗΣ",
                callback_data=f"edit_price:{order_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ CANCEL REQUEST"
                if english
                else "❌ ΑΚΥΡΩΣΗ ΔΙΑΔΡΟΜΗΣ",
                callback_data=f"cancel_order:{order_id}",
            )
        ],
    ]


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

    return InlineKeyboardMarkup([
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
    ])


def confirm_giveup_keyboard(order_id):
    return InlineKeyboardMarkup([
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
    ])


# ==================== ORDER CARDS ====================

def order_card(order):
    status = {
        "open": "🟢 ΔΙΑΘΕΣΙΜΗ",
        "taken": "✅ ΔΟΘΗΚΕ",
        "cancelled": "❌ ΑΚΥΡΩΘΗΚΕ",
    }.get(order["status"], "🟢 ΔΙΑΘΕΣΙΜΗ")

    title = (
        "ΠΕΛΑΤΗΣ"
        if order["creator_role"] == "client"
        else "ΟΔΗΓΟΣ"
    )

    details = html.escape(order["details"])

    if is_voice_order(order):
        details = (
            "🎤 Άκουσε το φωνητικό μήνυμα "
            "ακριβώς από πάνω."
        )

        if order["details"] != "VOICE ORDER":
            details += "\n" + html.escape(order["details"])

    price = (
        f"{html.escape(order['price'])} €"
        if order["price"]
        else "Συζητήσιμη"
    )

    text = (
        f"🚕 <b>ΔΙΑΔΡΟΜΗ #{order['id']} — {title}</b>\n"
        f"<b>{status}</b>\n\n"
        f"<b>{details}</b>\n\n"
        f"💶 <b>ΤΙΜΗ: {price}</b>\n"
        "👤 Από: "
        + contact_identity(
            order["creator_id"],
            order["creator_name"],
            order["creator_username"],
        )
    )

    if order["status"] == "taken":
        text += "\n🚖 Οδηγός: " + contact_identity(
            order["taker_id"],
            order["taker_name"],
            order["taker_username"],
        )

    return text


def private_order_card(order):
    if order["creator_role"] != "client":
        return order_card(order).replace(
            "ακριβώς από πάνω",
            "στην ομάδα οδηγών",
        )

    status = {
        "open": "🔎 Looking for a driver",
        "taken": "✅ Driver found",
        "cancelled": "❌ Cancelled",
    }.get(order["status"], "")

    price = (
        f"{html.escape(order['price'])} €"
        if order["price"]
        else "Negotiable"
    )

    text = (
        f"🚕 <b>YOUR REQUEST #{order['id']}</b>\n"
        f"{status}\n\n"
        f"{html.escape(order['details'])}\n\n"
        f"💶 <b>{price}</b>"
    )

    if order["status"] == "taken":
        text += "\n\n" + contacts_card(order, english=True)

    return text


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
            disable_web_page_preview=True,
        )

    except Exception:
        logger.exception("Could not refresh group order")


# ==================== PERSONAL ORDER ALERTS ====================

async def schedule_order_alerts(
    context,
    order_id,
    exclude_user_id=None,
):
    async with db_lock:
        with db() as conn:
            order = conn.execute(
                "SELECT * FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()

            if not order or order["status"] != "open":
                return

            generation = order["alert_generation"] + 1

            conn.execute(
                "UPDATE orders SET alert_generation = ? WHERE id = ?",
                (generation, order_id),
            )

            conn.execute(
                "DELETE FROM order_alerts WHERE order_id = ?",
                (order_id,),
            )

            conn.execute(
                """
                INSERT INTO order_alerts(
                    order_id, user_id, generation, next_at
                )
                SELECT ?, user_id, ?, ?
                FROM alert_drivers
                WHERE enabled = 1
                  AND user_id <> ?
                  AND user_id <> ?
                """,
                (
                    order_id,
                    generation,
                    time.time(),
                    order["creator_id"],
                    exclude_user_id or 0,
                ),
            )


def next_order_alert():
    with db() as conn:
        conn.execute("""
            UPDATE order_alerts
            SET stopped = 1
            WHERE stopped = 0
              AND EXISTS (
                  SELECT 1
                  FROM orders
                  WHERE orders.id = order_alerts.order_id
                    AND (
                        status <> 'open'
                        OR alert_generation <> generation
                    )
              )
        """)

        return conn.execute(
            """
            SELECT a.*
            FROM order_alerts a
            JOIN orders o ON o.id = a.order_id
            JOIN alert_drivers d ON d.user_id = a.user_id
            WHERE a.stopped = 0
              AND a.sent_count < ?
              AND a.next_at <= ?
              AND d.enabled = 1
              AND o.status = 'open'
              AND o.alert_generation = a.generation
            ORDER BY a.next_at, a.order_id, a.user_id
            LIMIT 1
            """,
            (ALERT_COUNT, time.time()),
        ).fetchone()


def alert_is_active(alert):
    with db() as conn:
        return conn.execute(
            """
            SELECT 1
            FROM order_alerts a
            JOIN orders o ON o.id = a.order_id
            JOIN alert_drivers d ON d.user_id = a.user_id
            WHERE a.order_id = ?
              AND a.user_id = ?
              AND a.generation = ?
              AND a.stopped = 0
              AND a.sent_count < ?
              AND d.enabled = 1
              AND o.status = 'open'
              AND o.alert_generation = a.generation
            """,
            (
                alert["order_id"],
                alert["user_id"],
                alert["generation"],
                ALERT_COUNT,
            ),
        ).fetchone() is not None


def update_alert(
    alert,
    *,
    sent=False,
    stop=False,
    delay=ALERT_INTERVAL,
    failure=False,
):
    with db() as conn:
        conn.execute(
            """
            UPDATE order_alerts
            SET sent_count = sent_count + ?,
                stopped = ?,
                next_at = ?,
                failures = CASE
                    WHEN ? THEN failures + 1
                    ELSE 0
                END
            WHERE order_id = ?
              AND user_id = ?
              AND generation = ?
              AND stopped = 0
            """,
            (
                int(sent),
                int(stop),
                time.time() + delay,
                int(failure),
                alert["order_id"],
                alert["user_id"],
                alert["generation"],
            ),
        )


def personal_alert_card(order, number):
    text = order_card(order).replace(
        "🎤 Άκουσε το φωνητικό μήνυμα ακριβώς από πάνω.",
        "🎤 Πάτησε ΑΚΟΥΣΕ ΤΟ ΦΩΝΗΤΙΚΟ παρακάτω.",
    )

    return f"🔔 Ειδοποίηση {number}/{ALERT_COUNT}\n\n" + text


def personal_alert_keyboard(order, generation):
    rows = [[
        button(
            "✅ ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ",
            callback_data=f"take:{order['id']}",
            color="success",
        ),
        button(
            "❌ ΔΕΝ ΕΝΔΙΑΦΕΡΟΜΑΙ",
            callback_data=f"refuse:{order['id']}:{generation}",
            color="danger",
        ),
    ]]

    if is_voice_order(order) and order["voice_group_message_id"]:
        group_id = str(order["group_chat_id"])

        if group_id.startswith("-100"):
            rows.append([
                button(
                    "🎤 ΑΚΟΥΣΕ ΤΟ ΦΩΝΗΤΙΚΟ",
                    url=(
                        f"https://t.me/c/{group_id[4:]}/"
                        f"{order['voice_group_message_id']}"
                    ),
                )
            ])

    return InlineKeyboardMarkup(rows)


async def deliver_order_alert(application, alert):
    try:
        member = await application.bot.get_chat_member(
            DRIVERS_GROUP_ID,
            alert["user_id"],
        )

        if not driver_can_receive(member):
            update_alert(alert, stop=True)

            with db() as conn:
                conn.execute(
                    "UPDATE alert_drivers SET enabled = 0 "
                    "WHERE user_id = ?",
                    (alert["user_id"],),
                )
            return

        if not alert_is_active(alert):
            return

        order = get_order(alert["order_id"])

        await application.bot.send_message(
            chat_id=alert["user_id"],
            text=personal_alert_card(
                order,
                alert["sent_count"] + 1,
            ),
            parse_mode="HTML",
            reply_markup=personal_alert_keyboard(
                order,
                alert["generation"],
            ),
            disable_notification=False,
            read_timeout=10,
            write_timeout=10,
            disable_web_page_preview=True,
        )

        update_alert(
            alert,
            sent=True,
            stop=alert["sent_count"] + 1 >= ALERT_COUNT,
        )

        with db() as conn:
            conn.execute(
                """
                UPDATE order_alerts
                SET next_at = MAX(next_at, ?)
                WHERE user_id = ? AND stopped = 0
                """,
                (time.time() + 1.1, alert["user_id"]),
            )

    except RetryAfter as exc:
        wait = exc.retry_after

        wait = (
            wait.total_seconds()
            if hasattr(wait, "total_seconds")
            else float(wait)
        )

        update_alert(alert, delay=wait + 1)

        with db() as conn:
            conn.execute(
                """
                UPDATE order_alerts
                SET next_at = MAX(next_at, ?)
                WHERE stopped = 0
                """,
                (time.time() + wait + 1,),
            )

    except Forbidden:
        update_alert(alert, stop=True)

        with db() as conn:
            conn.execute(
                "UPDATE alert_drivers SET enabled = 0 "
                "WHERE user_id = ?",
                (alert["user_id"],),
            )

    except BadRequest:
        logger.exception(
            "Invalid alert for order %s driver %s",
            alert["order_id"],
            alert["user_id"],
        )
        update_alert(alert, stop=True)

    except TelegramError:
        logger.warning(
            "Temporary alert failure for order %s driver %s",
            alert["order_id"],
            alert["user_id"],
            exc_info=True,
        )

        update_alert(
            alert,
            delay=10,
            failure=True,
            stop=alert["failures"] >= 4,
        )


async def order_alert_worker(application):
    while True:
        try:
            alert = next_order_alert()

            if alert is None:
                await asyncio.sleep(0.5)
                continue

            await deliver_order_alert(application, alert)
            await asyncio.sleep(ALERT_SEND_DELAY)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Order alert worker failed; retrying")
            await asyncio.sleep(2)


async def start_alert_worker(application):
    application.bot_data["order_alert_worker"] = (
        asyncio.create_task(order_alert_worker(application))
    )

    try:
        await application.bot.set_my_commands([
            ("start", "Menu / Μενού"),
            ("new", "New request / Νέα διαδρομή"),
            ("help", "Help / Βοήθεια"),
        ])

        await send_driver_panel(application)

        with db() as conn:
            orders = conn.execute(
                """
                SELECT id FROM orders
                WHERE status = 'open'
                  AND group_message_id IS NOT NULL
                """
            ).fetchall()

        for order in orders:
            await refresh_group_card(application, order["id"])
            await asyncio.sleep(0.1)

    except TelegramError:
        logger.exception(
            "Could not update driver panel on startup; "
            "use /panel later"
        )


async def stop_alert_worker(application):
    task = application.bot_data.pop("order_alert_worker", None)

    if task:
        task.cancel()

        with suppress(asyncio.CancelledError):
            await task


# ==================== PUBLISH ORDERS ====================

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
                INSERT INTO orders(
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
        disable_web_page_preview=True,
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

    await schedule_order_alerts(context, order_id)
    await refresh_group_card(context, order_id)

    return order_id


async def create_voice_order(
    context,
    user,
    voice_file_id,
    price=None,
):
    async with db_lock:
        with db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders(
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
                    ?, ?, ?, 'driver', ?, ?, 'open', ?, ?
                )
                """,
                (
                    user.id,
                    person_name(user),
                    user.username,
                    "VOICE ORDER",
                    price,
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
        disable_web_page_preview=True,
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

    await schedule_order_alerts(context, order_id)
    await refresh_group_card(context, order_id)

    return order_id


# ==================== NEW ORDER: THREE STEPS ====================

async def begin_new_order(update, context, role):
    user = update.effective_user

    user_state[user.id] = {
        "action": "new_details",
        "role": role,
        "draft_id": secrets.token_hex(4),
    }

    if role == "client":
        text = (
            "🚕 <b>STEP 1/3 — ROUTE</b>\n\n"
            "Send in one message:\n"
            "📍 Pickup → destination\n"
            "🕒 Date and time\n"
            "👥 Passengers\n\n"
            "Example: Larnaca Airport → Limassol, "
            "today 18:30, 2 passengers.\n\n"
            "Next: choose the price."
        )
    else:
        text = (
            "🚕 <b>ΒΗΜΑ 1/3 — ΔΙΑΔΡΟΜΗ</b>\n\n"
            "Στείλε σε ένα μήνυμα:\n"
            "📍 Από πού → πού\n"
            "🕒 Ημερομηνία και ώρα\n"
            "👥 Επιβάτες\n\n"
            "Παράδειγμα: Αεροδρόμιο Λάρνακας → Λεμεσός, "
            "σήμερα 18:30, 2 άτομα.\n\n"
            "Μπορείς να στείλεις και 🎤 φωνητικό.\n"
            "Μετά θα επιλέξεις την τιμή."
        )

    await context.bot.send_message(
        chat_id=user.id,
        text=text,
        parse_mode="HTML",
        reply_markup=cancel_keyboard(role),
        disable_notification=True,
        disable_web_page_preview=True,
    )


def price_keyboard(state):
    key = state["draft_id"]
    english = state["role"] == "client"

    return InlineKeyboardMarkup([
        [
            button(
                f"{amount} €",
                callback_data=f"draft:{key}:price_{amount}",
                color="primary",
            )
            for amount in (15, 20, 30)
        ],
        [
            button(
                "✏️ ENTER ANOTHER AMOUNT"
                if english
                else "✏️ ΑΛΛΟ ΠΟΣΟ",
                callback_data=f"draft:{key}:manual",
            )
        ],
        [
            button(
                "🤝 NEGOTIABLE"
                if english
                else "🤝 ΣΥΖΗΤΗΣΙΜΗ ΤΙΜΗ",
                callback_data=f"draft:{key}:negotiable",
            )
        ],
        [
            button(
                "⬅️ CHANGE ROUTE"
                if english
                else "⬅️ ΑΛΛΑΓΗ ΔΙΑΔΡΟΜΗΣ",
                callback_data=f"draft:{key}:details",
            )
        ],
        [
            button(
                "❌ CANCEL" if english else "❌ ΑΚΥΡΩΣΗ",
                callback_data=f"draft:{key}:cancel",
                color="danger",
            )
        ],
    ])


async def ask_price(context, user_id, state):
    state["action"] = "new_price"
    english = state["role"] == "client"

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            "💶 <b>STEP 2/3 — PRICE</b>\n\n"
            "Choose an amount or enter your own."
            if english
            else
            "💶 <b>ΒΗΜΑ 2/3 — ΤΙΜΗ</b>\n\n"
            "Διάλεξε ποσό ή γράψε τη δική σου τιμή."
        ),
        parse_mode="HTML",
        reply_markup=price_keyboard(state),
        disable_notification=True,
        disable_web_page_preview=True,
    )


async def show_draft(context, user_id, state):
    state["action"] = "new_confirm"
    english = state["role"] == "client"
    key = state["draft_id"]

    details = html.escape(
        state.get("details", "VOICE ORDER")
    )

    if state.get("voice_file_id"):
        details = (
            "🎤 Voice route"
            if english
            else "🎤 Φωνητική διαδρομή"
        )

        if state.get("details") not in (None, "VOICE ORDER"):
            details += "\n" + html.escape(state["details"])

    price = (
        f"{state['price']} €"
        if state.get("price")
        else ("Negotiable" if english else "Συζητήσιμη")
    )

    text = (
        (
            "✅ <b>STEP 3/3 — CHECK YOUR REQUEST</b>\n\n"
            if english
            else
            "✅ <b>ΒΗΜΑ 3/3 — ΕΛΕΓΧΟΣ ΔΙΑΔΡΟΜΗΣ</b>\n\n"
        )
        + details
        + f"\n\n💶 <b>{price}</b>\n\n"
        + (
            "Not published yet. Tap Publish below."
            if english
            else
            "Δεν έχει δημοσιευτεί ακόμα. "
            "Πάτησε Δημοσίευση παρακάτω."
        )
    )

    await context.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode="HTML",
        disable_notification=True,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [
                button(
                    "✅ PUBLISH REQUEST"
                    if english
                    else "✅ ΔΗΜΟΣΙΕΥΣΗ ΔΙΑΔΡΟΜΗΣ",
                    callback_data=f"draft:{key}:publish",
                    color="success",
                )
            ],
            [
                button(
                    "✏️ CHANGE ROUTE"
                    if english
                    else "✏️ ΑΛΛΑΓΗ ΔΙΑΔΡΟΜΗΣ",
                    callback_data=f"draft:{key}:details",
                )
            ],
            [
                button(
                    "💶 CHANGE PRICE"
                    if english
                    else "💶 ΑΛΛΑΓΗ ΤΙΜΗΣ",
                    callback_data=f"draft:{key}:prices",
                )
            ],
            [
                button(
                    "❌ CANCEL" if english else "❌ ΑΚΥΡΩΣΗ",
                    callback_data=f"draft:{key}:cancel",
                    color="danger",
                )
            ],
        ]),
    )


async def draft_callback(update, context):
    query = update.callback_query
    user = update.effective_user
    parts = (query.data or "").split(":")
    state = user_state.get(user.id)

    if (
        not query.message
        or query.message.chat.type != "private"
        or len(parts) != 3
        or not state
        or state.get("draft_id") != parts[1]
    ):
        await query.answer(
            "This step has expired. Open the menu /start.",
            show_alert=True,
        )
        return

    action = parts[2]
    english = state["role"] == "client"

    if action == "cancel":
        user_state.pop(user.id, None)
        await query.answer()

        with suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=None
            )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "Cancelled. Nothing was published."
                if english
                else "Ακυρώθηκε. Δεν δημοσιεύτηκε τίποτα."
            ),
            reply_markup=main_keyboard(state["role"]),
            disable_notification=True,
        )
        return

    if state.get("action") not in (
        "new_price",
        "new_manual_price",
        "new_confirm",
    ):
        await query.answer(
            "Complete the current step first.",
            show_alert=True,
        )
        return

    if (
        state["role"] == "driver"
        and not await is_driver(user.id, context)
    ):
        await query.answer(
            "Μόνο για οδηγούς.",
            show_alert=True,
        )
        return

    await query.answer()

    if action == "details":
        state["action"] = "new_details"

        with suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=None
            )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "Send the updated route."
                if english
                else
                "Στείλε τη νέα διαδρομή "
                "με κείμενο ή 🎤 φωνητικό."
            ),
            reply_markup=cancel_keyboard(state["role"]),
            disable_notification=True,
        )
        return

    if action == "prices":
        await ask_price(context, user.id, state)
        return

    if action == "manual":
        state["action"] = "new_manual_price"

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "💶 Enter an amount in EUR, "
                "for example 25 or 25.50."
                if english
                else
                "💶 Γράψε το ποσό σε ευρώ. "
                "Παράδειγμα: 25 ή 25,50."
            ),
            reply_markup=price_keyboard(state),
            disable_notification=True,
        )
        return

    if action == "negotiable" or action in (
        "price_15",
        "price_20",
        "price_30",
    ):
        state["price"] = (
            None
            if action == "negotiable"
            else action.split("_", 1)[1]
        )

        with suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=None
            )

        await show_draft(context, user.id, state)
        return

    if action == "publish" and state["action"] == "new_confirm":
        # Убираем черновик до сетевых запросов,
        # чтобы повторное нажатие не создало второй заказ.
        user_state.pop(user.id, None)

        with suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=None
            )

        try:
            if state.get("voice_file_id"):
                order_id = await create_voice_order(
                    context,
                    user,
                    state["voice_file_id"],
                    state.get("price"),
                )
            else:
                order_id = await create_text_order(
                    context,
                    user,
                    state["role"],
                    state["details"],
                    state.get("price"),
                )

        except Exception:
            logger.exception("Could not publish draft")

            await context.bot.send_message(
                chat_id=user.id,
                text=(
                    "Could not confirm publication. "
                    "Check the group before trying again."
                    if english
                    else
                    "Δεν επιβεβαιώθηκε η δημοσίευση. "
                    "Έλεγξε πρώτα την ομάδα "
                    "πριν δοκιμάσεις ξανά."
                ),
                reply_markup=main_keyboard(state["role"]),
                disable_notification=True,
            )
            return

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"✅ Published! Request #{order_id}. "
                "You will be notified when a driver accepts."
                if english
                else
                f"✅ Δημοσιεύτηκε! Διαδρομή #{order_id}. "
                "Θα ειδοποιηθείς όταν την πάρει οδηγός."
            ),
            reply_markup=main_keyboard(state["role"]),
            disable_notification=True,
        )

        order = get_order(order_id)

        await context.bot.send_message(
            chat_id=user.id,
            text=private_order_card(order),
            parse_mode="HTML",
            reply_markup=creator_manage_keyboard(order),
            disable_notification=True,
            disable_web_page_preview=True,
        )
        return


# ==================== DRIVER GROUP PANEL ====================

async def send_driver_panel(context):
    text = (
        "🚕 <b>ΔΙΑΔΡΟΜΕΣ ΟΔΗΓΩΝ</b>\n\n"
        "<b>ΘΕΛΕΙΣ ΝΑ ΔΩΣΕΙΣ ΔΙΑΔΡΟΜΗ;</b>\n"
        "Πάτησε το μπλε κουμπί παρακάτω.\n"
        "1️⃣ Στείλε διαδρομή\n"
        "2️⃣ Διάλεξε τιμή\n"
        "3️⃣ Επιβεβαίωσε δημοσίευση\n\n"
        "<b>ΘΕΛΕΙΣ ΝΑ ΠΑΡΕΙΣ ΔΙΑΔΡΟΜΗ;</b>\n"
        "Πάτησε ✅ ΠΑΡΕ ΤΗ ΔΙΑΔΡΟΜΗ "
        "κάτω από τον αντίστοιχο αριθμό.\n\n"
        "❌ ΔΕΝ ΕΝΔΙΑΦΕΡΟΜΑΙ: σταματά τις "
        "προσωπικές υπενθυμίσεις μόνο για εσένα.\n\n"
        "🔔 Για προσωπικές ειδοποιήσεις, "
        "άνοιξε το bot και πάτησε START."
    )

    markup = InlineKeyboardMarkup([
        [
            button(
                "➕ ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ",
                url=f"https://t.me/{BOT_USERNAME}?start=driver",
                color="primary",
            )
        ],
        [
            button(
                "🔔 ΑΝΟΙΓΜΑ BOT / ΜΕΝΟΥ",
                url=f"https://t.me/{BOT_USERNAME}?start=menu",
            )
        ],
    ])

    with db() as conn:
        saved = conn.execute(
            "SELECT value FROM bot_settings "
            "WHERE key = 'driver_panel'"
        ).fetchone()

    message_id = int(saved["value"]) if saved else None

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=DRIVERS_GROUP_ID,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )

        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                message_id = None

    if not message_id:
        message = await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_notification=True,
            disable_web_page_preview=True,
        )

        message_id = message.message_id

        with db() as conn:
            conn.execute(
                """
                INSERT INTO bot_settings(key, value)
                VALUES ('driver_panel', ?)
                ON CONFLICT(key)
                DO UPDATE SET value = excluded.value
                """,
                (str(message_id),),
            )

    with suppress(TelegramError):
        await context.bot.pin_chat_message(
            chat_id=DRIVERS_GROUP_ID,
            message_id=message_id,
            disable_notification=True,
        )

    return message_id


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
            "➕ <b>ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ</b>: "
            "στείλε διαδρομή → διάλεξε τιμή → επιβεβαίωσε.\n\n"
            "📋 <b>ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ</b>: "
            "διαδρομές που δημοσίευσες.\n"
            "📦 <b>ΔΙΑΔΡΟΜΕΣ ΠΟΥ ΠΗΡΑ</b>: "
            "διαδρομές που αποδέχτηκες.\n\n"
            "🔔 Οι προσωπικές ειδοποιήσεις ενεργοποιήθηκαν: "
            "έως 6 ανά διαδρομή.\n"
            "✅ Πάρε τη διαδρομή ή ❌ Δεν ενδιαφέρομαι "
            "για να σταματήσουν οι υπενθυμίσεις.\n\n"
            "👇 Διάλεξε κουμπί παρακάτω."
        )
    else:
        text = (
            "🚕 <b>CYPRUS TAXI</b>\n\n"
            "Tap REQUEST A TAXI. "
            "Send the route → choose the price → "
            "confirm publication."
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(role),
        disable_notification=True,
        disable_web_page_preview=True,
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
        await send_driver_panel(context)

        await context.bot.send_message(
            chat_id=DRIVERS_GROUP_ID,
            text="👇 Δώσε νέα διαδρομή από το κουμπί παρακάτω.",
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
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🚕 OPEN BOT & REQUEST TAXI",
                url=f"https://t.me/{BOT_USERNAME}?start=client",
            )
        ]]),
        disable_notification=True,
        disable_web_page_preview=True,
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
        disable_web_page_preview=True,
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_chat:
        await update.effective_message.reply_text(
            f"Chat ID: {update.effective_chat.id}"
        )


async def help_command(update, context):
    if (
        update.effective_chat
        and update.effective_chat.type == "private"
    ):
        await start(update, context)


async def new_command(update, context):
    if (
        update.effective_chat
        and update.effective_chat.type == "private"
    ):
        await begin_new_order(
            update,
            context,
            await role_for(update.effective_user.id, context),
        )


# ==================== ORDER LISTS ====================

async def show_my_orders(update, context, user_id, role):
    orders = get_creator_active_orders(user_id)

    if not orders:
        text = (
            "📋 <b>ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ</b>\n\n"
            "Δεν έχεις ενεργές διαδρομές."
            if role == "driver"
            else
            "📋 <b>MY ORDERS</b>\n\n"
            "You have no active taxi requests."
        )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=main_keyboard(role),
            disable_notification=True,
            disable_web_page_preview=True,
        )
        return

    buttons = []

    for order in orders:
        status = "✅" if order["status"] == "taken" else "🟢"

        if is_voice_order(order):
            short_details = "🎤 Φωνητική διαδρομή"
        else:
            short_details = order["details"].replace("\n", " ").strip()

            if len(short_details) > 30:
                short_details = short_details[:30] + "…"

        buttons.append([
            InlineKeyboardButton(
                f"{status} #{order['id']} — {short_details}",
                callback_data=f"manage:{order['id']}",
            )
        ])

    title = (
        "📋 <b>ΟΙ ΔΙΑΔΡΟΜΕΣ ΜΟΥ</b>\n\n"
        "👇 Διάλεξε τη διαδρομή:"
        if role == "driver"
        else
        "📋 <b>MY ORDERS</b>\n\n"
        "👇 Select the request you want to manage:"
    )

    await update.message.reply_text(
        title,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_notification=True,
        disable_web_page_preview=True,
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
            disable_web_page_preview=True,
        )
        return

    buttons = []

    for order in orders:
        if is_voice_order(order):
            short_details = "🎤 Φωνητική διαδρομή"
        else:
            short_details = order["details"].replace("\n", " ").strip()

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
        disable_web_page_preview=True,
    )


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

        card = (
            private_order_card(order)
            if recipient == order["creator_id"]
            else order_card(order)
        )

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
                disable_web_page_preview=True,
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

        await update.message.reply_text(
            "↩️ Request cancelled."
            if state_role == "client"
            else "↩️ Ακυρώθηκε.",
            reply_markup=main_keyboard(state_role),
            disable_notification=True,
        )
        return

    if text in (
        BTN_NEW_DRIVER,
        OLD_NEW_DRIVER,
        *LEGACY_DRIVER_BUTTONS,
    ):
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
        await show_my_orders(update, context, user.id, "client")
        return

    if (
        text in (BTN_DRIVER_ORDERS, OLD_MY_ORDER, OLD_MY_ORDERS)
        and role == "driver"
    ):
        await show_my_orders(update, context, user.id, "driver")
        return

    if text in (BTN_TAKEN, OLD_TAKEN, OLD_TAKEN_ORDERS):
        if await is_driver(user.id, context):
            await show_taken_orders(update, context, user.id)
        return

    state = user_state.get(user.id)

    if not state:
        role = await role_for(user.id, context)

        await update.message.reply_text(
            "👇 Χρησιμοποίησε τα κουμπιά παρακάτω."
            if role == "driver"
            else "👇 Please use the buttons below.",
            reply_markup=main_keyboard(role),
            disable_notification=True,
        )
        return

    action = state["action"]
    state_role = state.get("role", role)

    if action == "new_details":
        if len(text) < 3 or len(text) > 1800:
            await update.message.reply_text(
                "Please send a route of 3–1800 characters."
                if state_role == "client"
                else
                "Στείλε διαδρομή από 3 έως 1800 χαρακτήρες.",
                disable_notification=True,
            )
            return

        state["details"] = text
        state.pop("voice_file_id", None)

        await ask_price(context, user.id, state)
        return

    if action in ("new_price", "new_manual_price"):
        try:
            state["price"] = parse_price(text)

        except (ValueError, InvalidOperation):
            await update.message.reply_text(
                "Enter a positive amount in EUR, "
                "for example 25 or 25.50."
                if state_role == "client"
                else
                "Γράψε έγκυρο ποσό σε ευρώ. "
                "Παράδειγμα: 25 ή 25,50.",
                reply_markup=price_keyboard(state),
            )
            return

        await show_draft(context, user.id, state)
        return

    if action == "new_confirm":
        await update.message.reply_text(
            "Tap Publish, Change route or Change price below."
            if state_role == "client"
            else
            "Πάτησε Δημοσίευση, Αλλαγή διαδρομής "
            "ή Αλλαγή τιμής.",
            disable_notification=True,
        )

        await show_draft(context, user.id, state)
        return

    if action in ("edit_details", "edit_price"):
        order_id = state["order_id"]
        value = text

        if action == "edit_details" and (
            len(text) < 3 or len(text) > 1800
        ):
            await update.message.reply_text(
                "Use 3–1800 characters / "
                "Στείλε 3–1800 χαρακτήρες."
            )
            return

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

                allowed = can_manage_order(current, user.id)

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
            disable_web_page_preview=True,
        )
        return


# ==================== PRIVATE VOICE ====================

async def private_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.effective_user
        or not update.message
        or not update.message.voice
    ):
        return

    user = update.effective_user

    if not await is_driver(user.id, context):
        await update.message.reply_text(
            "Voice requests are not available for clients.",
            reply_markup=main_keyboard("client"),
        )
        return

    state = user_state.get(user.id)

    if (
        not state
        or state.get("action") != "new_details"
        or state.get("role") != "driver"
    ):
        await update.message.reply_text(
            "Πάτησε πρώτα ➕ ΔΩΣΕ ΝΕΑ ΔΙΑΔΡΟΜΗ.",
            reply_markup=main_keyboard("driver"),
        )
        return

    state["voice_file_id"] = update.message.voice.file_id
    state["details"] = "VOICE ORDER"

    await ask_price(context, user.id, state)


# ==================== ORDER CALLBACKS ====================

async def callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not update.effective_user:
        return

    user = update.effective_user
    data = query.data or ""

    if data.startswith("draft:"):
        await draft_callback(update, context)
        return

    # Отказ в группе: только для нажавшего водителя.
    if data.startswith("group_refuse:"):
        parts = data.split(":")

        if (
            len(parts) != 3
            or not all(part.isdigit() for part in parts[1:])
        ):
            await query.answer()
            return

        if (
            not query.message
            or query.message.chat.id != DRIVERS_GROUP_ID
        ):
            await query.answer(
                "Μόνο στην ομάδα οδηγών.",
                show_alert=True,
            )
            return

        member = await context.bot.get_chat_member(
            DRIVERS_GROUP_ID,
            user.id,
        )

        if not driver_can_receive(member):
            await query.answer(
                "Μόνο για οδηγούς.",
                show_alert=True,
            )
            return

        order_id, generation = map(int, parts[1:])

        async with db_lock:
            with db() as conn:
                current = conn.execute(
                    "SELECT * FROM orders WHERE id = ?",
                    (order_id,),
                ).fetchone()

                valid = bool(
                    current
                    and current["status"] == "open"
                    and current["alert_generation"] == generation
                    and current["creator_id"] != user.id
                )

                if valid:
                    conn.execute(
                        """
                        INSERT INTO order_alerts(
                            order_id,
                            user_id,
                            generation,
                            next_at,
                            stopped
                        )
                        VALUES (?, ?, ?, ?, 1)
                        ON CONFLICT(order_id, user_id)
                        DO UPDATE SET
                            generation = excluded.generation,
                            stopped = 1
                        """,
                        (
                            order_id,
                            user.id,
                            generation,
                            time.time(),
                        ),
                    )

        if not valid:
            await query.answer(
                "Η διαδρομή δεν είναι διαθέσιμη "
                "ή είναι δική σου.",
                show_alert=True,
            )
            return

        await query.answer(
            "✅ Δεν θα λάβεις άλλες ειδοποιήσεις "
            "για αυτή τη διαδρομή.",
            show_alert=True,
        )

        # Кнопки в общем сообщении сохраняются.
        return

    # Отказ в личном уведомлении.
    if data.startswith("refuse:"):
        parts = data.split(":")

        if (
            len(parts) != 3
            or not all(part.isdigit() for part in parts[1:])
        ):
            await query.answer()
            return

        order_id, generation = map(int, parts[1:])

        if (
            not query.message
            or query.message.chat.type != "private"
        ):
            await query.answer(
                "Μόνο σε προσωπικό μήνυμα.",
                show_alert=True,
            )
            return

        async with db_lock:
            with db() as conn:
                result = conn.execute(
                    """
                    UPDATE order_alerts
                    SET stopped = 1
                    WHERE order_id = ?
                      AND user_id = ?
                      AND generation = ?
                      AND EXISTS (
                          SELECT 1 FROM orders
                          WHERE id = ?
                            AND status = 'open'
                            AND alert_generation = ?
                      )
                    """,
                    (
                        order_id,
                        user.id,
                        generation,
                        order_id,
                        generation,
                    ),
                )

        if not result.rowcount:
            await query.answer(
                "Η ειδοποίηση δεν είναι πλέον ενεργή.",
                show_alert=True,
            )
            return

        await query.answer(
            "Δεν θα λάβεις άλλες ειδοποιήσεις "
            "για αυτή τη διαδρομή."
        )

        with suppress(TelegramError):
            await query.edit_message_reply_markup(
                reply_markup=None
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

    # Принятие заказа.
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
                    "✅ <b>ΠΗΡΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n"
                    "Συνεννοήσου με τον δημιουργό "
                    "από το κουμπί επικοινωνίας.\n\n"
                    + order_card(updated)
                ),
                parse_mode="HTML",
                reply_markup=accepted_driver_keyboard(updated),
                disable_notification=True,
                disable_web_page_preview=True,
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
                disable_web_page_preview=True,
            )

        except Exception:
            logger.exception("Could not message creator")

        return

    # Управление заказом.
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
                disable_web_page_preview=True,
            )
            return

        if action in ("edit_details", "edit_price"):
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
                    else
                    "Στείλε τη νέα τιμή σε EUR. Παράδειγμα: 50"
                )
            else:
                prompt = (
                    "Send the new trip details in one message."
                    if english
                    else
                    "Στείλε τα νέα στοιχεία σε ένα μήνυμα."
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

    # Действия принявшего водителя.
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
            disable_web_page_preview=True,
        )
        return

    if action == "giveup":
        await query.answer()

        await query.edit_message_reply_markup(
            reply_markup=confirm_giveup_keyboard(order_id),
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

        await schedule_order_alerts(
            context,
            order_id,
            exclude_user_id=user.id,
        )

        await query.answer("Η διαδρομή επέστρεψε.")
        await refresh_group_card(context, order_id)

        await query.edit_message_text(
            "↩️ <b>ΑΦΗΣΕΣ ΤΗ ΔΙΑΔΡΟΜΗ</b>\n\n"
            f"Η διαδρομή #{order_id} είναι ξανά διαθέσιμη.",
            parse_mode="HTML",
            disable_web_page_preview=True,
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
                disable_web_page_preview=True,
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

    if text in (
        BTN_NEW_DRIVER,
        OLD_NEW_DRIVER,
        *LEGACY_DRIVER_BUTTONS,
    ):
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
        .post_init(start_alert_worker)
        .post_stop(stop_alert_worker)
        .post_shutdown(stop_alert_worker)
        .build()
    )

    app.add_handler(
        TypeHandler(Update, enforce_driver_access),
        group=-1,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", help_command))
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
