import asyncio
import logging
import sqlite3
from typing import Dict, List

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8933053546:AAGFswdXPZH09WVeKYVz-tBSOvLLBzqljHw"

# Данные наставников
MENTORS = {
    8829145552: {"name": "@Dnkantk", "username": "Dnkantk"},
    8435647377: {"name": "@maneymakersss", "username": "maneymakersss"},
}
# ================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ (защита от повторного прохождения) ---
DB_NAME = "survey_users.db"


def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS completed_users (
                user_id INTEGER PRIMARY KEY,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def is_user_completed(user_id: int) -> bool:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM completed_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None


def mark_user_completed(user_id: int):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO completed_users (user_id) VALUES (?)",
            (user_id,),
        )
        conn.commit()


# --- ДАННЫЕ БАНКОВ ---
BANKS_DATA: Dict[str, List[Dict[str, any]]] = {
    "debit": [
        {"id": "tbank_deb", "name": "Т-Банк дебетовая карта", "price": 300},
    ],
    "rekbiz": [
        {"id": "ozon_rek", "name": "Ozon банк ИП + бизнес-счёт", "price": 3500},
        {"id": "alfa_rek", "name": "Альфа-Банк ИП + бизнес-счёт", "price": 5000},
        {"id": "tochka_rek", "name": "Банк Точка ИП + бизнес-счёт", "price": 3000},
        {"id": "tbank_rek", "name": "Т-Банк ИП + бизнес-счёт", "price": 1000},
    ],
    "rko": [
        {"id": "ozon_rko", "name": "Ozon банк бизнес-счёт", "price": 2000},
        {"id": "agroros_rko", "name": "Агророс ВЭД бизнес-счёт", "price": 1000},
        {"id": "akbars_rko", "name": "АК Барс банк бизнес-счёт", "price": 1000},
        {"id": "alfa_rko", "name": "Альфа-Банк бизнес-счёт", "price": 3500},
        {"id": "tochka_rko", "name": "Банк Точка бизнес-счёт", "price": 3500},
        {"id": "vtb_rko", "name": "ВТБ бизнес-счёт", "price": 3600},
        {"id": "ingo_rko", "name": "Ингобанк бизнес-счёт", "price": 5000},
        {"id": "otp_rko", "name": "ОТП банк бизнес-счёт", "price": 2900},
        {"id": "sber_rko", "name": "Сбербизнес бизнес-счёт", "price": 1000},
        {"id": "psb_rko", "name": "Промсвязьбанк бизнес-счёт", "price": 3600},
        {"id": "sovcom_rko", "name": "Совкомбанк бизнес-счёт", "price": 2000},
        {"id": "tbank_rko", "name": "Т-Банк бизнес-счёт", "price": 2000},
        {"id": "uralsib_rko", "name": "Уралсиб банк бизнес-счёт", "price": 3500},
    ],
}


class Survey(StatesGroup):
    choosing_banks = State()


# Генератор списка с галочками
def get_banks_keyboard(category: str, selected_ids: set) -> InlineKeyboardMarkup:
    banks = BANKS_DATA[category]
    buttons = []

    for bank in banks:
        is_checked = bank["id"] in selected_ids
        mark = "✅" if is_checked else "⬜"
        btn_text = f"{mark} {bank['name']} — {bank['price']} ₽"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"toggle:{bank['id']}")])

    all_selected = len(selected_ids) == len(banks)
    action_text = "✖ Сбросить всё" if all_selected else "🔘 Выбрать все"
    action_cb = "select_none" if all_selected else "select_all"

    control_row = [
        InlineKeyboardButton(text=action_text, callback_data=action_cb),
        InlineKeyboardButton(
            text=f"🚀 Отправить заявку ({len(selected_ids)})",
            callback_data="submit_selection",
        ),
    ]
    buttons.append(control_row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- 1. СТАРТ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    # Проверка на повторное прохождение
    if is_user_completed(message.from_user.id):
        await message.answer(
            "⚠️ **Вы уже отправляли заявку.**\n\n"
            "Повторное прохождение опроса недоступно. Ожидайте связи с выбранным наставником в ЛС.",
            parse_mode="Markdown",
        )
        return

    text = (
        "👋 **Добро пожаловать!**\n\n"
        "Этот бот поможет вам узнать, какой доход вы потенциально можете получать, "
        "сотрудничая с нашей командой в сфере РКО (расчётно-кассового обслуживания).\n\n"
        "📊 **Как это работает?**\n"
        "Вам предстоит ответить на несколько простых вопросов. На основе ваших ответов "
        "мы сможем предварительно оценить возможный размер вознаграждения, рассказать об "
        "условиях сотрудничества и объяснить, от чего зависит итоговый заработок.\n\n"
        "💼 **Что вы узнаете в результате?**\n"
        "• 💰 Какое вознаграждение предусмотрено за выполнение условий программы.\n"
        "• 📈 От чего зависит размер вашего дохода.\n"
        "• 🤝 Какие условия необходимо выполнить для получения выплаты.\n"
        "• 📋 Какие дальнейшие шаги нужны для начала сотрудничества.\n\n"
        "🚀 **Готовы узнать больше?**\n"
        "Нажмите кнопку «Начать» и ответьте на вопросы бота. Это займёт всего несколько минут!\n\n"
        "_Обратите внимание: указанные суммы являются предварительной оценкой. "
        "Фактический доход зависит от условий партнёрской программы, выполнения требований "
        "и подтверждения целевых действий._"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚀 Начать", callback_data="start_survey")]]
    )
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


# --- 2. ВЫБОР НАСТАВНИКА ---
@dp.callback_query(F.data == "start_survey")
async def choose_mentor(callback: CallbackQuery):
    if is_user_completed(callback.from_user.id):
        await callback.answer("Вы уже отправляли заявку!", show_alert=True)
        return

    text = "👤 **Выберите вашего наставника:**"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 @Dnkantk", callback_data="mentor:8829145552")],
            [InlineKeyboardButton(text="👤 @maneymakersss", callback_data="mentor:8435647377")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# --- 3. ВОПРОС О 18 ГОДАХ ---
@dp.callback_query(F.data.startswith("mentor:"))
async def ask_18(callback: CallbackQuery, state: FSMContext):
    mentor_id = int(callback.data.split(":")[1])
    await state.update_data(mentor_id=mentor_id)

    text = "❓ **Вопрос 1:** Вам уже исполнилось 18 лет?"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, есть 18", callback_data="age_18_yes"),
                InlineKeyboardButton(text="Нет, меньше 18", callback_data="age_18_no"),
            ]
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# --- 4. ВЕТКА МЕНЬШЕ 18 ЛЕТ -> ПРОВЕРКА 14 ЛЕТ ---
@dp.callback_query(F.data == "age_18_no")
async def ask_14(callback: CallbackQuery):
    text = "❓ Вам уже исполнилось 14 лет?"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, есть 14", callback_data="age_14_yes"),
                InlineKeyboardButton(text="Нет, меньше 14", callback_data="age_14_no"),
            ]
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "age_14_no")
async def reject_under_14(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ **К сожалению, вы нам не подходите.**\n\nСотрудничество возможно только с 14 лет.",
        parse_mode="Markdown",
    )
    await callback.answer()


# --- 5. ВЕТКА 18+ -> ПРОВЕРКА ИП ---
@dp.callback_query(F.data == "age_18_yes")
async def ask_ip(callback: CallbackQuery):
    text = "❓ **Вопрос 2:** Есть ли у вас уже открытое ИП?"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, ИП есть", callback_data="ip_yes"),
                InlineKeyboardButton(text="Нет, ИП нет", callback_data="ip_no"),
            ]
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# --- 6. ПОКАЗ БАНКОВ (ГАЛОЧКИ) ---
@dp.callback_query(F.data.in_(["age_14_yes", "ip_no", "ip_yes"]))
async def show_banks(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mentor_id = data.get("mentor_id", 8829145552)
    mentor_tag = MENTORS.get(mentor_id, {}).get("name", "@Dnkantk")

    if callback.data == "age_14_yes":
        category = "debit"
        age_str = "14–17 лет"
        ip_str = "Нет"
        header = (
            "📄 **Дебетовые карты**\n\n"
            "Выберите банки, которые готовы оформить (нажимайте на нужные пункты):"
        )
    elif callback.data == "ip_no":
        category = "rekbiz"
        age_str = "18+"
        ip_str = "Нет (требуется открытие)"
        header = (
            "📄 **Направление «Рекбиз» (Регистрация ИП + Счёт)**\n\n"
            "💡 _ИП достаточно открыть всего лишь 1 раз, а бизнес-счетов можно открыть по всем банкам._\n"
            f"Цены часто меняются, по уточнению писать {mentor_tag}\n\n"
            "Отметьте банки, которые хотите открыть:"
        )
    else:  # ip_yes
        category = "rko"
        age_str = "18+"
        ip_str = "Да"
        header = (
            "📄 **Направление «РКО» (Открытие счетов на действующее ИП)**\n\n"
            "💡 _Цены такие, потому что нужно открыть только бизнес-счёт при уже имеющемся ИП._\n"
            f"Цены часто меняются, по уточнению писать {mentor_tag}\n\n"
            "Отметьте банки, которые хотите открыть:"
        )

    await state.set_state(Survey.choosing_banks)
    await state.update_data(
        category=category,
        age_info=age_str,
        ip_info=ip_str,
        header_text=header,
        selected_ids=set(),
    )

    kb = get_banks_keyboard(category, set())
    await callback.message.edit_text(header, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# --- 7. ПЕРЕКЛЮЧЕНИЕ ГАЛОЧЕК ---
@dp.callback_query(Survey.choosing_banks, F.data.startswith("toggle:"))
async def toggle_bank(callback: CallbackQuery, state: FSMContext):
    bank_id = callback.data.split(":")[1]
    data = await state.get_data()
    selected_ids = set(data.get("selected_ids", set()))

    if bank_id in selected_ids:
        selected_ids.remove(bank_id)
    else:
        selected_ids.add(bank_id)

    await state.update_data(selected_ids=selected_ids)
    kb = get_banks_keyboard(data["category"], selected_ids)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@dp.callback_query(Survey.choosing_banks, F.data.in_(["select_all", "select_none"]))
async def toggle_all(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    category = data["category"]

    if callback.data == "select_all":
        selected_ids = {b["id"] for b in BANKS_DATA[category]}
    else:
        selected_ids = set()

    await state.update_data(selected_ids=selected_ids)
    kb = get_banks_keyboard(category, selected_ids)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


# --- 8. ОТПРАВКА ЗАЯВКИ ТОЛЬКО ВЫБРАННОМУ НАСТАВНИКУ ---
@dp.callback_query(Survey.choosing_banks, F.data == "submit_selection")
async def submit_order(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    if is_user_completed(user_id):
        await callback.answer("Вы уже отправили заявку!", show_alert=True)
        return

    data = await state.get_data()
    selected_ids = set(data.get("selected_ids", set()))

    if not selected_ids:
        await callback.answer("Выберите хотя бы один банк из списка!", show_alert=True)
        return

    category = data["category"]
    mentor_id = data.get("mentor_id", 8829145552)
    selected_banks = [b for b in BANKS_DATA[category] if b["id"] in selected_ids]
    total_payout = sum(b["price"] for b in selected_banks)

    # Навсегда фиксируем завершение опроса в БД
    mark_user_completed(user_id)

    # 1. Сообщение кандидату
    mentor_info = MENTORS.get(mentor_id, {"name": "наставник"})
    await callback.message.edit_text(
        "✅ **Ваша заявка успешно принята!**\n\n"
        f"Ожидайте, ваш наставник ({mentor_info['name']}) свяжется с вами в личных сообщениях "
        "для отправки всех инструкций и реферальных ссылок.",
        parse_mode="Markdown",
    )
    await callback.answer()

    # 2. Кнопка перехода в ЛС кандидата
    user = callback.from_user
    username_str = f"@{user.username}" if user.username else "Отсутствует"

    if user.username:
        account_url = f"https://t.me/{user.username}"
    else:
        account_url = f"tg://user?id={user.id}"

    btn_name = user.full_name.strip() if user.full_name else "Кандидат"
    button_text = f"💬 Написать: {btn_name}"

    banks_list_str = "\n".join([f"• {b['name']} — {b['price']} ₽" for b in selected_banks])

    mentor_report = (
        "🔔 **Новая заявка на сотрудничество!**\n\n"
        f"👤 **Кандидат:** {user.full_name} ({username_str})\n"
        f"🆔 **ID:** `{user.id}`\n\n"
        f"📌 **Возраст:** {data['age_info']}\n"
        f"📌 **Наличие ИП:** {data['ip_info']}\n"
        f"📌 **Категория:** {category.upper()}\n\n"
        f"🏦 **Выбранные банки ({len(selected_banks)} шт.):**\n"
        f"{banks_list_str}\n\n"
        f"💰 **Общий потенциальный доход:** {total_payout:,} ₽"
    )

    mentor_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=button_text, url=account_url)]]
    )

    # 3. Отправка ЛОГОВ ТОЛЬКО ВЫБРАННОМУ НАСТАВНИКУ
    try:
        await bot.send_message(
            chat_id=mentor_id,
            text=mentor_report,
            reply_markup=mentor_kb,
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.error(f"Не удалось отправить заявку наставнику {mentor_id}: {e}")

    await state.clear()


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
                   
