import os
import sqlite3
import threading
import schedule
import time
import random
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
import telebot
from telebot import types

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
YOOKASSA_TOKEN = os.getenv("YOOKASSA_TOKEN")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "20"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = telebot.TeleBot(TOKEN)

DB_PATH = os.path.join(os.path.dirname(__file__), "habits.db")

FREE_LIMIT = 3

# Варианты частоты: ключ → (короткий ярлык для статистики, max_gap в днях до сброса стрика)
FREQUENCY_OPTIONS: dict[str, dict] = {
    "ежедневно":  {"btn": "☀️ Каждый день",        "short": "каждый день",   "max_gap": 1},
    "через_день": {"btn": "🔁 Через день",           "short": "через день",    "max_gap": 2},
    "2р_нед":     {"btn": "2️⃣ 2 раза в неделю",    "short": "2р/нед",        "max_gap": 4},
    "3р_нед":     {"btn": "3️⃣ 3 раза в неделю",    "short": "3р/нед",        "max_gap": 3},
    "4р_нед":     {"btn": "4️⃣ 4 раза в неделю",    "short": "4р/нед",        "max_gap": 2},
    "5р_нед":     {"btn": "💼 5 раз в неделю",      "short": "5р/нед",        "max_gap": 3},
    "1р_нед":     {"btn": "📅 Раз в неделю",        "short": "1р/нед",        "max_gap": 8},
    "1р_мес":     {"btn": "🗓 Раз в месяц",         "short": "1р/мес",        "max_gap": 32},
}

# Хранение шага диалога: {user_id: {"step": ..., "data": ...}}
user_states: dict[int, dict] = {}

# ─────────────────────── МОТИВАЦИИ ───────────────────────

MOTIVATIONS_START = [
    "Каждый шаг вперёд — это победа. Ты молодец!",
    "Маленькие действия каждый день превращаются в большие результаты.",
    "Начало — это уже половина дела. Продолжай!",
    "Первый шаг сделан. Завтра будет легче.",
    "Движение — это жизнь. Отличный старт!",
]
MOTIVATIONS_GROWING = [
    "Привычка формируется. Ты на правильном пути!",
    "Стабильность — ключ к успеху. Держи ритм!",
    "Каждый день — это кирпичик в фундаменте твоего успеха.",
    "Ты уже лучше вчерашней версии себя. Так держать!",
    "Дисциплина — это любовь к себе. Ты это доказываешь!",
]
MOTIVATIONS_STREAK_7 = [
    "Неделя без перерыва! Ты — машина! 🔥",
    "7 дней — это уже настоящая привычка. Гордись собой!",
    "Целая неделя! Твой мозг уже перестраивается. Это мощно!",
]
MOTIVATIONS_STREAK_30 = [
    "30 дней! Это уже не просто привычка — это часть тебя. Легенда! 🏆",
    "Месяц без пропусков. Ты переписал свою жизнь. Серьёзно.",
    "30 дней — научно доказанный срок формирования привычки. Ты сделал это!",
]
MOTIVATIONS_STREAK_100 = [
    "100 дней! Ты — живое доказательство того, что воля побеждает всё. Эпик! 💎",
    "Сотня дней. Большинство людей даже не начинают. А ты — уже легенда.",
]


def get_motivation(streak: int) -> str:
    if streak >= 100:
        return random.choice(MOTIVATIONS_STREAK_100)
    if streak >= 30:
        return random.choice(MOTIVATIONS_STREAK_30)
    if streak >= 7:
        return random.choice(MOTIVATIONS_STREAK_7)
    if streak >= 3:
        return random.choice(MOTIVATIONS_GROWING)
    return random.choice(MOTIVATIONS_START)


def streak_emoji(streak: int) -> str:
    if streak >= 100:
        return "💎"
    if streak >= 30:
        return "🏆"
    if streak >= 7:
        return "🔥"
    if streak >= 3:
        return "⚡"
    return "✅"


# ─────────────────────── БАЗА ДАННЫХ ───────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                premium_until TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                frequency TEXT NOT NULL,
                streak INTEGER DEFAULT 0,
                last_check TEXT,
                UNIQUE(user_id, name)
            )
        """)
        # Миграция: добавить created_at если её нет
        try:
            conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        except Exception:
            pass
        conn.commit()


def ensure_user(user_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, premium_until, created_at) VALUES (?, NULL, ?)",
            (user_id, datetime.now().isoformat())
        )
        conn.commit()


def is_premium(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and row["premium_until"]:
            try:
                return datetime.fromisoformat(row["premium_until"]) > datetime.now()
            except ValueError:
                return False
    return False


def add_premium(user_id: int, days: int = 30):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and row["premium_until"]:
            try:
                current = datetime.fromisoformat(row["premium_until"])
                new_until = (current if current > datetime.now() else datetime.now()) + timedelta(days=days)
            except ValueError:
                new_until = datetime.now() + timedelta(days=days)
        else:
            new_until = datetime.now() + timedelta(days=days)
        conn.execute(
            "UPDATE users SET premium_until = ? WHERE user_id = ?",
            (new_until.isoformat(), user_id)
        )
        conn.commit()
    return new_until


def get_habits(user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM habits WHERE user_id = ? ORDER BY name",
            (user_id,)
        ).fetchall()


def get_habit_by_id(habit_id: int, user_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?",
            (habit_id, user_id)
        ).fetchone()


def count_habits(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM habits WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["cnt"] if row else 0


def add_habit(user_id: int, name: str, frequency: str) -> str:
    ensure_user(user_id)
    if not is_premium(user_id) and count_habits(user_id) >= FREE_LIMIT:
        return "limit"
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO habits (user_id, name, frequency, streak, last_check) VALUES (?, ?, ?, 0, NULL)",
                (user_id, name, frequency)
            )
            conn.commit()
            return "ok"
        except sqlite3.IntegrityError:
            return "exists"


def _should_reset_streak(frequency: str, last_check_str: str | None) -> bool:
    if not last_check_str:
        return False
    try:
        last = date.fromisoformat(last_check_str)
    except ValueError:
        return False
    delta = (date.today() - last).days
    max_gap = FREQUENCY_OPTIONS.get(frequency, {}).get("max_gap", 1)
    return delta > max_gap


def check_habit(user_id: int, habit_id: int) -> dict:
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
        ).fetchone()
        if not row:
            return {"status": "not_found"}
        if row["last_check"] == today:
            return {"status": "already", "name": row["name"]}
        new_streak = 1 if _should_reset_streak(row["frequency"], row["last_check"]) else row["streak"] + 1
        conn.execute(
            "UPDATE habits SET streak = ?, last_check = ? WHERE id = ? AND user_id = ?",
            (new_streak, today, habit_id, user_id)
        )
        conn.commit()
        return {"status": "ok", "name": row["name"], "streak": new_streak}


def delete_habit_by_id(user_id: int, habit_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id))
        conn.commit()
        return row["name"]


def get_unchecked_habits(user_id: int):
    today = date.today().isoformat()
    with get_conn() as conn:
        return conn.execute(
            "SELECT name FROM habits WHERE user_id = ? AND (last_check IS NULL OR last_check != ?)",
            (user_id, today)
        ).fetchall()


def all_user_ids():
    with get_conn() as conn:
        return [r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM habits").fetchall()]


def get_admin_stats() -> dict:
    today = date.today().isoformat()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    now_iso = datetime.now().isoformat()

    with get_conn() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_habits = conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0]
        premium_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE premium_until > ?", (now_iso,)
        ).fetchone()[0]
        new_users_week = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (week_ago,)
        ).fetchone()[0]
        active_today = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM habits WHERE last_check = ?", (today,)
        ).fetchone()[0]
        # Топ-5 популярных привычек
        top_habits = conn.execute(
            "SELECT name, COUNT(*) as cnt FROM habits GROUP BY name ORDER BY cnt DESC LIMIT 5"
        ).fetchall()
        # Распределение по частоте
        freq_dist = conn.execute(
            "SELECT frequency, COUNT(*) as cnt FROM habits GROUP BY frequency ORDER BY cnt DESC"
        ).fetchall()
        # Топ стрики
        top_streaks = conn.execute(
            "SELECT name, streak, user_id FROM habits ORDER BY streak DESC LIMIT 5"
        ).fetchall()

    return {
        "total_users": total_users,
        "total_habits": total_habits,
        "premium_users": premium_users,
        "new_users_week": new_users_week,
        "active_today": active_today,
        "top_habits": top_habits,
        "freq_dist": freq_dist,
        "top_streaks": top_streaks,
    }


# ─────────────────────── КЛАВИАТУРЫ ───────────────────────

def main_keyboard() -> types.ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу экрана."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        types.KeyboardButton("📊 Мои привычки"),
        types.KeyboardButton("➕ Добавить привычку"),
        types.KeyboardButton("⭐ Premium"),
        types.KeyboardButton("ℹ️ Помощь"),
    )
    return markup


def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    """Кнопка отмены во время диалога добавления."""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(types.KeyboardButton("❌ Отмена"))
    return markup


def frequency_inline() -> types.InlineKeyboardMarkup:
    """Выбор частоты привычки — все варианты."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(opt["btn"], callback_data=f"freq:{key}")
        for key, opt in FREQUENCY_OPTIONS.items()
    ]
    markup.add(*buttons)
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="freq:cancel"))
    return markup


def stats_inline(habits, today: str) -> types.InlineKeyboardMarkup:
    """Кнопки действий для каждой привычки в статистике."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    for h in habits:
        checked = h["last_check"] == today
        if checked:
            markup.add(
                types.InlineKeyboardButton(f"✅ {h['name']}", callback_data="noop"),
                types.InlineKeyboardButton("🗑 Удалить", callback_data=f"delete:{h['id']}"),
            )
        else:
            markup.add(
                types.InlineKeyboardButton(f"⭕ Отметить: {h['name']}", callback_data=f"check:{h['id']}"),
                types.InlineKeyboardButton("🗑", callback_data=f"delete:{h['id']}"),
            )
    markup.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="refresh_stats"))
    return markup


def confirm_delete_inline(habit_id: int, name: str) -> types.InlineKeyboardMarkup:
    """Подтверждение удаления привычки."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete:{habit_id}"),
        types.InlineKeyboardButton("↩️ Отмена", callback_data="refresh_stats"),
    )
    return markup


# ─────────────────────── ТЕКСТЫ ───────────────────────

def build_stats_text(user_id: int) -> str:
    habits = get_habits(user_id)
    premium = is_premium(user_id)
    today = date.today().isoformat()

    if not habits:
        return "📭 <b>Привычек пока нет</b>\n\nНажми <b>➕ Добавить привычку</b>, чтобы начать!"

    done = sum(1 for h in habits if h["last_check"] == today)
    total = len(habits)

    bar_filled = round(done / total * 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    lines = [
        f"📊 <b>Мои привычки — {date.today().strftime('%d.%m.%Y')}</b>",
        f"\nПрогресс сегодня: [{bar}] {done}/{total}\n",
    ]

    for h in habits:
        checked = h["last_check"] == today
        status = "✅" if checked else "⭕"
        freq_label = FREQUENCY_OPTIONS.get(h["frequency"], {}).get("short", h["frequency"])
        streak = h["streak"]

        if streak >= 100:
            badge = " 💎"
        elif streak >= 30:
            badge = " 🏆"
        elif streak >= 7:
            badge = " 🔥"
        else:
            badge = ""

        lines.append(
            f"{status} <b>{h['name']}</b>{badge}\n"
            f"   <i>{freq_label}</i> · серия: <b>{streak} дн.</b>"
        )

    if premium:
        lines.append("\n⭐ <b>Premium активен</b>")
    else:
        lines.append(f"\n🆓 Бесплатно: {count_habits(user_id)}/{FREE_LIMIT} привычек")

    return "\n".join(lines)


# ─────────────────────── HANDLERS: ГЛАВНОЕ МЕНЮ ───────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message.from_user.id)
    user_states.pop(message.from_user.id, None)
    name = message.from_user.first_name or "друг"
    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Я помогу тебе формировать полезные привычки и не сбиваться с пути.\n\n"
        "🎯 <b>Как это работает:</b>\n"
        "1. Добавь привычку — нажми <b>➕ Добавить привычку</b>\n"
        "2. Каждый день отмечай выполнение — нажми <b>📊 Мои привычки</b>\n"
        "3. Следи за серией дней и получай мотивацию!\n\n"
        "🆓 Бесплатно: до 3 привычек\n"
        "⭐ Premium: безлимит, 99 ₽/мес\n\n"
        "Используй кнопки меню внизу 👇"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard())


@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def menu_help(message):
    user_states.pop(message.from_user.id, None)
    text = (
        "ℹ️ <b>Как пользоваться ботом:</b>\n\n"
        "📊 <b>Мои привычки</b> — посмотреть список и отметить выполнение\n"
        "➕ <b>Добавить привычку</b> — создать новую привычку\n"
        "⭐ <b>Premium</b> — безлимитное количество привычек\n\n"
        "🔥 <b>Серия (streak)</b> — сколько дней подряд ты не пропускал привычку\n"
        "   Если пропустишь день (или 3 дня для 3р/нед) — серия сбросится\n\n"
        "🏅 <b>Значки серии:</b>\n"
        "   ⚡ 3+ дней · 🔥 7+ дней · 🏆 30+ дней · 💎 100+ дней\n\n"
        "⏰ Напоминания приходят ежедневно в 20:00"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard())


# ─────────────────────── HANDLERS: СТАТИСТИКА ───────────────────────

@bot.message_handler(func=lambda m: m.text == "📊 Мои привычки")
def menu_stats(message):
    user_states.pop(message.from_user.id, None)
    _show_stats(message.chat.id, message.from_user.id)


def _show_stats(chat_id: int, user_id: int, edit_message_id: int | None = None):
    habits = get_habits(user_id)
    text = build_stats_text(user_id)
    today = date.today().isoformat()

    if not habits:
        if edit_message_id:
            bot.edit_message_text(text, chat_id, edit_message_id, parse_mode="HTML")
        else:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=main_keyboard())
        return

    markup = stats_inline(habits, today)
    if edit_message_id:
        try:
            bot.edit_message_text(text, chat_id, edit_message_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
    else:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


# ─────────────────────── HANDLERS: ДОБАВЛЕНИЕ ПРИВЫЧКИ ───────────────────────

@bot.message_handler(func=lambda m: m.text == "➕ Добавить привычку")
def menu_add(message):
    user_id = message.from_user.id
    ensure_user(user_id)

    if not is_premium(user_id) and count_habits(user_id) >= FREE_LIMIT:
        text = (
            f"🔒 <b>Лимит достигнут</b>\n\n"
            f"На бесплатном аккаунте можно добавить не более {FREE_LIMIT} привычек.\n\n"
            "Нажми ⭐ <b>Premium</b>, чтобы снять ограничение!"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard())
        return

    user_states[user_id] = {"step": "awaiting_name"}
    bot.send_message(
        message.chat.id,
        "✏️ <b>Шаг 1/2</b> — Как назовём привычку?\n\n"
        "Напиши название, например:\n"
        "<i>Бег · Медитация · Читать 20 минут · Пить воду</i>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == "❌ Отмена")
def menu_cancel(message):
    user_states.pop(message.from_user.id, None)
    bot.send_message(
        message.chat.id,
        "↩️ Отменено. Возвращаемся в меню.",
        reply_markup=main_keyboard()
    )


@bot.message_handler(func=lambda m: user_states.get(m.from_user.id, {}).get("step") == "awaiting_name")
def handle_habit_name(message):
    user_id = message.from_user.id
    name = message.text.strip()

    if not name or len(name) > 50:
        bot.send_message(
            message.chat.id,
            "⚠️ Название должно быть от 1 до 50 символов. Попробуй ещё раз:",
            reply_markup=cancel_keyboard()
        )
        return

    user_states[user_id] = {"step": "awaiting_frequency", "name": name}
    bot.send_message(
        message.chat.id,
        f"✏️ <b>Шаг 2/2</b> — Как часто?\n\n"
        f"Привычка: <b>{name}</b>\n\n"
        "Выбери частоту:",
        parse_mode="HTML",
        reply_markup=cancel_keyboard()
    )
    bot.send_message(
        message.chat.id,
        "👇",
        reply_markup=frequency_inline()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("freq:"))
def callback_frequency(call):
    user_id = call.from_user.id
    freq = call.data.split(":")[1]

    if freq == "cancel":
        user_states.pop(user_id, None)
        bot.answer_callback_query(call.id, "Отменено")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(call.message.chat.id, "↩️ Отменено.", reply_markup=main_keyboard())
        return

    if freq not in FREQUENCY_OPTIONS:
        bot.answer_callback_query(call.id, "Неизвестная частота.")
        return

    state = user_states.get(user_id, {})
    if state.get("step") != "awaiting_frequency":
        bot.answer_callback_query(call.id, "Устаревший запрос. Начни заново.")
        return

    name = state.get("name", "")
    user_states.pop(user_id, None)

    result = add_habit(user_id, name, freq)

    freq_label = FREQUENCY_OPTIONS[freq]["btn"]
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

    if result == "ok":
        bot.answer_callback_query(call.id, "✅ Привычка добавлена!")
        bot.send_message(
            call.message.chat.id,
            f"🎉 <b>Привычка добавлена!</b>\n\n"
            f"📌 <b>{name}</b>\n"
            f"📅 Частота: {freq_label}\n\n"
            f"Отмечай выполнение в <b>📊 Мои привычки</b>!",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    elif result == "exists":
        bot.answer_callback_query(call.id, "Уже существует")
        bot.send_message(
            call.message.chat.id,
            f"⚠️ Привычка <b>{name}</b> уже есть в твоём списке.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
    elif result == "limit":
        bot.answer_callback_query(call.id, "Лимит достигнут")
        bot.send_message(
            call.message.chat.id,
            f"🔒 Лимит {FREE_LIMIT} привычек достигнут. Получи ⭐ Premium!",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


# ─────────────────────── HANDLERS: INLINE-ДЕЙСТВИЯ В СТАТИСТИКЕ ───────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("check:"))
def callback_check(call):
    user_id = call.from_user.id
    try:
        habit_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Ошибка.")
        return

    result = check_habit(user_id, habit_id)

    if result["status"] == "not_found":
        bot.answer_callback_query(call.id, "Привычка не найдена.")
    elif result["status"] == "already":
        bot.answer_callback_query(call.id, f"✅ Уже отмечено сегодня!", show_alert=False)
    else:
        streak = result["streak"]
        emoji = streak_emoji(streak)
        motivation = get_motivation(streak)
        bot.answer_callback_query(call.id, f"{emoji} Streak: {streak} дн.!", show_alert=False)
        _show_stats(call.message.chat.id, user_id, call.message.message_id)
        bot.send_message(
            call.message.chat.id,
            f"{emoji} <b>{result['name']}</b> — выполнено!\n"
            f"🗓 Серия: <b>{streak} дн.</b>\n\n"
            f"💬 <i>{motivation}</i>",
            parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("delete:"))
def callback_delete(call):
    user_id = call.from_user.id
    try:
        habit_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Ошибка.")
        return

    habit = get_habit_by_id(habit_id, user_id)
    if not habit:
        bot.answer_callback_query(call.id, "Привычка не найдена.")
        return

    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        f"🗑 <b>Удалить привычку?</b>\n\n"
        f"«<b>{habit['name']}</b>»\n\n"
        f"Серия {habit['streak']} дней будет потеряна безвозвратно.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML",
        reply_markup=confirm_delete_inline(habit_id, habit["name"])
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_delete:"))
def callback_confirm_delete(call):
    user_id = call.from_user.id
    try:
        habit_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Ошибка.")
        return

    name = delete_habit_by_id(user_id, habit_id)
    if name:
        bot.answer_callback_query(call.id, f"🗑 «{name}» удалена")
        _show_stats(call.message.chat.id, user_id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "Не удалось удалить.")


@bot.callback_query_handler(func=lambda c: c.data == "refresh_stats")
def callback_refresh(call):
    bot.answer_callback_query(call.id, "Обновлено!")
    _show_stats(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data == "noop")
def callback_noop(call):
    bot.answer_callback_query(call.id, "✅ Уже выполнено сегодня!")


# ─────────────────────── HANDLERS: PREMIUM ───────────────────────

@bot.message_handler(func=lambda m: m.text == "⭐ Premium")
def menu_premium(message):
    user_states.pop(message.from_user.id, None)
    _show_premium(message.chat.id, message.from_user.id)


def _show_premium(chat_id: int, user_id: int):
    if not YOOKASSA_TOKEN:
        bot.send_message(
            chat_id,
            "⚠️ Оплата временно недоступна. Попробуйте позже.",
            reply_markup=main_keyboard()
        )
        return

    if is_premium(user_id):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT premium_until FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        until = datetime.fromisoformat(row["premium_until"]).strftime("%d.%m.%Y")
        bot.send_message(
            chat_id,
            f"⭐ <b>Premium активен до {until}</b>\n\n"
            "Оплата продлит подписку ещё на 30 дней.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )
        return

    try:
        prices = [types.LabeledPrice(label="Premium 30 дней", amount=9900)]
        bot.send_invoice(
            chat_id=chat_id,
            title="⭐ Premium подписка",
            description="Неограниченное количество привычек на 30 дней",
            invoice_payload="premium_30_days",
            provider_token=YOOKASSA_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="premium",
            photo_url="https://i.imgur.com/4M34hi2.png",
            photo_width=600,
            photo_height=300,
            need_name=False,
            need_phone_number=False,
            need_email=False,
            is_flexible=False,
        )
    except Exception as e:
        bot.send_message(
            chat_id,
            f"❌ Ошибка при создании счёта. Попробуйте позже.\n<code>{e}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


@bot.pre_checkout_query_handler(func=lambda q: True)
def handle_pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    user_id = message.from_user.id
    if message.successful_payment.invoice_payload == "premium_30_days":
        new_until = add_premium(user_id, days=30)
        bot.send_message(
            message.chat.id,
            f"🎉 <b>Оплата прошла успешно!</b>\n\n"
            f"⭐ Premium активирован до <b>{new_until.strftime('%d.%m.%Y')}</b>\n"
            "Теперь можешь добавлять неограниченное количество привычек!",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )


# ─────────────────────── КОМАНДЫ УТИЛИТЫ ───────────────────────

@bot.message_handler(commands=["myid"])
def cmd_myid(message):
    uid = message.from_user.id
    bot.send_message(
        message.chat.id,
        f"🪪 Твой Telegram ID: <code>{uid}</code>\n\n"
        "Скопируй это число — оно нужно для настройки доступа.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "⛔ Нет доступа.", reply_markup=main_keyboard())
        return

    s = get_admin_stats()

    # Топ привычек
    top_h = "\n".join(
        f"  {i+1}. {r['name']} — {r['cnt']} польз."
        for i, r in enumerate(s["top_habits"])
    ) or "  —"

    # Распределение частот
    freq_lines = "\n".join(
        f"  • {FREQUENCY_OPTIONS.get(r['frequency'], {}).get('short', r['frequency'])}: {r['cnt']}"
        for r in s["freq_dist"]
    ) or "  —"

    # Топ стрики
    streak_lines = "\n".join(
        f"  {i+1}. {r['name']} — {r['streak']} дн. (id:{r['user_id']})"
        for i, r in enumerate(s["top_streaks"])
    ) or "  —"

    text = (
        f"📊 <b>Админ-панель — {date.today().strftime('%d.%m.%Y')}</b>\n"
        f"{'─' * 30}\n\n"
        f"👥 <b>Пользователи</b>\n"
        f"  Всего: <b>{s['total_users']}</b>\n"
        f"  Новых за 7 дней: <b>{s['new_users_week']}</b>\n"
        f"  Активны сегодня: <b>{s['active_today']}</b>\n"
        f"  ⭐ Premium: <b>{s['premium_users']}</b>\n\n"
        f"📌 <b>Привычки</b>\n"
        f"  Всего создано: <b>{s['total_habits']}</b>\n\n"
        f"🏆 <b>Топ-5 названий:</b>\n{top_h}\n\n"
        f"📅 <b>По частоте:</b>\n{freq_lines}\n\n"
        f"🔥 <b>Топ стрики:</b>\n{streak_lines}"
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard())


# ─────────────────────── НАПОМИНАНИЯ ───────────────────────

def send_reminders():
    for user_id in all_user_ids():
        unchecked = get_unchecked_habits(user_id)
        if unchecked:
            names = "\n".join(f"  • {h['name']}" for h in unchecked)
            try:
                bot.send_message(
                    user_id,
                    f"⏰ <b>Вечернее напоминание</b>\n\n"
                    f"Ты ещё не отметил сегодня:\n{names}\n\n"
                    "Открой 📊 <b>Мои привычки</b> и отметь выполнение!",
                    parse_mode="HTML",
                    reply_markup=main_keyboard()
                )
            except Exception:
                pass


def run_scheduler():
    schedule.every().day.at(f"{REMINDER_HOUR:02d}:00").do(send_reminders)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ─────────────────────── ЗАПУСК ───────────────────────

if __name__ == "__main__":
    init_db()
    print("✅ База данных инициализирована")

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print(f"⏰ Планировщик запущен (каждый день в {REMINDER_HOUR:02d}:00)")

    print("🤖 Бот запущен...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
