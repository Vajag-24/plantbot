import asyncio
import logging
import sqlite3
import os
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN", "8748512036:AAFIJZHmN_rwie1Pd7iUwWp-RiphzJkwcHc")
ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else set()

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Asia/Almaty")

# ─── Database ─────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect("plants.db")
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '🌱',
            interval_days INTEGER NOT NULL DEFAULT 7,
            last_watered TEXT,
            method TEXT DEFAULT '',
            tip TEXT DEFAULT '',
            color TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            watered_by TEXT NOT NULL,
            watered_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def get_con():
    return sqlite3.connect("plants.db")

def register_chat(chat_id):
    with get_con() as con:
        con.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))

def get_plants(chat_id):
    with get_con() as con:
        cur = con.execute(
            "SELECT id, name, emoji, interval_days, last_watered, method, tip FROM plants WHERE chat_id=? ORDER BY id",
            (chat_id,)
        )
        return cur.fetchall()

def get_plant(plant_id):
    with get_con() as con:
        cur = con.execute(
            "SELECT id, name, emoji, interval_days, last_watered, method, tip, chat_id FROM plants WHERE id=?",
            (plant_id,)
        )
        return cur.fetchone()

def add_plant(chat_id, name, emoji, interval_days, method, tip):
    with get_con() as con:
        cur = con.execute(
            "INSERT INTO plants (chat_id, name, emoji, interval_days, method, tip) VALUES (?,?,?,?,?,?)",
            (chat_id, name, emoji, interval_days, method, tip)
        )
        return cur.lastrowid

def delete_plant(plant_id):
    with get_con() as con:
        con.execute("DELETE FROM plants WHERE id=?", (plant_id,))
        con.execute("DELETE FROM history WHERE plant_id=?", (plant_id,))

def water_plant(plant_id, username):
    today = date.today().isoformat()
    with get_con() as con:
        con.execute("UPDATE plants SET last_watered=? WHERE id=?", (today, plant_id))
        con.execute(
            "INSERT INTO history (plant_id, chat_id, watered_by, watered_at) SELECT id, chat_id, ?, ? FROM plants WHERE id=?",
            (username, today, plant_id)
        )

def get_history(chat_id, limit=20):
    with get_con() as con:
        cur = con.execute(
            """SELECT p.emoji, p.name, h.watered_by, h.watered_at
               FROM history h JOIN plants p ON h.plant_id=p.id
               WHERE h.chat_id=? ORDER BY h.watered_at DESC, h.id DESC LIMIT ?""",
            (chat_id, limit)
        )
        return cur.fetchall()

def get_stats(chat_id):
    with get_con() as con:
        cur = con.execute(
            """SELECT p.emoji, p.name, COUNT(h.id) as cnt
               FROM plants p LEFT JOIN history h ON p.id=h.plant_id
               WHERE p.chat_id=? GROUP BY p.id ORDER BY cnt DESC""",
            (chat_id,)
        )
        return cur.fetchall()

def get_all_chats():
    with get_con() as con:
        cur = con.execute("SELECT chat_id FROM chats")
        return [r[0] for r in cur.fetchall()]

# ─── Helpers ──────────────────────────────────────────────────────
def days_until_next(last_watered, interval_days):
    if not last_watered:
        return None
    last = date.fromisoformat(last_watered)
    next_date = last + timedelta(days=interval_days)
    return (next_date - date.today()).days

def status_emoji(days):
    if days is None: return "❓"
    if days < 0:  return "🔴"
    if days == 0: return "🟡"
    if days <= 2: return "🟠"
    return "🟢"

def format_status(days):
    if days is None: return "не поливали"
    if days < 0:  return f"просрочен на {abs(days)} д."
    if days == 0: return "сегодня!"
    if days == 1: return "завтра"
    return f"через {days} д."

def can_water(days):
    return days is None or days <= 0

def build_plant_card(plant):
    pid, name, emoji, interval, last_watered, method, tip = plant
    days = days_until_next(last_watered, interval)
    se = status_emoji(days)
    st = format_status(days)
    last_str = date.fromisoformat(last_watered).strftime("%-d %b").lower() if last_watered else "—"

    text = f"{se} *{emoji} {name}*\n"
    text += f"   Полив: {st}\n"
    text += f"   Последний: {last_str} · каждые {interval} д."
    if method:
        text += f"\n\n📋 _{method}_"
    if tip:
        text += f"\n💡 _{tip}_"
    return text

# ─── Keyboards ────────────────────────────────────────────────────
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💧 Полить", callback_data="action:water")
    kb.button(text="📋 Статус", callback_data="action:status")
    kb.button(text="📊 Статистика", callback_data="action:stats")
    kb.button(text="📜 История", callback_data="action:history")
    kb.button(text="➕ Добавить", callback_data="action:add")
    kb.button(text="🗑 Удалить", callback_data="action:delete")
    kb.adjust(2, 2, 2)
    return kb.as_markup()

def plants_water_kb(plants):
    kb = InlineKeyboardBuilder()
    for pid, name, emoji, interval, last_watered, *_ in plants:
        days = days_until_next(last_watered, interval)
        se = status_emoji(days)
        if can_water(days):
            kb.button(text=f"{se} {emoji} {name}", callback_data=f"water:{pid}")
        else:
            kb.button(text=f"{se} {emoji} {name} — {format_status(days)}", callback_data=f"skip:{pid}")
    kb.button(text="◀️ Назад", callback_data="action:menu")
    kb.adjust(1)
    return kb.as_markup()

def plants_delete_kb(plants):
    kb = InlineKeyboardBuilder()
    for pid, name, emoji, *_ in plants:
        kb.button(text=f"🗑 {emoji} {name}", callback_data=f"confirmdelete:{pid}")
    kb.button(text="◀️ Назад", callback_data="action:menu")
    kb.adjust(1)
    return kb.as_markup()

def confirm_delete_kb(plant_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"dodelete:{plant_id}")
    kb.button(text="❌ Отмена", callback_data="action:menu")
    kb.adjust(2)
    return kb.as_markup()

def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню", callback_data="action:menu")
    return kb.as_markup()

# ─── State for adding plants ──────────────────────────────────────
add_state = {}  # chat_id -> step data

# ─── Status text ─────────────────────────────────────────────────
def build_status_text(plants, title="🌿 *Мои растения*"):
    if not plants:
        return "Растений пока нет. Нажми ➕ Добавить"
    lines = [title, ""]
    for p in plants:
        pid, name, emoji, interval, last_watered, *_ = p
        days = days_until_next(last_watered, interval)
        se = status_emoji(days)
        st = format_status(days)
        last_str = date.fromisoformat(last_watered).strftime("%-d %b").lower() if last_watered else "—"
        lines.append(f"{se} {emoji} *{name}*")
        lines.append(f"   _{st}_ · последний: {last_str}")
    return "\n".join(lines)

# ─── Handlers ────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    register_chat(msg.chat.id)
    plants = get_plants(msg.chat.id)
    text = build_status_text(plants, "🌿 *Привет! Я трекер полива*\n\nВот ваши растения:")
    await msg.answer(text, parse_mode="Markdown", reply_markup=main_kb())

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    register_chat(msg.chat.id)
    plants = get_plants(msg.chat.id)
    text = build_status_text(plants)
    await msg.answer(text, parse_mode="Markdown", reply_markup=main_kb())

# ─── Callback handlers ────────────────────────────────────────────
@dp.callback_query(F.data == "action:menu")
async def cb_menu(cb: CallbackQuery):
    plants = get_plants(cb.message.chat.id)
    text = build_status_text(plants)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=main_kb())
    await cb.answer()

@dp.callback_query(F.data == "action:status")
async def cb_status(cb: CallbackQuery):
    plants = get_plants(cb.message.chat.id)
    text = build_status_text(plants)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=main_kb())
    await cb.answer()

@dp.callback_query(F.data == "action:water")
async def cb_water_list(cb: CallbackQuery):
    plants = get_plants(cb.message.chat.id)
    if not plants:
        await cb.answer("Нет растений!", show_alert=True)
        return
    text = "💧 *Выбери что полить:*\n\nСерые кнопки — ещё не время, подожди"
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=plants_water_kb(plants))
    await cb.answer()

@dp.callback_query(F.data.startswith("water:"))
async def cb_do_water(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = get_plant(plant_id)
    if not plant:
        await cb.answer("Растение не найдено", show_alert=True)
        return
    pid, name, emoji, interval, last_watered, method, tip, chat_id = plant
    days = days_until_next(last_watered, interval)
    if not can_water(days):
        await cb.answer(f"Рано! Следующий полив {format_status(days)}", show_alert=True)
        return
    username = cb.from_user.full_name or cb.from_user.username or "кто-то"
    water_plant(plant_id, username)
    plants = get_plants(cb.message.chat.id)
    text = build_status_text(plants)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=main_kb())
    await cb.answer(f"✅ {emoji} {name} полит!")
    # Notify chat
    next_date = (date.today() + timedelta(days=interval)).strftime("%-d %b")
    await bot.send_message(
        cb.message.chat.id,
        f"💧 *{username}* полил {emoji} *{name}*\nСледующий полив: {next_date}",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = get_plant(plant_id)
    if plant:
        days = days_until_next(plant[4], plant[3])
        await cb.answer(f"Рано! {format_status(days)}", show_alert=True)
    else:
        await cb.answer("Не найдено", show_alert=True)

@dp.callback_query(F.data == "action:history")
async def cb_history(cb: CallbackQuery):
    history = get_history(cb.message.chat.id)
    if not history:
        text = "📜 История поливов пуста"
    else:
        lines = ["📜 *Последние поливы:*", ""]
        for emoji, name, by, at in history:
            d = date.fromisoformat(at).strftime("%-d %b").lower()
            lines.append(f"{emoji} {name} — {d} ({by})")
        text = "\n".join(lines)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "action:stats")
async def cb_stats(cb: CallbackQuery):
    stats = get_stats(cb.message.chat.id)
    if not stats:
        text = "📊 Статистика пуста"
    else:
        lines = ["📊 *Статистика поливов:*", ""]
        for emoji, name, cnt in stats:
            lines.append(f"{emoji} {name} — {cnt} раз")
        text = "\n".join(lines)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "action:delete")
async def cb_delete_list(cb: CallbackQuery):
    plants = get_plants(cb.message.chat.id)
    if not plants:
        await cb.answer("Нет растений!", show_alert=True)
        return
    await cb.message.edit_text("🗑 *Выбери растение для удаления:*", parse_mode="Markdown",
                                reply_markup=plants_delete_kb(plants))
    await cb.answer()

@dp.callback_query(F.data.startswith("confirmdelete:"))
async def cb_confirm_delete(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = get_plant(plant_id)
    if not plant:
        await cb.answer("Не найдено", show_alert=True)
        return
    name, emoji = plant[1], plant[2]
    await cb.message.edit_text(
        f"Удалить {emoji} *{name}*?\nВся история полива тоже удалится.",
        parse_mode="Markdown",
        reply_markup=confirm_delete_kb(plant_id)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("dodelete:"))
async def cb_do_delete(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = get_plant(plant_id)
    name = plant[1] if plant else "растение"
    emoji = plant[2] if plant else "🌱"
    delete_plant(plant_id)
    plants = get_plants(cb.message.chat.id)
    text = build_status_text(plants)
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=main_kb())
    await cb.answer(f"🗑 {emoji} {name} удалён")

# ─── Add plant flow ───────────────────────────────────────────────
@dp.callback_query(F.data == "action:add")
async def cb_add_start(cb: CallbackQuery):
    add_state[cb.message.chat.id] = {"step": "name", "msg_id": cb.message.message_id}
    await cb.message.edit_text(
        "➕ *Добавляем растение*\n\nШаг 1/4: Напиши *название* растения",
        parse_mode="Markdown"
    )
    await cb.answer()

@dp.message(F.text)
async def handle_text(msg: types.Message):
    chat_id = msg.chat.id
    state = add_state.get(chat_id)
    if not state:
        return

    step = state["step"]

    if step == "name":
        state["name"] = msg.text.strip()
        state["step"] = "emoji"
        await msg.answer("Шаг 2/4: Отправь *эмодзи* для этого растения (например 🌴 🌵 🌿 🌾 🌺 🪴)",
                         parse_mode="Markdown")

    elif step == "emoji":
        state["emoji"] = msg.text.strip()[:2] or "🌱"
        state["step"] = "interval"
        await msg.answer("Шаг 3/4: Каждые сколько *дней* поливать? Напиши число",
                         parse_mode="Markdown")

    elif step == "interval":
        try:
            days = int(msg.text.strip())
            if days < 1 or days > 365:
                raise ValueError
        except ValueError:
            await msg.answer("Введи число от 1 до 365")
            return
        state["interval"] = days
        state["step"] = "method"
        await msg.answer("Шаг 4/4: Напиши *способ полива* (или отправь `-` чтобы пропустить)",
                         parse_mode="Markdown")

    elif step == "method":
        method = "" if msg.text.strip() == "-" else msg.text.strip()
        state["method"] = method
        state["step"] = "tip"
        await msg.answer("Последнее: напиши *заметку/совет* (или `-` пропустить)",
                         parse_mode="Markdown")

    elif step == "tip":
        tip = "" if msg.text.strip() == "-" else msg.text.strip()
        name = state["name"]
        emoji = state["emoji"]
        interval = state["interval"]
        method = state["method"]

        add_plant(chat_id, name, emoji, interval, method, tip)
        del add_state[chat_id]

        plants = get_plants(chat_id)
        text = build_status_text(plants)
        await msg.answer(
            f"✅ {emoji} *{name}* добавлен!\n\n{text}",
            parse_mode="Markdown",
            reply_markup=main_kb()
        )

# ─── Daily reminder ───────────────────────────────────────────────
async def daily_check():
    chats = get_all_chats()
    for chat_id in chats:
        plants = get_plants(chat_id)
        due = []
        for p in plants:
            pid, name, emoji, interval, last_watered, *_ = p
            days = days_until_next(last_watered, interval)
            if days is not None and days <= 0:
                due.append(f"{emoji} {name}")
            elif days is None:
                due.append(f"{emoji} {name} (не поливали)")
        if due:
            text = "🔔 *Пора поливать!*\n\n" + "\n".join(due)
            try:
                await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=main_kb())
            except Exception as e:
                logging.warning(f"Can't send to {chat_id}: {e}")

# ─── Main ─────────────────────────────────────────────────────────
async def main():
    init_db()
    scheduler.add_job(daily_check, "cron", hour=9, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
