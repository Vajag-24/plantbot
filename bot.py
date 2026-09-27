import asyncio
import logging
import os
from datetime import date, timedelta
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InaccessibleMessage, ErrorEvent
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан в переменных окружения")

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone="Asia/Almaty")
pool = None

# ─── Database ─────────────────────────────────────────────────────
async def init_db():
    global pool
    # max_inactive_connection_lifetime — чтобы после рестарта Postgres
    # в пуле не оставались мёртвые соединения
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=15,
        max_inactive_connection_lifetime=300,
    )
    async with pool.acquire() as con:
        await con.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS plants (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '🌱',
                interval_days INTEGER NOT NULL DEFAULT 7,
                last_watered TEXT,
                method TEXT DEFAULT '',
                tip TEXT DEFAULT ''
            )
        """)
        await con.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id SERIAL PRIMARY KEY,
                plant_id INTEGER NOT NULL,
                chat_id BIGINT NOT NULL,
                watered_by TEXT NOT NULL,
                watered_at TEXT NOT NULL
            )
        """)

async def register_chat(chat_id):
    async with pool.acquire() as con:
        await con.execute("INSERT INTO chats(chat_id) VALUES($1) ON CONFLICT DO NOTHING", chat_id)

async def get_plants(chat_id):
    async with pool.acquire() as con:
        return await con.fetch(
            "SELECT id, name, emoji, interval_days, last_watered, method, tip FROM plants WHERE chat_id=$1 ORDER BY id",
            chat_id
        )

async def get_plant(plant_id):
    async with pool.acquire() as con:
        return await con.fetchrow(
            "SELECT id, name, emoji, interval_days, last_watered, method, tip, chat_id FROM plants WHERE id=$1",
            plant_id
        )

async def add_plant(chat_id, name, emoji, interval_days, method, tip):
    async with pool.acquire() as con:
        return await con.fetchval(
            "INSERT INTO plants(chat_id, name, emoji, interval_days, method, tip) VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
            chat_id, name, emoji, interval_days, method, tip
        )

async def delete_plant(plant_id):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM plants WHERE id=$1", plant_id)
        await con.execute("DELETE FROM history WHERE plant_id=$1", plant_id)

async def update_plant(plant_id, name, emoji, interval_days, method, tip):
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE plants SET name=$1, emoji=$2, interval_days=$3, method=$4, tip=$5 WHERE id=$6",
            name, emoji, interval_days, method, tip, plant_id
        )

async def water_plant(plant_id, username):
    today = date.today().isoformat()
    async with pool.acquire() as con:
        await con.execute("UPDATE plants SET last_watered=$1 WHERE id=$2", today, plant_id)
        row = await con.fetchrow("SELECT chat_id FROM plants WHERE id=$1", plant_id)
        await con.execute(
            "INSERT INTO history(plant_id, chat_id, watered_by, watered_at) VALUES($1,$2,$3,$4)",
            plant_id, row['chat_id'], username, today
        )

async def get_history(chat_id, limit=20):
    async with pool.acquire() as con:
        return await con.fetch(
            """SELECT p.emoji, p.name, h.watered_by, h.watered_at
               FROM history h JOIN plants p ON h.plant_id=p.id
               WHERE h.chat_id=$1 ORDER BY h.watered_at DESC, h.id DESC LIMIT $2""",
            chat_id, limit
        )

async def get_stats(chat_id):
    async with pool.acquire() as con:
        return await con.fetch(
            """SELECT p.emoji, p.name, COUNT(h.id) as cnt
               FROM plants p LEFT JOIN history h ON p.id=h.plant_id
               WHERE p.chat_id=$1 GROUP BY p.id, p.emoji, p.name ORDER BY cnt DESC""",
            chat_id
        )

async def get_all_chats():
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT chat_id FROM chats")
        return [r['chat_id'] for r in rows]

# ─── Helpers ──────────────────────────────────────────────────────
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн",
                "июл", "авг", "сен", "окт", "ноя", "дек"]

def fmt_day(d: date) -> str:
    # "%-d %b" — расширение glibc, падает на Windows и musl; собираем вручную
    return f"{d.day} {MONTHS_SHORT[d.month - 1]}"

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

def back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ В меню", callback_data="action:menu")
    return kb.as_markup()

def plants_water_kb(plants):
    kb = InlineKeyboardBuilder()
    for p in plants:
        days = days_until_next(p['last_watered'], p['interval_days'])
        se = status_emoji(days)
        if can_water(days):
            kb.button(text=f"{se} {p['emoji']} {p['name']}", callback_data=f"water:{p['id']}")
        else:
            kb.button(text=f"{se} {p['emoji']} {p['name']} — {format_status(days)}", callback_data=f"skip:{p['id']}")
    kb.button(text="◀️ Назад", callback_data="action:menu")
    kb.adjust(1)
    return kb.as_markup()

def plants_delete_kb(plants):
    kb = InlineKeyboardBuilder()
    for p in plants:
        kb.button(text=f"🗑 {p['emoji']} {p['name']}", callback_data=f"confirmdelete:{p['id']}")
    kb.button(text="◀️ Назад", callback_data="action:menu")
    kb.adjust(1)
    return kb.as_markup()

def confirm_delete_kb(plant_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"dodelete:{plant_id}")
    kb.button(text="❌ Отмена", callback_data="action:menu")
    kb.adjust(2)
    return kb.as_markup()

# ─── Callback helpers ─────────────────────────────────────────────
async def ack(cb: CallbackQuery, text: str = None, alert: bool = False):
    """Снять «часики» с кнопки. Вызывать как можно раньше и никогда не падать."""
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramBadRequest as e:
        # query is too old / already answered — кнопка всё равно разблокируется
        logging.info(f"answer_callback_query: {e}")

async def safe_edit(cb: CallbackQuery, text: str, reply_markup=None):
    """Отредактировать сообщение, а если нельзя — прислать новое.

    edit_text не работает в трёх случаях, и каждый из них раньше молча
    убивал обработчик:
      - сообщение старше 48 ч: Telegram отдаёт InaccessibleMessage без edit_text
      - текст не изменился: 400 message is not modified
      - сообщение удалено пользователем
    """
    msg = cb.message
    if msg is None:
        return
    chat_id = msg.chat.id

    if isinstance(msg, InaccessibleMessage):
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)
        return

    try:
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        low = str(e).lower()
        if "message is not modified" in low:
            return
        logging.info(f"edit_text не удался ({e}), отправляю новое сообщение")
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)

# ─── Status text ──────────────────────────────────────────────────
def build_status_text(plants, title="🌿 *Мои растения*"):
    if not plants:
        return "Растений пока нет. Нажми ➕ Добавить"
    lines = [title, ""]
    for p in plants:
        days = days_until_next(p['last_watered'], p['interval_days'])
        se = status_emoji(days)
        st = format_status(days)
        last_str = fmt_day(date.fromisoformat(p['last_watered'])) if p['last_watered'] else "—"
        lines.append(f"{se} {p['emoji']} *{p['name']}*")
        lines.append(f"   _{st}_ · последний: {last_str}")
    return "\n".join(lines)

# ─── Add state ────────────────────────────────────────────────────
add_state = {}  # (chat_id, user_id) -> step data

# ─── Handlers ─────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await register_chat(msg.chat.id)
    key = (msg.chat.id, msg.from_user.id)
    add_state.pop(key, None)
    plants = await get_plants(msg.chat.id)
    text = build_status_text(plants, "🌿 *Привет! Я трекер полива*\n\nВот ваши растения:")
    await msg.answer(text, parse_mode="Markdown", reply_markup=main_kb())

@dp.message(Command("menu"))
async def cmd_menu(msg: types.Message):
    await register_chat(msg.chat.id)
    key = (msg.chat.id, msg.from_user.id)
    add_state.pop(key, None)
    plants = await get_plants(msg.chat.id)
    text = build_status_text(plants)
    await msg.answer(text, parse_mode="Markdown", reply_markup=main_kb())

@dp.callback_query(F.data == "action:menu")
async def cb_menu(cb: CallbackQuery):
    await ack(cb)
    plants = await get_plants(cb.message.chat.id)
    await safe_edit(cb, build_status_text(plants), main_kb())

@dp.callback_query(F.data == "action:status")
async def cb_status(cb: CallbackQuery):
    await ack(cb)
    plants = await get_plants(cb.message.chat.id)
    await safe_edit(cb, build_status_text(plants), main_kb())

@dp.callback_query(F.data == "action:water")
async def cb_water_list(cb: CallbackQuery):
    plants = await get_plants(cb.message.chat.id)
    if not plants:
        await ack(cb, "Нет растений!", alert=True)
        return
    await ack(cb)
    await safe_edit(cb, "💧 *Выбери что полить:*", plants_water_kb(plants))

@dp.callback_query(F.data.startswith("water:"))
async def cb_do_water(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = await get_plant(plant_id)
    if not plant:
        await ack(cb, "Не найдено", alert=True)
        return
    days = days_until_next(plant['last_watered'], plant['interval_days'])
    if not can_water(days):
        await ack(cb, f"Рано! {format_status(days)}", alert=True)
        return
    username = cb.from_user.full_name or cb.from_user.username or "кто-то"
    await water_plant(plant_id, username)
    await ack(cb, f"✅ {plant['emoji']} {plant['name']} полит!")
    plants = await get_plants(cb.message.chat.id)
    await safe_edit(cb, build_status_text(plants), main_kb())
    next_date = fmt_day(date.today() + timedelta(days=plant['interval_days']))
    await bot.send_message(
        cb.message.chat.id,
        f"💧 *{username}* полил {plant['emoji']} *{plant['name']}*\nСледующий полив: {next_date}",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = await get_plant(plant_id)
    if not plant:
        await ack(cb, "Не найдено", alert=True)
        return
    days = days_until_next(plant['last_watered'], plant['interval_days'])
    await ack(cb, f"Рано! {format_status(days)}", alert=True)

@dp.callback_query(F.data == "action:history")
async def cb_history(cb: CallbackQuery):
    await ack(cb)
    history = await get_history(cb.message.chat.id)
    if not history:
        text = "📜 История поливов пуста"
    else:
        lines = ["📜 *Последние поливы:*", ""]
        for r in history:
            d = fmt_day(date.fromisoformat(r['watered_at']))
            lines.append(f"{r['emoji']} {r['name']} — {d} ({r['watered_by']})")
        text = "\n".join(lines)
    await safe_edit(cb, text, back_kb())

@dp.callback_query(F.data == "action:stats")
async def cb_stats(cb: CallbackQuery):
    await ack(cb)
    stats = await get_stats(cb.message.chat.id)
    if not stats:
        text = "📊 Статистика пуста"
    else:
        lines = ["📊 *Статистика поливов:*", ""]
        for r in stats:
            lines.append(f"{r['emoji']} {r['name']} — {r['cnt']} раз")
        text = "\n".join(lines)
    await safe_edit(cb, text, back_kb())

@dp.callback_query(F.data == "action:delete")
async def cb_delete_list(cb: CallbackQuery):
    plants = await get_plants(cb.message.chat.id)
    if not plants:
        await ack(cb, "Нет растений!", alert=True)
        return
    await ack(cb)
    await safe_edit(cb, "🗑 *Выбери растение для удаления:*", plants_delete_kb(plants))

@dp.callback_query(F.data.startswith("confirmdelete:"))
async def cb_confirm_delete(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = await get_plant(plant_id)
    if not plant:
        await ack(cb, "Не найдено", alert=True)
        return
    await ack(cb)
    await safe_edit(
        cb,
        f"Удалить {plant['emoji']} *{plant['name']}*?\nВся история тоже удалится.",
        confirm_delete_kb(plant_id)
    )

@dp.callback_query(F.data.startswith("dodelete:"))
async def cb_do_delete(cb: CallbackQuery):
    plant_id = int(cb.data.split(":")[1])
    plant = await get_plant(plant_id)
    name = plant['name'] if plant else "растение"
    emoji = plant['emoji'] if plant else "🌱"
    await delete_plant(plant_id)
    await ack(cb, f"🗑 {emoji} {name} удалён")
    plants = await get_plants(cb.message.chat.id)
    await safe_edit(cb, build_status_text(plants), main_kb())

# ─── Add plant flow ───────────────────────────────────────────────
@dp.callback_query(F.data == "action:add")
async def cb_add_start(cb: CallbackQuery):
    await ack(cb)
    key = (cb.message.chat.id, cb.from_user.id)
    add_state[key] = {"step": "name", "chat_id": cb.message.chat.id}
    await safe_edit(cb, "➕ *Добавляем растение*\n\nШаг 1/4: Напиши *название* растения")

@dp.message(F.text)
async def handle_text(msg: types.Message):
    key = (msg.chat.id, msg.from_user.id)
    state = add_state.get(key)
    if not state:
        return
    chat_id = state["chat_id"]
    step = state["step"]

    if step == "name":
        state["name"] = msg.text.strip()
        state["step"] = "emoji"
        await msg.answer("Шаг 2/4: Отправь *эмодзи* (например 🌴 🌵 🌿 🌾 🌺 🪴)", parse_mode="Markdown")
    elif step == "emoji":
        state["emoji"] = msg.text.strip()[:2] or "🌱"
        state["step"] = "interval"
        await msg.answer("Шаг 3/4: Каждые сколько *дней* поливать?", parse_mode="Markdown")
    elif step == "interval":
        try:
            days = int(msg.text.strip())
            if days < 1 or days > 365: raise ValueError
        except ValueError:
            await msg.answer("Введи число от 1 до 365")
            return
        state["interval"] = days
        state["step"] = "method"
        await msg.answer("Шаг 4/4: *Способ полива* (или `-` пропустить)", parse_mode="Markdown")
    elif step == "method":
        state["method"] = "" if msg.text.strip() == "-" else msg.text.strip()
        state["step"] = "tip"
        await msg.answer("*Заметка/совет* (или `-` пропустить)", parse_mode="Markdown")
    elif step == "tip":
        tip = "" if msg.text.strip() == "-" else msg.text.strip()
        await add_plant(chat_id, state["name"], state["emoji"], state["interval"], state["method"], tip)
        del add_state[key]
        plants = await get_plants(chat_id)
        await msg.answer(
            f"✅ {state['emoji']} *{state['name']}* добавлен!\n\n{build_status_text(plants)}",
            parse_mode="Markdown", reply_markup=main_kb()
        )

# ─── Global error handler ─────────────────────────────────────────
@dp.errors()
async def on_error(event: ErrorEvent):
    """Ни одно исключение не должно оставлять кнопку в «часиках» молча."""
    logging.exception(f"Ошибка при обработке апдейта: {event.exception}")
    cb = event.update.callback_query
    if cb is not None:
        try:
            await cb.answer("⚠️ Ошибка, попробуй ещё раз или /menu", show_alert=True)
        except Exception:
            pass
    return True

# ─── Daily reminder ───────────────────────────────────────────────
async def daily_check():
    try:
        chats = await get_all_chats()
    except Exception as e:
        logging.exception(f"daily_check: не смог прочитать чаты: {e}")
        return
    for chat_id in chats:
        try:
            plants = await get_plants(chat_id)
        except Exception as e:
            logging.exception(f"daily_check: не смог прочитать растения {chat_id}: {e}")
            continue
        due = []
        for p in plants:
            days = days_until_next(p['last_watered'], p['interval_days'])
            if days is None or days <= 0:
                due.append(f"{p['emoji']} {p['name']}")
        if due:
            try:
                await bot.send_message(
                    chat_id,
                    "🔔 *Пора поливать!*\n\n" + "\n".join(due),
                    parse_mode="Markdown", reply_markup=main_kb()
                )
            except Exception as e:
                logging.warning(f"Can't send to {chat_id}: {e}")

# ─── Main ─────────────────────────────────────────────────────────
async def main():
    await init_db()
    logging.info("БД готова")

    # если когда-то был выставлен webhook, getUpdates не получит ни одного апдейта
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    logging.info(f"Запускаю polling для @{me.username} (id={me.id})")

    scheduler.add_job(daily_check, "cron", hour=9, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
