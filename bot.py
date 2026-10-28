import asyncio
import html
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8756747119:AAH4RWn0oVCluPBT2gA8-r6DzYJOHm3gMJI"
SUPER_ADMIN_ID = 8829145552  # Ваш постоянный цифровой Telegram ID
ADMIN_CMD = "manage_panel"  # Команда открытия CRM-панели (без слеша)
DB_PATH = "crm_database.db"

# ================= ЛОГИРОВАНИЕ =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot_activity.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("CRM_Bot")

# ================= СЕРВИСЫ И ЭТАПЫ =================
RKO_SERVICES = {
    "rko_alfa": "Альфа-Банк — 4 000 ₽",
    "rko_ozon": "Озон Банк — 3 000 ₽",
    "rko_sber": "Сбер — 3 000 ₽",
    "rko_vtb": "ВТБ — 1 000 ₽",
    "rko_tbank": "Т-Банк — 3 000 ₽",
}

REGBIZ_SERVICES = {
    "reg_alfa": "Альфа-Банк (ИП + счёт) — 4 000 ₽"
}

STAGES = [
    "Подал заявку",
    "Договорился о встрече",
    "Забрал карту",
    "Ждет активацию ИП",
    "ИП активировано",
    "Активировал карту",
    "Выполнил ЦД",
    "Получили выплату"
]

# ================= БАЗА ДАННЫХ =================
@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        yield db

async def init_db():
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                pd_accepted INTEGER DEFAULT 0,
                client_status TEXT DEFAULT 'Новый',
                visits_count INTEGER DEFAULT 0,
                is_approved INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                form_type TEXT DEFAULT 'РКО',
                fio TEXT DEFAULT 'Не указано',
                inn TEXT DEFAULT 'Не указано',
                tg_username TEXT DEFAULT 'Не указано',
                current_bank TEXT DEFAULT 'Не указано',
                stage TEXT DEFAULT 'Подал заявку',
                done_banks TEXT DEFAULT 'Не указано',
                reg_date TEXT DEFAULT 'Не указано',
                submission_time TEXT DEFAULT 'Не указано',
                comment TEXT DEFAULT 'Нет',
                is_completed INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER,
                added_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tutorials (
                type TEXT PRIMARY KEY,
                text TEXT,
                photo_id TEXT
            )
        """)

        # Миграции
        try:
            await db.execute("ALTER TABLE profiles ADD COLUMN submission_time TEXT DEFAULT 'Не указано';")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0;")
        except Exception:
            pass

        # АВТООЧИСТКА: удаляем старые пустые анкеты-призраки
        await db.execute("DELETE FROM profiles WHERE is_completed = 0 OR fio = 'Не указано';")

        await db.execute("""
            INSERT OR IGNORE INTO admins (user_id, added_by, added_at)
            VALUES (?, 0, ?)
        """, (SUPER_ADMIN_ID, datetime.now().strftime("%d.%m.%Y %H:%M")))

        await db.execute("""
            INSERT OR IGNORE INTO tutorials (type, text, photo_id)
            VALUES 
            ('РКО', 'Инструкция по РКО пока не заполнена администратором.', NULL),
            ('РЕГБИЗ', 'Инструкция по РЕГБИЗ пока не заполнена администратором.', NULL)
        """)
        await db.commit()
    logger.info("База данных успешно инициализирована и очищена от пустых записей.")

async def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    async with get_db() as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as cur:
            return (await cur.fetchone()) is not None

async def is_user_blocked(user_id: int) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return bool(row[0]) if row else False

async def get_all_admins() -> List[int]:
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM admins") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]

# ================= FSM СОСТОЯНИЯ =================
class ClientSurvey(StatesGroup):
    waiting_fio = State()
    waiting_inn = State()
    waiting_current_bank = State()
    waiting_done_banks = State()
    waiting_reg_date = State()
    waiting_comment = State()

class AdminEdit(StatesGroup):
    target_user_id = State()
    field_to_edit = State()
    waiting_new_value = State()

class AdminTutorialEdit(StatesGroup):
    tutorial_type = State()
    waiting_content = State()

class AdminManageState(StatesGroup):
    waiting_new_admin_id = State()

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
user_cart = {}

async def notify_admins(text: str, reply_markup=None):
    admins = await get_all_admins()
    for aid in admins:
        try:
            await bot.send_message(aid, text, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {aid}: {e}")

# ================= ПРОВЕРКА БЛОКИРОВКИ =================
@dp.message.outer_middleware
async def check_user_blocked_middleware(handler, event, data):
    user = getattr(event, "from_user", None)
    if user:
        if await is_user_blocked(user.id):
            if not await is_admin(user.id):
                if isinstance(event, types.Message):
                    await event.answer("⛔️ <b>Ваш аккаунт заблокирован.</b> Доступ к боту закрыт.")
                elif isinstance(event, types.CallbackQuery):
                    await event.answer("⛔️ Ваш аккаунт заблокирован.", show_alert=True)
                return
    return await handler(event, data)

# ================= СТАРТ =================
@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    raw_uname = message.from_user.username
    username = f"@{raw_uname}" if raw_uname else "Без username"
    full_name = html.escape(message.from_user.full_name)
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    async with get_db() as db:
        async with db.execute("SELECT visits_count FROM users WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            if row is None:
                is_first = True
                await db.execute(
                    "INSERT INTO users (user_id, username, full_name, visits_count, created_at) VALUES (?, ?, ?, 1, ?)",
                    (uid, username, full_name, now_str)
                )
            else:
                is_first = False
                await db.execute(
                    "UPDATE users SET visits_count = visits_count + 1, username = ?, full_name = ? WHERE user_id = ?",
                    (username, full_name, uid)
                )
        await db.commit()

    tag = "🟢 [НОВЫЙ]" if is_first else "🟡 [СТАРЫЙ]"
    logger.info(f"Вход в бота: {tag} ID={uid}, Name={full_name}, Username={username}")

    admin_alert = (
        f"{tag} вход в бота!\n"
        f"👤 Клиент: {full_name}\n"
        f"🔗 Telegram: {username}\n"
        f"🆔 ID: <code>{uid}</code>"
    )
    await notify_admins(admin_alert)

    async with get_db() as db:
        async with db.execute("SELECT pd_accepted FROM users WHERE user_id = ?", (uid,)) as cur:
            accepted = (await cur.fetchone())[0]

    if not accepted:
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтверждаю обработку ПД", callback_data="accept_pd")
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "Для продолжения работы необходимо дать согласие на обработку персональных данных.",
            reply_markup=kb.as_markup()
        )
    else:
        await show_client_main_menu(uid, message.answer)

@dp.callback_query(F.data == "accept_pd")
async def accept_pd_handler(callback: types.CallbackQuery):
    uid = callback.from_user.id
    async with get_db() as db:
        await db.execute("UPDATE users SET pd_accepted = 1 WHERE user_id = ?", (uid,))
        await db.commit()
    await callback.message.edit_text("✅ Обработка персональных данных подтверждена.")
    await show_status_choice(callback.message.answer)
    await callback.answer()

async def show_status_choice(send_func):
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Я новый клиент", callback_data="status_new")
    kb.button(text="🔄 Я уже в работе / откручиваюсь", callback_data="status_old")
    kb.adjust(1)
    await send_func("Выберите ваш текущий статус работы:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.in_(["status_new", "status_old"]))
async def status_choice_handler(callback: types.CallbackQuery):
    status_text = "Новый" if callback.data == "status_new" else "В работе"
    uid = callback.from_user.id
    async with get_db() as db:
        await db.execute("UPDATE users SET client_status = ? WHERE user_id = ?", (status_text, uid))
        await db.commit()
    await callback.message.edit_text(f"Статус зафиксирован: <b>{status_text}</b>")
    await show_client_main_menu(uid, callback.message.answer)
    await callback.answer()

# ================= ГЛАВНОЕ МЕНЮ =================
async def show_client_main_menu(user_id: int, send_func):
    async with get_db() as db:
        async with db.execute("SELECT is_approved FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            is_approved = row[0] if row else 0

    user_is_admin = await is_admin(user_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Выбрать РКО", callback_data="nav_rko")
    kb.button(text="🏢 Выбрать РЕГБИЗ", callback_data="nav_regbiz")
    
    if is_approved:
        kb.button(text="📖 Обучающие материалы (Туторы)", callback_data="nav_tutorial")
    else:
        kb.button(text="🔒 Туторы (Ожидает одобрения анкеты)", callback_data="tutorial_locked")
        
    kb.button(text="📋 Заполнить мою анкету", callback_data="start_survey")

    # Для администраторов кнопка админки прямо на главном экране
    if user_is_admin:
        kb.button(text="⚙️ CRM-Панель Администратора", callback_data="adm_hub")
        kb.adjust(2, 1, 1, 1)
    else:
        kb.adjust(2, 1, 1)

    await send_func(
        "<b>Главное меню</b>\n\n"
        "Выберите направление работы или заполните анкету для проверки:",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "tutorial_locked")
async def tutorial_locked_handler(callback: types.CallbackQuery):
    await callback.answer(
        "⚠️ Туториалы станут доступны после заполнения и одобрения вашей анкеты администратором!", 
        show_alert=True
    )

# ================= ВЫБОР УСЛУГ =================
@dp.callback_query(F.data == "nav_regbiz")
async def regbiz_list(callback: types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    for k, v in REGBIZ_SERVICES.items():
        kb.button(text=v, callback_data=f"sel_reg_{k}")
    kb.button(text="⬅️ В главное меню", callback_data="nav_home")
    kb.adjust(1)

    await callback.message.edit_text(
        "🏢 <b>РЕГБИЗ (Регистрация бизнеса)</b>:\n\n"
        "Можно выбрать только <b>1 банк</b>. Нажмите на вариант ниже:",
        reply_markup=kb.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("sel_reg_"))
async def regbiz_picked(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.replace("sel_reg_", "")
    bank_title = REGBIZ_SERVICES.get(code, "Банк")
    
    await state.update_data(chosen_type="РЕГБИЗ", chosen_bank=bank_title)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заполнить анкету", callback_data="start_survey")
    kb.button(text="⬅️ В главное меню", callback_data="nav_home")
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Вы выбрали РЕГБИЗ:</b>\n{bank_title}\n\n"
        "Теперь перейдите к заполнению анкеты для верификации заявки.",
        reply_markup=kb.as_markup()
    )
    await callback.answer()

def get_rko_keyboard(user_id: int):
    selected = user_cart.get(user_id, set())
    kb = InlineKeyboardBuilder()
    for k, v in RKO_SERVICES.items():
        icon = "✅" if k in selected else "▫️"
        kb.button(text=f"{icon} {v}", callback_data=f"tgl_rko_{k}")
    kb.adjust(1)
    if selected:
        kb.button(text=f"🚀 Подтвердить выбор ({len(selected)})", callback_data="confirm_rko_selection")
    kb.button(text="⬅️ В меню", callback_data="nav_home")
    kb.adjust(1)
    return kb.as_markup()

@dp.callback_query(F.data == "nav_rko")
async def rko_list(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_cart:
        user_cart[uid] = set()
    await callback.message.edit_text(
        "📂 <b>РКО (Бизнес-счета)</b>:\n\n"
        "Отметьте нужное количество банков и подтвердите выбор:",
        reply_markup=get_rko_keyboard(uid)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("tgl_rko_"))
async def rko_toggle(callback: types.CallbackQuery):
    uid = callback.from_user.id
    k = callback.data.replace("tgl_rko_", "")
    if uid not in user_cart:
        user_cart[uid] = set()

    if k in user_cart[uid]:
        user_cart[uid].remove(k)
    else:
        user_cart[uid].add(k)

    await callback.message.edit_reply_markup(reply_markup=get_rko_keyboard(uid))
    await callback.answer()

@dp.callback_query(F.data == "confirm_rko_selection")
async def rko_confirmed(callback: types.CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    selected = user_cart.get(uid, set())
    chosen_names = [RKO_SERVICES[x] for x in selected if x in RKO_SERVICES]
    chosen_str = ", ".join(chosen_names)

    await state.update_data(chosen_type="РКО", chosen_bank=chosen_str)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заполнить анкету", callback_data="start_survey")
    kb.button(text="⬅️ В главное меню", callback_data="nav_home")
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Выбранные банки по РКО:</b>\n" + "\n".join([f"• {x}" for x in chosen_names]) + "\n\n"
        "Заполните анкету для отправки кураторам на подтверждение.",
        reply_markup=kb.as_markup()
    )
    await callback.answer()

# ================= АНКЕТИРОВАНИЕ КЛИЕНТА =================
def skip_kb():
    b = ReplyKeyboardBuilder()
    b.button(text="Пропустить")
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)

@dp.callback_query(F.data == "start_survey")
async def start_survey_flow(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ClientSurvey.waiting_fio)
    await callback.message.answer(
        "Шаг 1 из 6.\nВведите ваше <b>ФИО полностью</b> (минимум 2 слова, только буквы):"
    )
    await callback.answer()

@dp.message(ClientSurvey.waiting_fio)
async def survey_fio(message: types.Message, state: FSMContext):
    fio = message.text.strip()
    words = fio.split()
    if len(words) < 2 or not all(re.match(r"^[A-Za-zА-Яа-яЁё\-]+$", w) for w in words):
        await message.answer("❌ <b>Некорректный формат!</b>\nНапишите Фамилию и Имя буквами через пробел:")
        return

    await state.update_data(fio=html.escape(fio))
    await state.set_state(ClientSurvey.waiting_inn)
    await message.answer("Шаг 2 из 6.\nВведите ваш <b>ИНН</b> (ровно 10 цифр для ООО или 12 цифр для ИП/физлиц):")

@dp.message(ClientSurvey.waiting_inn)
async def survey_inn(message: types.Message, state: FSMContext):
    inn = message.text.strip()
    if not (inn.isdigit() and len(inn) in (10, 12)):
        await message.answer("❌ <b>Некорректный ИНН!</b>\nДопускаются только цифры (строго 10 или 12 знаков):")
        return

    await state.update_data(inn=inn)
    await state.set_state(ClientSurvey.waiting_current_bank)
    await message.answer(
        "Шаг 3 из 6.\nУкажите <b>текущий банк в работе</b> (или нажмите кнопку «Пропустить»):",
        reply_markup=skip_kb()
    )

@dp.message(ClientSurvey.waiting_current_bank)
async def survey_current_bank(message: types.Message, state: FSMContext):
    txt = "Не указано" if message.text == "Пропустить" else html.escape(message.text.strip())
    await state.update_data(current_bank=txt)
    await state.set_state(ClientSurvey.waiting_done_banks)
    await message.answer(
        "Шаг 4 из 6.\n<b>Какие банки уже делали ранее?</b> (или нажмите «Пропустить»):",
        reply_markup=skip_kb()
    )

@dp.message(ClientSurvey.waiting_done_banks)
async def survey_done_banks(message: types.Message, state: FSMContext):
    txt = "Не указано" if message.text == "Пропустить" else html.escape(message.text.strip())
    await state.update_data(done_banks=txt)
    await state.set_state(ClientSurvey.waiting_reg_date)
    await message.answer(
        "Шаг 5 из 6.\nВведите <b>желаемую дату оформления</b> строго в формате <code>ДД.ММ.ГГГГ</code> (например, 27.10.2026):",
        reply_markup=skip_kb()
    )

@dp.message(ClientSurvey.waiting_reg_date)
async def survey_date(message: types.Message, state: FSMContext):
    raw_date = message.text.strip()
    if raw_date != "Пропустить":
        try:
            valid_date = datetime.strptime(raw_date, "%d.%m.%Y")
            formatted_date = valid_date.strftime("%d.%m.%Y")
        except ValueError:
            await message.answer(
                "❌ <b>Неверная дата!</b>\nВведите дату строго через точки: <code>ДД.ММ.ГГГГ</code> (например: <code>15.05.2026</code>):",
                reply_markup=skip_kb()
            )
            return
    else:
        formatted_date = "Не указано"

    await state.update_data(reg_date=formatted_date)
    await state.set_state(ClientSurvey.waiting_comment)
    await message.answer(
        "Шаг 6 из 6.\n<b>Дополнительный комментарий / пожелания</b> (или «Пропустить»):",
        reply_markup=skip_kb()
    )

@dp.message(ClientSurvey.waiting_comment)
async def survey_finish(message: types.Message, state: FSMContext):
    txt = "Нет" if message.text == "Пропустить" else html.escape(message.text.strip())
    data = await state.get_data()
    uid = message.from_user.id
    uname = f"@{message.from_user.username}" if message.from_user.username else "Без username"
    
    current_time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    form_type = data.get("chosen_type", "РКО")
    bank_in_work = data.get("current_bank")
    if bank_in_work == "Не указано" and data.get("chosen_bank"):
        bank_in_work = data.get("chosen_bank")

    # Сохраняем анкету с признаком is_completed = 1
    async with get_db() as db:
        await db.execute("""
            INSERT INTO profiles (
                user_id, form_type, fio, inn, tg_username, 
                current_bank, stage, done_banks, reg_date, 
                submission_time, comment, is_completed
            ) VALUES (?, ?, ?, ?, ?, ?, 'Подал заявку', ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                form_type = excluded.form_type,
                fio = excluded.fio,
                inn = excluded.inn,
                tg_username = excluded.tg_username,
                current_bank = excluded.current_bank,
                stage = 'Подал заявку',
                done_banks = excluded.done_banks,
                reg_date = excluded.reg_date,
                submission_time = excluded.submission_time,
                comment = excluded.comment,
                is_completed = 1
        """, (uid, form_type, data['fio'], data['inn'], uname, bank_in_work, data['done_banks'], data['reg_date'], current_time_str, txt))
        await db.commit()

    await state.clear()
    logger.info(f"Анкета отправлена: ID={uid}, FIO={data['fio']}, Time={current_time_str}")

    # Клиенту время НЕ показываем
    await message.answer(
        "🎉 <b>Анкета успешно отправлена!</b>\n\n"
        "После проверки данных администратор одобрит вашу заявку, и вам станет доступен раздел туториалов.",
        reply_markup=types.ReplyKeyboardRemove()
    )

    # Админам время фиксации ПРИСЫЛАЕМ
    summary = (
        f"⚡️ <b>НОВАЯ ЗАЯВКА НА ПРОВЕРКУ [{form_type}]!</b>\n"
        f"🕒 <b>Время фиксации:</b> <code>{current_time_str}</code>\n\n"
        f"1. <b>ФИО:</b> {data['fio']}\n"
        f"2. <b>ИНН:</b> {data['inn']}\n"
        f"4. <b>Telegram:</b> {uname}\n"
        f"5. <b>Текущий банк:</b> {bank_in_work}\n"
        f"6. <b>Этап:</b> <u>Подал заявку</u>\n"
        f"8. <b>Сделанные банки:</b> {data['done_banks']}\n"
        f"11. <b>Дата:</b> {data['reg_date']}\n"
        f"13. <b>Комментарий:</b> {txt}\n"
        f"🆔 ID: <code>{uid}</code>"
    )
    adm_kb = InlineKeyboardBuilder()
    adm_kb.button(text="✅ Одобрить", callback_data=f"adm_approve_{uid}")
    adm_kb.button(text="❌ Отклонить", callback_data=f"adm_reject_{uid}")
    adm_kb.button(text="✏️ Открыть в CRM", callback_data=f"adm_card_{uid}")
    adm_kb.adjust(2, 1)

    await notify_admins(summary, reply_markup=adm_kb.as_markup())

# ================= ОДОБРЕНИЕ / ОТКЛОНЕНИЕ =================
@dp.callback_query(F.data.startswith("adm_approve_"))
async def approve_client(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_approve_", ""))

    async with get_db() as db:
        await db.execute("UPDATE users SET is_approved = 1 WHERE user_id = ?", (uid,))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} ОДОБРИЛ заявку {uid}")
    await callback.message.edit_text(callback.message.text + "\n\n🟢 <b>ОДОБРЕНО АДМИНИСТРАТОРОМ</b>")

    try:
        kb = InlineKeyboardBuilder()
        kb.button(text="📖 Открыть туториалы", callback_data="nav_tutorial")
        await bot.send_message(
            uid,
            "✅ <b>Ваша анкета одобрена!</b>\n"
            "Вам открыт доступ к обучающим материалам и инструкциям.",
            reply_markup=kb.as_markup()
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление пользователю {uid}: {e}")
    await callback.answer("Клиент одобрен")

@dp.callback_query(F.data.startswith("adm_reject_"))
async def reject_client(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_reject_", ""))

    async with get_db() as db:
        await db.execute("UPDATE users SET is_approved = 0 WHERE user_id = ?", (uid,))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} ОТКЛОНИЛ заявку {uid}")
    await callback.message.edit_text(callback.message.text + "\n\n🔴 <b>ОТКЛОНЕНО АДМИНИСТРАТОРОМ</b>")

    try:
        await bot.send_message(
            uid,
            "❌ <b>Ваша заявка отклонена модератором.</b>\n"
            "Свяжитесь с куратором или заполните анкету повторно с корректными данными."
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление об отказе пользователю {uid}: {e}")
    await callback.answer("Клиент отклонен")

# ================= РАЗДЕЛ ТУТОРИАЛОВ =================
@dp.callback_query(F.data == "nav_tutorial")
async def show_tutorial_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    async with get_db() as db:
        async with db.execute("SELECT is_approved FROM users WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            if not row or not row[0]:
                await callback.answer("Доступ откроется только после одобрения анкеты!", show_alert=True)
                return

    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Тутор по РКО", callback_data="tutor_show_РКО")
    kb.button(text="🏢 Тутор по РЕГБИЗ", callback_data="tutor_show_РЕГБИЗ")
    kb.button(text="⬅️ В главное меню", callback_data="nav_home")
    kb.adjust(2, 1)

    await callback.message.edit_text("Обучающие инструкции сервиса:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("tutor_show_"))
async def display_tutorial(callback: types.CallbackQuery):
    ttype = callback.data.replace("tutor_show_", "")
    async with get_db() as db:
        async with db.execute("SELECT text, photo_id FROM tutorials WHERE type = ?", (ttype,)) as cur:
            row = await cur.fetchone()

    kb = InlineKeyboardBuilder().button(text="⬅️ К туториалам", callback_data="nav_tutorial")
    text_content = row[0] if row else "Инструкция пока отсутствует."
    photo_id = row[1] if row else None

    if photo_id:
        await callback.message.delete()
        if len(text_content) <= 1000:
            await callback.message.answer_photo(
                photo=photo_id,
                caption=f"📖 <b>Инструкция {ttype}:</b>\n\n{text_content}",
                reply_markup=kb.as_markup()
            )
        else:
            await callback.message.answer_photo(
                photo=photo_id,
                caption=f"📖 <b>Инструкция {ttype} (Скриншот к материалу)</b>"
            )
            await callback.message.answer(text_content, reply_markup=kb.as_markup())
    else:
        await callback.message.edit_text(f"📖 <b>Инструкция {ttype}:</b>\n\n{text_content}", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "nav_home")
async def nav_home_handler(callback: types.CallbackQuery):
    await show_client_main_menu(callback.from_user.id, callback.message.edit_text)
    await callback.answer()

# ================= АДМИН-ПАНЕЛЬ =================
@dp.message(Command(ADMIN_CMD))
async def admin_panel_entry(message: types.Message, state: FSMContext):
    await state.clear()
    if not await is_admin(message.from_user.id):
        return
    logger.info(f"Администратор {message.from_user.id} зашёл в админку.")
    await show_admin_hub(message.answer)

async def show_admin_hub(send_func):
    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Анкеты: РКО", callback_data="adm_filter_РКО")
    kb.button(text="🏢 Анкеты: РЕГБИЗ", callback_data="adm_filter_РЕГБИЗ")
    kb.button(text="📋 Все анкеты", callback_data="adm_filter_ALL")
    kb.button(text="🖼 Редактировать туторы", callback_data="adm_edit_tutorials_menu")
    kb.button(text="👥 Управление админами", callback_data="adm_manage_admins")
    kb.button(text="⬅️ В главное меню", callback_data="nav_home")
    kb.adjust(2, 1, 1, 1, 1)

    await send_func(
        "⚙️ <b>Панель управления CRM</b>\nВыберите интересующий раздел:",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "adm_hub")
async def adm_hub_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_admin(callback.from_user.id):
        return
    await show_admin_hub(callback.message.edit_text)
    await callback.answer()

# --- Туториалы со скриншотами ---
@dp.callback_query(F.data == "adm_edit_tutorials_menu")
async def adm_tutor_menu(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить тутор РКО", callback_data="set_tutor_РКО")
    kb.button(text="✏️ Изменить тутор РЕГБИЗ", callback_data="set_tutor_РЕГБИЗ")
    kb.button(text="⬅️ Назад", callback_data="adm_hub")
    kb.adjust(1)

    await callback.message.edit_text("Какой туториал требуется обновить?", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("set_tutor_"))
async def ask_new_tutor_content(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    ttype = callback.data.replace("set_tutor_", "")
    await state.update_data(tutorial_type=ttype)
    await state.set_state(AdminTutorialEdit.waiting_content)

    await callback.message.edit_text(
        f"Отправьте новое содержание для <b>{ttype}</b>.\n\n"
        "📸 Вы можете отправить <b>фотографию/скриншот с текстом в подписи</b> или просто <b>текст</b>:\n"
        "<i>(Для отмены отправьте /cancel)</i>"
    )
    await callback.answer()

@dp.message(AdminTutorialEdit.waiting_content)
async def save_new_tutor_content(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Действие отменено.")
        return

    data = await state.get_data()
    ttype = data["tutorial_type"]

    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
        text = html.escape(message.caption) if message.caption else "Инструкция на прикрепленном скриншоте."
    else:
        text = html.escape(message.text)

    async with get_db() as db:
        await db.execute("""
            INSERT INTO tutorials (type, text, photo_id) VALUES (?, ?, ?)
            ON CONFLICT(type) DO UPDATE SET text=excluded.text, photo_id=excluded.photo_id
        """, (ttype, text, photo_id))
        await db.commit()

    logger.info(f"Администратор {message.from_user.id} обновил туториал для {ttype}")
    await state.clear()

    kb = InlineKeyboardBuilder().button(text="⬅️ В админ-панель", callback_data="adm_hub")
    await message.answer(f"✅ Туториал для <b>{ttype}</b> успешно сохранён!", reply_markup=kb.as_markup())

# --- Управление админами ---
@dp.callback_query(F.data == "adm_manage_admins")
async def manage_admins_menu(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить админа", callback_data="adm_add_admin")
    kb.button(text="➖ Снять админа", callback_data="adm_remove_list")
    kb.button(text="⬅️ В панель", callback_data="adm_hub")
    kb.adjust(2, 1)

    admins = await get_all_admins()
    admins_str = "\n".join([f"• <code>{aid}</code> {'(Главный)' if aid == SUPER_ADMIN_ID else ''}" for aid in admins])

    await callback.message.edit_text(
        f"👥 <b>Управление администраторами</b>\n\nТекущие админы:\n{admins_str}",
        reply_markup=kb.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_add_admin")
async def prompt_add_admin(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    await state.set_state(AdminManageState.waiting_new_admin_id)
    await callback.message.edit_text("Отправьте числовой <b>Telegram ID</b> нового администратора:")
    await callback.answer()

@dp.message(AdminManageState.waiting_new_admin_id)
async def process_add_admin(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    target_id_str = message.text.strip()
    if not target_id_str.isdigit():
        await message.answer("❌ ID должен содержать только цифры. Повторите ввод:")
        return

    new_id = int(target_id_str)
    async with get_db() as db:
        await db.execute("""
            INSERT OR IGNORE INTO admins (user_id, added_by, added_at)
            VALUES (?, ?, ?)
        """, (new_id, message.from_user.id, datetime.now().strftime("%d.%m.%Y %H:%M")))
        await db.commit()

    logger.info(f"Администратор {message.from_user.id} добавил админа {new_id}")
    await state.clear()
    kb = InlineKeyboardBuilder().button(text="👥 К списку админов", callback_data="adm_manage_admins")
    await message.answer(f"✅ Пользователь с ID <code>{new_id}</code> назначен администратором!", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "adm_remove_list")
async def prompt_remove_admin(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    admins = await get_all_admins()
    kb = InlineKeyboardBuilder()
    for aid in admins:
        if aid != SUPER_ADMIN_ID and aid != callback.from_user.id:
            kb.button(text=f"Снять {aid}", callback_data=f"del_adm_{aid}")
    kb.button(text="⬅️ Отмена", callback_data="adm_manage_admins")
    kb.adjust(1)

    await callback.message.edit_text("Выберите администратора для снятия прав:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("del_adm_"))
async def execute_remove_admin(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    target_id = int(callback.data.replace("del_adm_", ""))
    async with get_db() as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (target_id,))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} снял права с {target_id}")
    await callback.answer("Администратор удален")
    await manage_admins_menu(callback)

# ================= СПИСОК ТОЛЬКО ЗАПОЛНЕННЫХ АНКЕТ С ВРЕМЕНЕМ =================
@dp.callback_query(F.data.startswith("adm_filter_"))
async def admin_list_profiles(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    ftype = callback.data.replace("adm_filter_", "")
    kb = InlineKeyboardBuilder()

    async with get_db() as db:
        # ВЫБИРАЕМ ТОЛЬКО РЕАЛЬНО ЗАПОЛНЕННЫЕ АНКЕТЫ (is_completed = 1)
        if ftype == "ALL":
            query = """
                SELECT user_id, fio, form_type, stage, submission_time 
                FROM profiles 
                WHERE is_completed = 1 AND fio != 'Не указано'
                ORDER BY user_id DESC LIMIT 40
            """
            params = ()
        else:
            query = """
                SELECT user_id, fio, form_type, stage, submission_time 
                FROM profiles 
                WHERE form_type = ? AND is_completed = 1 AND fio != 'Не указано'
                ORDER BY user_id DESC LIMIT 40
            """
            params = (ftype,)

        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()

    if not rows:
        kb.button(text="⬅️ Назад", callback_data="adm_hub")
        await callback.message.edit_text("В этой категории пока нет заполненных анкет.", reply_markup=kb.as_markup())
        await callback.answer()
        return

    for uid, fio, t, stg, sub_time in rows:
        if sub_time and sub_time != "Не указано":
            parts = sub_time.split(" ")
            d_part = parts[0][:5] if len(parts) > 0 else ""
            t_part = parts[1][:5] if len(parts) > 1 else ""
            time_display = f"{d_part} {t_part} | "
        else:
            time_display = ""

        # Формат кнопки: [РКО] 27.10 14:35 | Иванов И. (Подал заявку)
        label = f"[{t}] {time_display}{fio} ({stg})"
        kb.button(text=label[:45], callback_data=f"adm_card_{uid}")

    kb.button(text="⬅️ Назад", callback_data="adm_hub")
    kb.adjust(1)

    await callback.message.edit_text(f"Список анкет ({ftype}):", reply_markup=kb.as_markup())
    await callback.answer()

# ================= КАРТОЧКА АНКЕТЫ =================
@dp.callback_query(F.data.startswith("adm_card_"))
async def adm_show_card(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_card_", ""))

    async with get_db() as db:
        async with db.execute("""
            SELECT form_type, fio, inn, tg_username, current_bank, stage, done_banks, reg_date, submission_time, comment 
            FROM profiles WHERE user_id = ? AND is_completed = 1
        """, (uid,)) as cur:
            p = await cur.fetchone()

        async with db.execute("SELECT is_blocked FROM users WHERE user_id = ?", (uid,)) as cur:
            u_row = await cur.fetchone()
            is_blocked = bool(u_row[0]) if u_row else False

    if not p:
        await callback.answer("Анкета не найдена или уже удалена!", show_alert=True)
        await show_admin_hub(callback.message.edit_text)
        return

    status_tag = "⛔️ ЗАБЛОКИРОВАН" if is_blocked else "🟢 АКТИВЕН"

    text = (
        f"📋 <b>АНКЕТА {p[0]}</b> (ID: <code>{uid}</code>)\n"
        f"Статус клиента: <b>{status_tag}</b>\n"
        f"🕒 <b>Время фиксации заявки:</b> <code>{p[8]}</code>\n\n"
        f"1. <b>ФИО:</b> {p[1]}\n"
        f"2. <b>ИНН:</b> {p[2]}\n"
        f"4. <b>Telegram:</b> {p[3]}\n"
        f"5. <b>Текущий банк:</b> {p[4]}\n"
        f"6. <b>Этап оформления:</b> <u>{p[5]}</u>\n"
        f"8. <b>Сделанные банки:</b> {p[6]}\n"
        f"11. <b>Желаемая дата:</b> {p[7]}\n"
        f"13. <b>Комментарий:</b> {p[9]}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Тип (РКО/РЕГБИЗ)", callback_data=f"adm_tgl_type_{uid}")
    kb.button(text="⚡️ Сменить этап", callback_data=f"adm_ch_stage_{uid}")
    kb.button(text="✏️ ФИО", callback_data=f"ed_fio_{uid}")
    kb.button(text="✏️ ИНН", callback_data=f"ed_inn_{uid}")
    kb.button(text="✏️ Банк в работе", callback_data=f"ed_current_bank_{uid}")
    kb.button(text="✏️ Сделал банки", callback_data=f"ed_done_banks_{uid}")
    kb.button(text="✏️ Дата", callback_data=f"ed_reg_date_{uid}")
    kb.button(text="✏️ Комментарий", callback_data=f"ed_comment_{uid}")

    # Блокировка
    if is_blocked:
        kb.button(text="🟢 Разблокировать пользователя", callback_data=f"adm_unblock_{uid}")
    else:
        kb.button(text="🚫 Заблокировать пользователя", callback_data=f"adm_block_{uid}")

    # Полное удаление
    kb.button(text="🗑 Удалить анкету навсегда", callback_data=f"adm_del_confirm_{uid}")

    kb.button(text="⬅️ К списку анкет", callback_data="adm_filter_ALL")
    kb.adjust(1, 1, 2, 2, 2, 1, 1, 1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()

# --- Логика блокировки и разблокировки ---
@dp.callback_query(F.data.startswith("adm_block_"))
async def adm_block_user(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_block_", ""))

    async with get_db() as db:
        await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (uid,))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} ЗАБЛОКИРОВАЛ пользователя {uid}")
    try:
        await bot.send_message(uid, "⛔️ <b>Ваш аккаунт был заблокирован администрацией.</b>")
    except Exception:
        pass

    callback.data = f"adm_card_{uid}"
    await adm_show_card(callback)

@dp.callback_query(F.data.startswith("adm_unblock_"))
async def adm_unblock_user(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_unblock_", ""))

    async with get_db() as db:
        await db.execute("UPDATE users SET is_blocked = 0 WHERE user_id = ?", (uid,))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} РАЗБЛОКИРОВАЛ пользователя {uid}")
    try:
        await bot.send_message(uid, "🟢 <b>Ваш аккаунт разблокирован!</b> Вы можете продолжить работу.")
    except Exception:
        pass

    callback.data = f"adm_card_{uid}"
    await adm_show_card(callback)

# --- НАСТОЯЩЕЕ УДАЛЕНИЕ АНКЕТЫ ---
@dp.callback_query(F.data.startswith("adm_del_confirm_"))
async def adm_confirm_delete_profile(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_del_confirm_", ""))

    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Да, удалить навсегда", callback_data=f"adm_del_execute_{uid}")
    kb.button(text="❌ Отмена", callback_data=f"adm_card_{uid}")
    kb.adjust(1)

    await callback.message.edit_text(
        f"⚠️ <b>Вы уверены, что хотите удалить анкету клиента ID <code>{uid}</code>?</b>\n\n"
        "Анкета будет полностью стёрта из базы данных и исчезнет из списка.",
        reply_markup=kb.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_del_execute_"))
async def adm_execute_delete_profile(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_del_execute_", ""))

    async with get_db() as db:
        # ФИЗИЧЕСКОЕ УДАЛЕНИЕ ЗАПИСИ
        await db.execute("DELETE FROM profiles WHERE user_id = ?", (uid,))
        # Сброс одобрения пользователя
        await db.execute("UPDATE users SET is_approved = 0 WHERE user_id = ?", (uid,))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} ФИЗИЧЕСКИ УДАЛИЛ анкету пользователя {uid}")
    await callback.answer("✅ Анкета удалена!", show_alert=True)
    
    # Возвращаем админа обратно в список всех анкет
    callback.data = "adm_filter_ALL"
    await admin_list_profiles(callback)

# --- Редактирование полей ---
@dp.callback_query(F.data.startswith("adm_tgl_type_"))
async def toggle_form_type(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_tgl_type_", ""))
    async with get_db() as db:
        async with db.execute("SELECT form_type FROM profiles WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            curr = row[0] if row else "РКО"
        nxt = "РЕГБИЗ" if curr == "РКО" else "РКО"
        await db.execute("UPDATE profiles SET form_type = ? WHERE user_id = ?", (nxt, uid))
        await db.commit()

    callback.data = f"adm_card_{uid}"
    await adm_show_card(callback)

@dp.callback_query(F.data.startswith("adm_ch_stage_"))
async def adm_choose_stage(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    uid = int(callback.data.replace("adm_ch_stage_", ""))
    kb = InlineKeyboardBuilder()
    for i, s in enumerate(STAGES):
        kb.button(text=s, callback_data=f"adm_set_stg_{uid}_{i}")
    kb.button(text="⬅️ Отмена", callback_data=f"adm_card_{uid}")
    kb.adjust(1)

    await callback.message.edit_text("Выберите новый этап сделки:", reply_markup=kb.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_set_stg_"))
async def adm_set_stage(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        return
    _, _, _, uid, idx = callback.data.split("_")
    new_stage = STAGES[int(idx)]

    async with get_db() as db:
        await db.execute("UPDATE profiles SET stage = ? WHERE user_id = ?", (new_stage, int(uid)))
        await db.commit()

    logger.info(f"Администратор {callback.from_user.id} установил этап '{new_stage}' пользователю {uid}")
    callback.data = f"adm_card_{uid}"
    await adm_show_card(callback)

@dp.callback_query(F.data.startswith("ed_"))
async def adm_edit_text_field(callback: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id):
        return
    parts = callback.data.split("_")
    field = "_".join(parts[1:-1])
    target_uid = int(parts[-1])

    await state.update_data(target_uid=target_uid, field=field)
    await state.set_state(AdminEdit.waiting_new_value)

    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:\n<i>(Для отмены отправьте /cancel)</i>")
    await callback.answer()

@dp.message(AdminEdit.waiting_new_value)
async def adm_save_field_value(message: types.Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Редактирование отменено.")
        return

    data = await state.get_data()
    target_uid = data["target_uid"]
    field = data["field"]
    raw_val = message.text.strip()

    if field == "reg_date":
        try:
            val = datetime.strptime(raw_val, "%d.%m.%Y").strftime("%d.%m.%Y")
        except ValueError:
            await message.answer("❌ Формат даты должен быть строго <code>ДД.ММ.ГГГГ</code>! Повторите ввод:")
            return
    elif field == "inn":
        if not (raw_val.isdigit() and len(raw_val) in (10, 12)):
            await message.answer("❌ ИНН должен состоять строго из 10 или 12 цифр! Повторите ввод:")
            return
    else:
        val = html.escape(raw_val)

    allowed = ["fio", "inn", "current_bank", "done_banks", "reg_date", "comment"]
    if field in allowed:
        async with get_db() as db:
            await db.execute(f"UPDATE profiles SET {field} = ? WHERE user_id = ?", (val, target_uid))
            await db.commit()

    logger.info(f"Администратор {message.from_user.id} изменил {field} для {target_uid} на '{val}'")
    await state.clear()
    kb = InlineKeyboardBuilder().button(text="📋 К анкете", callback_data=f"adm_card_{target_uid}")
    await message.answer("✅ Данные успешно обновлены!", reply_markup=kb.as_markup())

# ================= СТАРТ БОТА =================
async def main():
    await init_db()
    logger.info("Бот успешно запущен и готов к работе.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
