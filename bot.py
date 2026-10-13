1
2
3
4
5
6
7
8
9
10
11
12
13
14
15
16
17
18
19
20
21
22
23
24
25
26
27
28
29
30
31
32
33
34
35
36
37
38
39
40
41
42
43
44
45
46
47
48
49
50
51
52
53
54
55
56
57
58
59
60
61
62
63
64
65
66
67
68
69
70
71
72
73
74
75
76
77
78
79
80
81
82
83
84
85
86
87
88
89
90
91
92
93
94
95
96
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
    KeyboardButton,
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
