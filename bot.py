import os
import sqlite3
import logging

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


# ==========================================
# НАСТРОЙКИ
# ==========================================

TOKEN = os.getenv("BOT_TOKEN")

# Группа «Κούρσες δωρεάν»
GROUP_ID = -1004449292276

# Если подключим Railway Volume к /data,
# база будет храниться именно там.
DATA_DIR = os.getenv("DATA_DIR", ".")

os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "taxi_bot.db")

NEW_ORDER_BUTTON = "🚕 Νέα διαδρομή"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Кто нажал «Новый заказ» и сейчас пишет его
waiting_for_order = set()


# ==========================================
# БАЗА ДАННЫХ
# ==========================================

def get_db():
    return sqlite3.connect(
        DB_PATH,
        timeout=30
    )


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                creator_name TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                accepted_by INTEGER,
                accepted_name TEXT,
                message_id INTEGER
            )
            """
        )

        conn.commit()


# ==========================================
# ПОСТОЯННАЯ КНОПКА
# ==========================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[NEW_ORDER_BUTTON]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Γράψε μήνυμα...",
    )


# ==========================================
# /START
# ==========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    # В нашей группе
    if update.effective_chat.id == GROUP_ID:
        await update.message.reply_text(
            "🚕 Το σύστημα διαδρομών είναι ενεργό.",
            reply_markup=main_keyboard(),
        )
        return

    # В личном чате
    await update.message.reply_text(
        "🚕 Cyprus Drivers Orders\n\n"
        "Οι διαδρομές δίνονται μέσα από την ομάδα "
        "«Κούρσες δωρεάν»."
    )


# ==========================================
# СООБЩЕНИЯ ГРУППЫ
# ==========================================

async def group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    text = update.message.text

    if not text:
        return

    # --------------------------------------
    # Нажали постоянную кнопку
    # --------------------------------------

    if text == NEW_ORDER_BUTTON:

        waiting_for_order.add(user.id)

        # Telegram отправляет текст кнопки как сообщение.
        # Сразу удаляем его, чтобы чат оставался чистым.
        try:
            await update.message.delete()
        except Exception:
            pass

        return

    # --------------------------------------
    # Обычная переписка
    # --------------------------------------

    if user.id not in waiting_for_order:
        return

    # --------------------------------------
    # Это новый заказ
    # --------------------------------------

    waiting_for_order.discard(user.id)

    # Сохраняем заказ в базе
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO orders (
                creator_id,
                creator_name,
                text,
                status
            )
            VALUES (?, ?, ?, 'open')
            """,
            (
                user.id,
                user.full_name,
                text,
            ),
        )

        order_id = cursor.lastrowid
        conn.commit()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Πάρε τη διαδρομή",
                    callback_data=f"accept_{order_id}",
                )
            ]
        ]
    )

    # Удаляем исходный текст водителя,
    # чтобы заказ не дублировался.
    try:
        await update.message.delete()
    except Exception:
        pass

    # Публикуем одно чистое сообщение заказа
    sent_message = await context.bot.send_message(
        chat_id=GROUP_ID,
        text=(
            "🚕 ΝΕΑ ΔΙΑΔΡΟΜΗ\n\n"
            f"{text}\n\n"
            f"👤 {user.full_name}"
        ),
        reply_markup=keyboard,
    )

    # Запоминаем Telegram message_id
    with get_db() as conn:
        conn.execute(
            """
            UPDATE orders
            SET message_id = ?
            WHERE id = ?
            """,
            (
                sent_message.message_id,
                order_id,
            ),
        )

        conn.commit()


# ==========================================
# ПРИНЯТЬ ЗАКАЗ
# ==========================================

async def accept_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    user = query.from_user

    try:
        order_id = int(
            query.data.split("_")[1]
        )
    except (ValueError, IndexError):
        await query.answer()
        return

    # BEGIN IMMEDIATE нужен, чтобы два водителя
    # не смогли одновременно забрать один заказ.
    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        order = conn.execute(
            """
            SELECT
                creator_id,
                creator_name,
                text,
                status
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

        if not order:
            conn.rollback()

            await query.answer(
                "Η διαδρομή δεν είναι πλέον διαθέσιμη.",
                show_alert=True,
            )
            return

        creator_id = order[0]
        order_text = order[2]
        status = order[3]

        # Нельзя взять свой заказ
        if user.id == creator_id:
            conn.rollback()

            await query.answer(
                "Δεν μπορείς να πάρεις τη δική σου διαδρομή.",
                show_alert=True,
            )
            return

        # Уже забрали
        if status != "open":
            conn.rollback()

            await query.answer(
                "Η διαδρομή έχει ήδη δοθεί.",
                show_alert=True,
            )
            return

        # Закрепляем заказ за первым водителем
        conn.execute(
            """
            UPDATE orders
            SET
                status = 'accepted',
                accepted_by = ?,
                accepted_name = ?
            WHERE id = ?
              AND status = 'open'
            """,
            (
                user.id,
                user.full_name,
                order_id,
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    await query.answer(
        "Η διαδρομή είναι δική σου! ✅"
    )

    username = (
        f"@{user.username}"
        if user.username
        else ""
    )

    # То же сообщение меняется,
    # а кнопка «принять» исчезает.
    await query.edit_message_text(
        text=(
            "✅ Η ΔΙΑΔΡΟΜΗ ΔΟΘΗΚΕ\n\n"
            f"{order_text}\n\n"
            f"🚕 Την πήρε: {user.full_name}"
            + (
                f" ({username})"
                if username
                else ""
            )
        )
    )

    # Личное уведомление автору,
    # если он раньше открывал чат с ботом.
    try:
        await context.bot.send_message(
            chat_id=creator_id,
            text=(
                "✅ Η διαδρομή σου δόθηκε.\n\n"
                f"🚕 Οδηγός: {user.full_name}"
                + (
                    f"\nTelegram: {username}"
                    if username
                    else ""
                )
            ),
        )
    except Exception:
        pass


# ==========================================
# ОШИБКИ
# ==========================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Exception while handling an update:",
        exc_info=context.error,
    )


# ==========================================
# ЗАПУСК
# ==========================================

def main():

    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set"
        )

    init_db()

    logger.info(
        "Database: %s",
        DB_PATH,
    )

    app = (
        Application.builder()
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
        CallbackQueryHandler(
            accept_order,
            pattern=r"^accept_\d+$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & filters.TEXT
            & ~filters.COMMAND,
            group_message,
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
