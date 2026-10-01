import os
import sqlite3

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


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            text TEXT,
            status TEXT DEFAULT 'open',
            accepted_by INTEGER
        )
    """)

    conn.commit()
    conn.close()


def register_driver(user):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO drivers (user_id, name, username)
        VALUES (?, ?, ?)
        """,
        (user.id, user.full_name, user.username),
    )

    conn.commit()
    conn.close()


def get_drivers():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM drivers")
    drivers = [row[0] for row in cur.fetchall()]

    conn.close()
    return drivers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_driver(update.effective_user)

    keyboard = [
        [InlineKeyboardButton("🚕 Δώσε διαδρομή", callback_data="new_order")]
    ]

    await update.message.reply_text(
        "Καλώς ήρθες στην ανταλλαγή διαδρομών 🚕\n\n"
        "Έχεις εγγραφεί και θα λαμβάνεις νέες διαδρομές από τους συναδέλφους.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if query.data.startswith("accept_"):
        order_id = int(query.data.split("_")[1])
        user = query.from_user

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()

        cur.execute(
            "SELECT creator_id, text, status FROM orders WHERE id = ?",
            (order_id,),
        )
        order = cur.fetchone()

        if not order:
            conn.close()
            await query.message.reply_text("Η διαδρομή δεν βρέθηκε.")
            return

        creator_id, order_text, status = order

        if status != "open":
            conn.close()
            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True
            )
            return

        cur.execute(
            """
            UPDATE orders
            SET status = 'accepted', accepted_by = ?
            WHERE id = ? AND status = 'open'
            """,
            (user.id, order_id),
        )

        conn.commit()

        if cur.rowcount == 0:
            conn.close()
            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True
            )
            return

        conn.close()

        name = user.full_name
        username = f"@{user.username}" if user.username else "χωρίς username"

        await query.edit_message_reply_markup(reply_markup=None)

        await query.message.reply_text(
            "✅ Πήρες τη διαδρομή."
        )

        try:
            await context.bot.send_message(
                chat_id=creator_id,
                text=(
                    "✅ Η διαδρομή σου έγινε αποδεκτή!\n\n"
                    f"{order_text}\n\n"
                    f"Οδηγός: {name}\n"
                    f"Telegram: {username}"
                ),
            )
        except Exception:
            pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_order"):
        return

    context.user_data["waiting_for_order"] = False

    user = update.effective_user
    order_text = update.message.text

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO orders (creator_id, text, status)
        VALUES (?, ?, 'open')
        """,
        (user.id, order_text),
    )

    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Πάρε τη διαδρομή",
                callback_data=f"accept_{order_id}",
            )
        ]]
    )

    sent = 0

    for driver_id in get_drivers():
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
            pass

    await update.message.reply_text(
        f"✅ Η διαδρομή στάλθηκε στους συναδέλφους.\n"
        f"Παραλήπτες: {sent}"
    )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print("Taxi orders bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()        )
    """)

    conn.commit()
    conn.close()


def register_driver(user):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO drivers (user_id, name, username)
        VALUES (?, ?, ?)
        """,
        (user.id, user.full_name, user.username),
    )

    conn.commit()
    conn.close()


def get_drivers():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM drivers")
    drivers = [row[0] for row in cur.fetchall()]

    conn.close()
    return drivers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_driver(update.effective_user)

    keyboard = [
        [InlineKeyboardButton("🚕 Δώσε διαδρομή", callback_data="new_order")]
    ]

    await update.message.reply_text(
        "Καλώς ήρθες στην ανταλλαγή διαδρομών 🚕\n\n"
        "Έχεις εγγραφεί και θα λαμβάνεις νέες διαδρομές από τους συναδέλφους.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if query.data.startswith("accept_"):
        order_id = int(query.data.split("_")[1])
        user = query.from_user

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()

        cur.execute(
            "SELECT creator_id, text, status FROM orders WHERE id = ?",
            (order_id,),
        )
        order = cur.fetchone()

        if not order:
            conn.close()
            await query.message.reply_text("Η διαδρομή δεν βρέθηκε.")
            return

        creator_id, order_text, status = order

        if status != "open":
            conn.close()
            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True
            )
            return

        cur.execute(
            """
            UPDATE orders
            SET status = 'accepted', accepted_by = ?
            WHERE id = ? AND status = 'open'
            """,
            (user.id, order_id),
        )

        conn.commit()

        if cur.rowcount == 0:
            conn.close()
            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True
            )
            return

        conn.close()

        name = user.full_name
        username = f"@{user.username}" if user.username else "χωρίς username"

        await query.edit_message_reply_markup(reply_markup=None)

        await query.message.reply_text(
            "✅ Πήρες τη διαδρομή."
        )

        try:
            await context.bot.send_message(
                chat_id=creator_id,
                text=(
                    "✅ Η διαδρομή σου έγινε αποδεκτή!\n\n"
                    f"{order_text}\n\n"
                    f"Οδηγός: {name}\n"
                    f"Telegram: {username}"
                ),
            )
        except Exception:
            pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_order"):
        return

    context.user_data["waiting_for_order"] = False

    user = update.effective_user
    order_text = update.message.text

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO orders (creator_id, text, status)
        VALUES (?, ?, 'open')
        """,
        (user.id, order_text),
    )

    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Πάρε τη διαδρομή",
                callback_data=f"accept_{order_id}",
            )
        ]]
    )

    sent = 0

    for driver_id in get_drivers():
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
            pass

    await update.message.reply_text(
        f"✅ Η διαδρομή στάλθηκε στους συναδέλφους.\n"
        f"Παραλήπτες: {sent}"
    )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print("Taxi orders bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()            accepted_by INTEGER
        )
    """)

    conn.commit()
    conn.close()


def register_driver(user):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO drivers (user_id, name, username)
        VALUES (?, ?, ?)
        """,
        (user.id, user.full_name, user.username),
    )

    conn.commit()
    conn.close()


def get_drivers():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM drivers")
    drivers = [row[0] for row in cur.fetchall()]

    conn.close()
    return drivers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_driver(update.effective_user)

    keyboard = [
        [InlineKeyboardButton("🚕 Передать заказ", callback_data="new_order")]
    ]

    await update.message.reply_text(
        "Добро пожаловать в обмен заказами такси 🚕\n\n"
        "Вы зарегистрированы и будете получать новые заказы от коллег.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "new_order":
        context.user_data["waiting_for_order"] = True

        await query.message.reply_text(
            "Напишите заказ одним сообщением.\n\n"
            "Например:\n"
            "Лимассол → аэропорт Ларнака\n"
            "Сегодня 18:30\n"
            "Цена €55\n"
            "2 пассажира"
        )
        return

    if query.data.startswith("accept_"):
        order_id = int(query.data.split("_")[1])
        user = query.from_user

        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()

        cur.execute(
            "SELECT creator_id, text, status FROM orders WHERE id = ?",
            (order_id,),
        )
        order = cur.fetchone()

        if not order:
            conn.close()
            await query.message.reply_text("Заказ не найден.")
            return

        creator_id, order_text, status = order

        if status != "open":
            conn.close()
            await query.answer("Этот заказ уже приняли.", show_alert=True)
            return

        cur.execute(
            """
            UPDATE orders
            SET status = 'accepted', accepted_by = ?
            WHERE id = ? AND status = 'open'
            """,
            (user.id, order_id),
        )

        conn.commit()

        if cur.rowcount == 0:
            conn.close()
            await query.answer("Этот заказ уже приняли.", show_alert=True)
            return

        conn.close()

        name = user.full_name
        username = f"@{user.username}" if user.username else "без username"

        await query.edit_message_reply_markup(reply_markup=None)

        await query.message.reply_text(
            "✅ Вы приняли этот заказ."
        )

        try:
            await context.bot.send_message(
                chat_id=creator_id,
                text=(
                    "✅ Ваш заказ приняли!\n\n"
                    f"{order_text}\n\n"
                    f"Водитель: {name}\n"
                    f"Telegram: {username}"
                ),
            )
        except Exception:
            pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_order"):
        return

    context.user_data["waiting_for_order"] = False

    user = update.effective_user
    order_text = update.message.text

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO orders (creator_id, text, status)
        VALUES (?, ?, 'open')
        """,
        (user.id, order_text),
    )

    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ Принять заказ",
                callback_data=f"accept_{order_id}",
            )
        ]]
    )

    sent = 0

    for driver_id in get_drivers():
        if driver_id == user.id:
            continue

        try:
            await context.bot.send_message(
                chat_id=driver_id,
                text=(
                    "🚕 НОВЫЙ ЗАКАЗ\n\n"
                    f"{order_text}"
                ),
                reply_markup=keyboard,
            )
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ Заказ отправлен коллегам.\n"
        f"Получателей: {sent}"
    )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print("Taxi orders bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
