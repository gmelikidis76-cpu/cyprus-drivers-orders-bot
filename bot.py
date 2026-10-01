import os
import sqlite3
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")
DB_FILE = "taxi_bot.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


def db_connect():
    return sqlite3.connect(DB_FILE)


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drivers (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                username TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                accepted_by INTEGER
            )
        """)


def register_driver(user):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO drivers (user_id, name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username
            """,
            (user.id, user.full_name, user.username),
        )


def get_driver_ids():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id FROM drivers"
        ).fetchall()

    return [row[0] for row in rows]


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    register_driver(user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚕 Δώσε διαδρομή",
                callback_data="new_order"
            )
        ]
    ])

    await update.effective_message.reply_text(
        "Καλώς ήρθες στην ανταλλαγή διαδρομών 🚕\n\n"
        "Έχεις εγγραφεί και θα λαμβάνεις νέες "
        "διαδρομές από τους συναδέλφους.",
        reply_markup=keyboard,
    )


# Временная команда для получения ID вашей группы
async def groupid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat = update.effective_chat

    await update.effective_message.reply_text(
        f"GROUP ID: {chat.id}"
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    if query.data == "new_order":

        context.user_data["waiting_for_order"] = True

        await query.message.reply_text(
            "Γράψε τα στοιχεία της διαδρομής "
            "σε ένα μήνυμα.\n\n"
            "Παράδειγμα:\n"
            "Λεμεσός → Αεροδρόμιο Λάρνακας\n"
            "Σήμερα 18:30\n"
            "Τιμή €55\n"
            "2 επιβάτες"
        )

        return

    if not query.data.startswith("accept_"):
        return

    try:
        order_id = int(
            query.data.split("_", 1)[1]
        )

    except (ValueError, IndexError):

        await query.message.reply_text(
            "Παρουσιάστηκε σφάλμα "
            "με τη διαδρομή."
        )

        return

    user = query.from_user

    conn = db_connect()

    try:
        conn.execute("BEGIN IMMEDIATE")

        order = conn.execute(
            """
            SELECT creator_id, text, status
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            conn.rollback()

            await query.message.reply_text(
                "Η διαδρομή δεν βρέθηκε."
            )

            return

        creator_id, order_text, status = order

        if status != "open":
            conn.rollback()

            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )

            return

        if creator_id == user.id:
            conn.rollback()

            await query.answer(
                "Δεν μπορείς να πάρεις "
                "τη δική σου διαδρομή.",
                show_alert=True,
            )

            return

        result = conn.execute(
            """
            UPDATE orders
            SET status = 'accepted',
                accepted_by = ?
            WHERE id = ?
              AND status = 'open'
            """,
            (user.id, order_id),
        )

        if result.rowcount != 1:
            conn.rollback()

            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )

            return

        conn.commit()

    finally:
        conn.close()

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )

    except Exception:
        logger.exception(
            "Could not remove accept button"
        )

    await query.message.reply_text(
        "✅ Πήρες τη διαδρομή."
    )

    username = (
        f"@{user.username}"
        if user.username
        else "Δεν υπάρχει username"
    )

    try:
        await context.bot.send_message(
            chat_id=creator_id,
            text=(
                "✅ Η διαδρομή σου έγινε αποδεκτή!\n\n"
                f"{order_text}\n\n"
                f"Οδηγός: {user.full_name}\n"
                f"Telegram: {username}"
            ),
        )

    except Exception:
        logger.exception(
            "Could not notify order creator"
        )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not context.user_data.get(
        "waiting_for_order"
    ):
        return

    order_text = (
        update.effective_message.text or ""
    ).strip()

    if not order_text:

        await update.effective_message.reply_text(
            "Γράψε τα στοιχεία της διαδρομής "
            "σε ένα μήνυμα."
        )

        return

    context.user_data["waiting_for_order"] = False

    user = update.effective_user

    register_driver(user)

    with db_connect() as conn:

        cursor = conn.execute(
            """
            INSERT INTO orders (
                creator_id,
                text,
                status
            )
            VALUES (?, ?, 'open')
            """,
            (user.id, order_text),
        )

        order_id = cursor.lastrowid

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Πάρε τη διαδρομή",
                callback_data=f"accept_{order_id}",
            )
        ]
    ])

    sent = 0

    for driver_id in get_driver_ids():

        if driver_id == user.id:
            continue

        try:
            await context.bot.send_message(
                chat_id=driver_id,
                text=(
                    "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                    f"{order_text}"
                ),
                reply_markup=keyboard,
            )

            sent += 1

        except Exception:
            logger.exception(
                "Could not send order "
                "to driver %s",
                driver_id,
            )

    await update.effective_message.reply_text(
        "✅ Η διαδρομή στάλθηκε "
        "στους συναδέλφους.\n"
        f"Παραλήπτες: {sent}"
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Unhandled error",
        exc_info=context.error
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
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("groupid", groupid)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(error_handler)

    logger.info(
        "Taxi orders bot started"
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                accepted_by INTEGER
            )
        """)


def register_driver(user):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO drivers (user_id, name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username
            """,
            (user.id, user.full_name, user.username),
        )


def get_driver_ids():
    with db_connect() as conn:
        rows = conn.execute("SELECT user_id FROM drivers").fetchall()

    return [row[0] for row in rows]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_driver(user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚕 Δώσε διαδρομή",
                callback_data="new_order"
            )
        ]
    ])

    await update.effective_message.reply_text(
        "Καλώς ήρθες στην ανταλλαγή διαδρομών 🚕\n\n"
        "Έχεις εγγραφεί και θα λαμβάνεις νέες διαδρομές "
        "από τους συναδέλφους.",
        reply_markup=keyboard,
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    if query.data == "new_order":
        context.user_data["waiting_for_order"] = True

        await query.message.reply_text(
            "Γράψε τα στοιχεία της διαδρομής σε ένα μήνυμα.\n\n"
            "Παράδειγμα:\n"
            "Λεμεσός → Αεροδρόμιο Λάρνακας\n"
            "Σήμερα 18:30\n"
            "Τιμή €55\n"
            "2 επιβάτες"
        )
        return

    if not query.data.startswith("accept_"):
        return

    try:
        order_id = int(query.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text(
            "Παρουσιάστηκε σφάλμα με τη διαδρομή."
        )
        return

    user = query.from_user

    conn = db_connect()

    try:
        conn.execute("BEGIN IMMEDIATE")

        order = conn.execute(
            """
            SELECT creator_id, text, status
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            conn.rollback()
            await query.message.reply_text(
                "Η διαδρομή δεν βρέθηκε."
            )
            return

        creator_id, order_text, status = order

        if status != "open":
            conn.rollback()

            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )
            return

        if creator_id == user.id:
            conn.rollback()

            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )
            return

        result = conn.execute(
            """
            UPDATE orders
            SET status = 'accepted',
                accepted_by = ?
            WHERE id = ?
              AND status = 'open'
            """,
            (user.id, order_id),
        )

        if result.rowcount != 1:
            conn.rollback()

            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )
            return

        conn.commit()

    finally:
        conn.close()

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        logger.exception(
            "Could not remove accept button"
        )

    await query.message.reply_text(
        "✅ Πήρες τη διαδρομή."
    )

    username = (
        f"@{user.username}"
        if user.username
        else "Δεν υπάρχει username"
    )

    try:
        await context.bot.send_message(
            chat_id=creator_id,
            text=(
                "✅ Η διαδρομή σου έγινε αποδεκτή!\n\n"
                f"{order_text}\n\n"
                f"Οδηγός: {user.full_name}\n"
                f"Telegram: {username}"
            ),
        )

    except Exception:
        logger.exception(
            "Could not notify order creator"
        )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not context.user_data.get(
        "waiting_for_order"
    ):
        return

    order_text = (
        update.effective_message.text or ""
    ).strip()

    if not order_text:
        await update.effective_message.reply_text(
            "Γράψε τα στοιχεία της διαδρομής "
            "σε ένα μήνυμα."
        )
        return

    context.user_data["waiting_for_order"] = False

    user = update.effective_user
    register_driver(user)

    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders (
                creator_id,
                text,
                status
            )
            VALUES (?, ?, 'open')
            """,
            (user.id, order_text),
        )

        order_id = cursor.lastrowid

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Πάρε τη διαδρομή",
                callback_data=f"accept_{order_id}",
            )
        ]
    ])

    sent = 0

    for driver_id in get_driver_ids():

        if driver_id == user.id:
            continue

        try:
            await context.bot.send_message(
                chat_id=driver_id,
                text=(
                    "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
                    f"{order_text}"
                ),
                reply_markup=keyboard,
            )

            sent += 1

        except Exception:
            logger.exception(
                "Could not send order to driver %s",
                driver_id,
            )

    await update.effective_message.reply_text(
        "✅ Η διαδρομή στάλθηκε "
        "στους συναδέλφους.\n"
        f"Παραλήπτες: {sent}"
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Unhandled error",
        exc_info=context.error
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
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(error_handler)

    logger.info(
        "Taxi orders bot started"
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
