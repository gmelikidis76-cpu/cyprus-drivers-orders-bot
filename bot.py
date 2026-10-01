import os
import re
import sqlite3
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")

# База данных хранит заказы и не позволяет двум водителям
# одновременно взять один и тот же заказ.
db = sqlite3.connect("orders.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    original_text TEXT NOT NULL,
    author_id INTEGER,
    author_name TEXT,
    taken_by_id INTEGER,
    taken_by_name TEXT,
    created_at TEXT
)
""")
db.commit()


def driver_name(user):
    if user.username:
        return f"@{user.username}"

    name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()

    return name or f"Driver {user.id}"


def looks_like_order(text):
    """
    Принимаем обычные сообщения водителей.
    Слишком короткие сообщения типа 'ok', 'test', '+' игнорируем.
    """
    if not text:
        return False

    text = text.strip()

    if len(text) < 8:
        return False

    ignored = {
        "test", "ok", "okay", "thanks", "thank you",
        "ευχαριστώ", "οκ", "καλημέρα", "καλησπέρα"
    }

    if text.lower() in ignored:
        return False

    return True


async def new_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    # Не реагируем на команды Telegram
    if message.text.startswith("/"):
        return

    if not looks_like_order(message.text):
        return

    text = message.text.strip()
    author = driver_name(user)

    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO orders
        (chat_id, original_text, author_id, author_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            message.chat_id,
            text,
            user.id,
            author,
            datetime.utcnow().isoformat(),
        ),
    )
    order_id = cursor.lastrowid
    db.commit()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚕 ΑΝΑΛΑΜΒΑΝΩ",
                callback_data=f"take:{order_id}"
            )
        ]
    ])

    order_message = (
        "🚖 ΝΕΑ ΚΡΑΤΗΣΗ\n\n"
        f"📍 {text}\n\n"
        "🟢 Διαθέσιμη\n"
        "👇 Πατήστε το κουμπί για να την αναλάβετε."
    )

    sent = await message.reply_text(
        order_message,
        reply_markup=keyboard
    )

    db.execute(
        "UPDATE orders SET message_id = ? WHERE id = ?",
        (sent.message_id, order_id),
    )
    db.commit()


async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    match = re.match(r"take:(\d+)", query.data or "")
    if not match:
        return

    order_id = int(match.group(1))
    user = query.from_user
    name = driver_name(user)

    cursor = db.cursor()

    # BEGIN IMMEDIATE блокирует одновременное взятие одного заказа.
    cursor.execute("BEGIN IMMEDIATE")

    cursor.execute(
        """
        SELECT original_text, taken_by_id, taken_by_name
        FROM orders
        WHERE id = ?
        """,
        (order_id,),
    )

    order = cursor.fetchone()

    if not order:
        db.rollback()
        await query.answer(
            "Η κράτηση δεν βρέθηκε.",
            show_alert=True
        )
        return

    original_text, taken_by_id, taken_by_name = order

    if taken_by_id:
        db.rollback()

        if taken_by_id == user.id:
            await query.answer(
                "Έχετε ήδη αναλάβει αυτή την κράτηση.",
                show_alert=True
            )
        else:
            await query.answer(
                f"Η κράτηση έχει ήδη αναληφθεί από {taken_by_name}.",
                show_alert=True
            )
        return

    cursor.execute(
        """
        UPDATE orders
        SET taken_by_id = ?, taken_by_name = ?
        WHERE id = ? AND taken_by_id IS NULL
        """,
        (user.id, name, order_id),
    )

    if cursor.rowcount != 1:
        db.rollback()
        await query.answer(
            "Η κράτηση μόλις αναλήφθηκε από άλλον οδηγό.",
            show_alert=True
        )
        return

    db.commit()

    completed_message = (
        "🚖 ΚΡΑΤΗΣΗ\n\n"
        f"📍 {original_text}\n\n"
        "🔴 ΑΝΑΛΗΦΘΗΚΕ\n"
        f"👤 Οδηγός: {name}"
    )

    await query.edit_message_text(completed_message)

    # Отдельное сообщение в группе — все увидят, кто взял заказ.
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"✅ Η κράτηση αναλήφθηκε από τον οδηγό {name}."
    )


def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not configured. Add it as an environment variable."
        )

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CallbackQueryHandler(take_order, pattern=r"^take:\d+$")
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            new_order
        )
    )

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
