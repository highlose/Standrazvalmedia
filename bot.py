import asyncio
import logging
import os
import sqlite3
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

# ================= КОНФИГУРАЦИЯ (ДЛЯ RAILWAY) =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_БОТА")
# ID админа (или нескольких через запятую в ENV: "123456789,987654321")
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "123456789").split(",")
    if x.strip().isdigit()
]
# Путь к БД (если на Railway подключен Volume, укажи например /data/standrazval.db)
DB_PATH = os.getenv("DB_PATH", "standrazval.db")

# Обязательные каналы для подписки (бот должен быть админом в этих каналах!)
REQUIRED_CHANNELS = [
    {"chat_id": "@highreverse", "title": "highreverse", "url": "https://t.me/highreverse"},
    {"chat_id": "@StandRazval", "title": "StandRazval", "url": "https://t.me/StandRazval"},
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ================= ПРОВЕРКА ПОДПИСКИ =================
async def get_unsubscribed_channels(user_id: int) -> list[dict]:
    if user_id in ADMIN_IDS:
        return []

    not_subscribed = []
    allowed_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }
    for ch in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status not in allowed_statuses:
                not_subscribed.append(ch)
        except Exception as e:
            logging.error(f"Ошибка проверки подписки на {ch['chat_id']} для {user_id}: {e}")
            not_subscribed.append(ch)
    return not_subscribed


def subscribe_kb(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 Подписаться на {ch['title']}", url=ch["url"])]
        for ch in channels
    ]
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user or user.is_bot:
            return await handler(event, data)

        # Пропускаем сам коллбэк кнопки "Проверить подписку", чтобы обработать его отдельно
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        not_sub = await get_unsubscribed_channels(user.id)
        if not_sub:
            text = (
                "🔒 <b>Доступ к боту ограничен!</b>\n\n"
                "Для использования бота необходимо подписаться на наши Telegram-каналы:\n"
                + "\n".join(f"• <a href='{ch['url']}'>{ch['title']}</a>" for ch in not_sub)
                + "\n\nПосле подписки нажми кнопку <b>«✅ Проверить подписку»</b> ниже:"
            )
            if isinstance(event, Message):
                await event.answer(text, reply_markup=subscribe_kb(not_sub), disable_web_page_preview=True)
            elif isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишись на обязательные каналы!", show_alert=True)
                if event.message:
                    await event.message.answer(text, reply_markup=subscribe_kb(not_sub), disable_web_page_preview=True)
            return

        return await handler(event, data)


dp.update.outer_middleware(SubscriptionMiddleware())


@router.callback_query(F.data == "check_sub")
async def callback_check_sub(callback: CallbackQuery, state: FSMContext):
    not_sub = await get_unsubscribed_channels(callback.from_user.id)
    if not_sub:
        await callback.answer("❌ Ты подписался ещё не на все каналы!", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=subscribe_kb(not_sub))
        except Exception:
            pass
        return

    await state.clear()
    save_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    await callback.answer("✅ Подписка подтверждена!")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "✅ <b>Спасибо за подписку! Доступ к боту открыт.</b>\n\n"
        "Выбери нужное действие в меню ниже:",
        reply_markup=main_menu_kb(callback.from_user.id),
    )


# ================= БАЗА ДАННЫХ (SQLite) =================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                answers TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                admin_msg_id INTEGER,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def save_user(user_id: int, username: Optional[str], full_name: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name
            """,
            (user_id, username or "нет", full_name),
        )


def get_active_application(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, status FROM applications WHERE user_id = ? AND status IN ('pending', 'approved') ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        return cur.fetchone()


def create_application(user_id: int, username: Optional[str], answers_text: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO applications (user_id, username, answers, status) VALUES (?, ?, ?, 'pending')",
            (user_id, username or "нет", answers_text),
        )
        return cur.lastrowid


def update_application_status(app_id: int, status: str) -> Optional[int]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT user_id FROM applications WHERE id = ?", (app_id,))
        row = cur.fetchone()
        if not row:
            return None
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        return row[0]


# ================= FSM СОСТОЯНИЯ =================
class MediaForm(StatesGroup):
    q1_nickname = State()
    q2_main_link = State()
    q3_extra_links = State()
    q4_age = State()
    q5_direction = State()
    q5_direction_other = State()
    q6_frequency = State()
    q7_audience = State()
    q8_experience = State()
    q9_rules = State()
    q10_contact = State()


class SupportState(StatesGroup):
    waiting_message = State()


# ================= КЛАВИАТУРЫ =================
def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📝 Подать заявку в медиаполе")],
        [KeyboardButton(text="📊 Статус моей заявки"), KeyboardButton(text="🛠 Техподдержка")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def skip_or_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭ Пропустить")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def q5_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Стримы")],
            [KeyboardButton(text="Обзоры и гайды")],
            [KeyboardButton(text="Мемы / короткие видео (Shorts и т.д.)")],
            [KeyboardButton(text="✍️ Другое (написать свой вариант)")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def q6_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1–2 единицы контента")],
            [KeyboardButton(text="3–5 единиц")],
            [KeyboardButton(text="6+ (стримлю/пощу почти каждый день)")],
            [KeyboardButton(text="Пока не знаю, зависит от загрузки")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def q7_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="До 100"), KeyboardButton(text="100–1000")],
            [KeyboardButton(text="1000–10 000"), KeyboardButton(text="10 000+")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def q9_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, полностью готов!")],
            [KeyboardButton(text="Готов, но есть вопросы")],
            [KeyboardButton(text="Нет.")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def admin_review_kb(app_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"app_approve:{app_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"app_reject:{app_id}"),
            ],
            [
                InlineKeyboardButton(text="💬 Написать кандидату", url=f"tg://user?id={user_id}"),
            ],
        ]
    )


# ================= БАЗОВЫЕ КОМАНДЫ =================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await message.answer(
        "👋 Привет! Это официальный бот набора в <b>медиаполе StandRazval</b>.\n\n"
        "Здесь ты можешь заполнить анкету кандидата или обратиться в техподдержку.\n"
        "Выбери нужное действие в меню ниже:",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@router.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Действие отменено. Возвращаю в главное меню.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@router.message(F.text == "📊 Статус моей заявки")
async def check_status(message: Message):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT id, status, created_at FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (message.from_user.id,),
        )
        row = cur.fetchone()

    if not row:
        await message.answer("У тебя пока нет поданных заявок. Нажми <b>«📝 Подать заявку в медиаполе»</b>.")
        return

    app_id, status, created_at = row
    status_map = {
        "pending": "⏳ На проверке у администрации",
        "approved": "✅ Одобрена! Ожидай связи от администратора",
        "rejected": "❌ Отклонена (ты можешь подать заявку повторно позже)",
    }
    await message.answer(
        f"📄 <b>Заявка #{app_id}</b>\n"
        f"📅 Дата подачи: {created_at}\n"
        f"📌 Статус: <b>{status_map.get(status, status)}</b>"
    )


# ================= ОПРОСНИК (10 ВОПРОСОВ) =================
@router.message(F.text == "📝 Подать заявку в медиаполе")
async def start_form(message: Message, state: FSMContext):
    existing = get_active_application(message.from_user.id)
    if existing:
        app_id, status = existing
        if status == "pending":
            await message.answer(f"⏳ Твоя заявка <b>#{app_id}</b> уже находится на проверке. Дождись решения админа!")
            return
        if status == "approved":
            await message.answer(f"✅ Твоя заявка <b>#{app_id}</b> уже одобрена!")
            return

    await state.set_state(MediaForm.q1_nickname)
    await message.answer(
        "🚀 Начинаем заполнение анкеты в <b>медиаполе StandRazval</b> (10 вопросов).\n\n"
        "<b>1/10. Никнейм (в игре и на площадках)</b> *",
        reply_markup=cancel_kb(),
    )


@router.message(MediaForm.q1_nickname)
async def process_q1(message: Message, state: FSMContext):
    await state.update_data(q1=message.text.strip())
    await state.set_state(MediaForm.q2_main_link)
    await message.answer(
        "<b>2/10. Ссылка на твой основной контент-канал (YouTube / Twitch / TikTok / VK / Telegram)</b> *",
        reply_markup=cancel_kb(),
    )


@router.message(MediaForm.q2_main_link)
async def process_q2(message: Message, state: FSMContext):
    await state.update_data(q2=message.text.strip())
    await state.set_state(MediaForm.q3_extra_links)
    await message.answer(
        "<b>3/10. Ссылки на дополнительные площадки (если есть)</b>\n"
        "<i>Необязательный вопрос — можешь нажать «Пропустить».</i>",
        reply_markup=skip_or_cancel_kb(),
    )


@router.message(MediaForm.q3_extra_links)
async def process_q3(message: Message, state: FSMContext):
    val = "Не указано" if message.text == "⏭ Пропустить" else message.text.strip()
    await state.update_data(q3=val)
    await state.set_state(MediaForm.q4_age)
    await message.answer(
        "<b>4/10. Сколько тебе лет?</b> *",
        reply_markup=cancel_kb(),
    )


@router.message(MediaForm.q4_age)
async def process_q4(message: Message, state: FSMContext):
    await state.update_data(q4=message.text.strip())
    await state.set_state(MediaForm.q5_direction)
    await message.answer(
        "<b>5/10. Какое направление контента тебе ближе?</b> *\n"
        "Выбери вариант кнопкой или напиши свой:",
        reply_markup=q5_kb(),
    )


@router.message(MediaForm.q5_direction)
async def process_q5(message: Message, state: FSMContext):
    if message.text == "✍️ Другое (написать свой вариант)":
        await state.set_state(MediaForm.q5_direction_other)
        await message.answer("Напиши своё направление контента:", reply_markup=cancel_kb())
        return

    await state.update_data(q5=message.text.strip())
    await state.set_state(MediaForm.q6_frequency)
    await message.answer(
        "<b>6/10. Сколько контента ты готов выпускать в неделю?</b> *",
        reply_markup=q6_kb(),
    )


@router.message(MediaForm.q5_direction_other)
async def process_q5_other(message: Message, state: FSMContext):
    await state.update_data(q5=f"Другое: {message.text.strip()}")
    await state.set_state(MediaForm.q6_frequency)
    await message.answer(
        "<b>6/10. Сколько контента ты готов выпускать в неделю?</b> *",
        reply_markup=q6_kb(),
    )


@router.message(MediaForm.q6_frequency)
async def process_q6(message: Message, state: FSMContext):
    await state.update_data(q6=message.text.strip())
    await state.set_state(MediaForm.q7_audience)
    await message.answer(
        "<b>7/10. Какая у тебя текущая аудитория (суммарно по всем площадкам)?</b> *",
        reply_markup=q7_kb(),
    )


@router.message(MediaForm.q7_audience)
async def process_q7(message: Message, state: FSMContext):
    await state.update_data(q7=message.text.strip())
    await state.set_state(MediaForm.q8_experience)
    await message.answer(
        "<b>8/10. Есть ли у тебя опыт в медиаполе / работе с играми?</b>\n"
        "<i>Необязательный вопрос — можешь нажать «Пропустить».</i>",
        reply_markup=skip_or_cancel_kb(),
    )


@router.message(MediaForm.q8_experience)
async def process_q8(message: Message, state: FSMContext):
    val = "Не указано" if message.text == "⏭ Пропустить" else message.text.strip()
    await state.update_data(q8=val)
    await state.set_state(MediaForm.q9_rules)
    await message.answer(
        "<b>9/10. Готов(а) ли ты соблюдать правила медиаполя (дедлайны, маркировка спойлеров, NDA на ранние версии)?</b> *",
        reply_markup=q9_kb(),
    )


@router.message(MediaForm.q9_rules)
async def process_q9(message: Message, state: FSMContext):
    await state.update_data(q9=message.text.strip())
    await state.set_state(MediaForm.q10_contact)

    kb_rows = []
    if message.from_user.username:
        kb_rows.append([KeyboardButton(text=f"Использовать @{message.from_user.username}")])
    kb_rows.append([KeyboardButton(text="❌ Отмена")])

    await message.answer(
        "<b>10/10. Укажи контакт для связи (ник / ссылка) (Telegram)</b> *",
        reply_markup=ReplyKeyboardMarkup(keyboard=kb_rows, resize_keyboard=True),
    )


@router.message(MediaForm.q10_contact)
async def process_q10(message: Message, state: FSMContext):
    contact = message.text.replace("Использовать ", "").strip()
    data = await state.get_data()
    await state.clear()

    user = message.from_user
    tg_tag = f"@{user.username}" if user.username else f"id:{user.id}"

    formatted_answers = (
        f"1️⃣ <b>Никнейм:</b> {data['q1']}\n"
        f"2️⃣ <b>Основной канал:</b> {data['q2']}\n"
        f"3️⃣ <b>Доп. площадки:</b> {data['q3']}\n"
        f"4️⃣ <b>Возраст:</b> {data['q4']}\n"
        f"5️⃣ <b>Направление:</b> {data['q5']}\n"
        f"6️⃣ <b>Контента в неделю:</b> {data['q6']}\n"
        f"7️⃣ <b>Аудитория:</b> {data['q7']}\n"
        f"8️⃣ <b>Опыт в медиа:</b> {data['q8']}\n"
        f"9️⃣ <b>Соблюдение правил / NDA:</b> {data['q9']}\n"
        f"🔟 <b>Контакт:</b> {contact}"
    )

    app_id = create_application(user.id, user.username, formatted_answers)

    await message.answer(
        f"✅ <b>Заявка #{app_id} успешно отправлена!</b>\n\n"
        "Администратор проверит твои ответы и вынесет решение. Бот пришлёт тебе уведомление.",
        reply_markup=main_menu_kb(user.id),
    )

    admin_text = (
        f"📥 <b>Новая заявка в медиаполе StandRazval (#{app_id})</b>\n"
        f"👤 От: {user.full_name} ({tg_tag} | <code>{user.id}</code>)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{formatted_answers}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=admin_review_kb(app_id, user.id),
            )
        except Exception as e:
            logging.error(f"Не удалось отправить заявку админу {admin_id}: {e}")


# ================= ПРОВЕРКА ЗАЯВОК АДМИНОМ =================
@router.callback_query(F.data.startswith("app_approve:"))
async def approve_application(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав!", show_alert=True)
        return

    app_id = int(callback.data.split(":")[1])
    target_user_id = update_application_status(app_id, "approved")

    if not target_user_id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    await callback.message.edit_text(
        callback.message.html_text + f"\n\n✅ <b>ОДОБРЕНО</b> (админ: {callback.from_user.full_name})",
        reply_markup=None,
    )
    await callback.answer("Заявка одобрена!")

    try:
        await bot.send_message(
            target_user_id,
            f"🎉 <b>Поздравляем! Твоя заявка #{app_id} в медиаполе StandRazval одобрена!</b>\n"
            "В ближайшее время с тобой свяжется администратор по указанному контакту.",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("app_reject:"))
async def reject_application(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет прав!", show_alert=True)
        return

    app_id = int(callback.data.split(":")[1])
    target_user_id = update_application_status(app_id, "rejected")

    if not target_user_id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    await callback.message.edit_text(
        callback.message.html_text + f"\n\n❌ <b>ОТКЛОНЕНО</b> (админ: {callback.from_user.full_name})",
        reply_markup=None,
    )
    await callback.answer("Заявка отклонена.")

    try:
        await bot.send_message(
            target_user_id,
            f"😔 <b>К сожалению, твоя заявка #{app_id} в медиаполе StandRazval отклонена.</b>\n"
            "Если у тебя есть вопросы — можешь написать в техподдержку бота.",
        )
    except Exception:
        pass


# ================= ТЕХПОДДЕРЖКА (ТИКЕТЫ ЧЕРЕЗ REPLY) =================
@router.message(F.text == "🛠 Техподдержка")
async def support_start(message: Message, state: FSMContext):
    await state.set_state(SupportState.waiting_message)
    await message.answer(
        "🛠 <b>Техподдержка StandRazval</b>\n\n"
        "Опиши свой вопрос или проблему одним сообщением (можно прикрепить скриншот). "
        "Администратор ответит тебе прямо сюда:",
        reply_markup=cancel_kb(),
    )


@router.message(SupportState.waiting_message)
async def support_receive(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    tg_tag = f"@{user.username}" if user.username else "без юзернейма"

    header = (
        f"🆘 <b>Новое обращение в техподдержку</b>\n"
        f"👤 Юзер: {user.full_name} ({tg_tag})\n"
        f"🆔 UserID: <code>{user.id}</code>\n"
        f"<i>Ответь на это сообщение (Reply), чтобы отправить ответ пользователю.</i>"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, header)
            copied = await message.copy_to(admin_id)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO support_tickets (user_id, admin_msg_id) VALUES (?, ?)",
                    (user.id, copied.message_id),
                )
        except Exception as e:
            logging.error(f"Ошибка пересылки тикета админу {admin_id}: {e}")

    await message.answer(
        "✅ Сообщение отправлено в техподдержку! Ожидай ответа.",
        reply_markup=main_menu_kb(user.id),
    )


# Ответ админа на тикет через Reply
@router.message(F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_reply_ticket(message: Message):
    replied_id = message.reply_to_message.message_id
    target_user_id = None

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT user_id FROM support_tickets WHERE admin_msg_id = ?",
            (replied_id,),
        )
        row = cur.fetchone()
        if row:
            target_user_id = row[0]

    # Резервный поиск ID в тексте шапки, если админ ответил на шапку
    if not target_user_id and message.reply_to_message.text:
        for line in message.reply_to_message.text.splitlines():
            if "UserID:" in line:
                digits = "".join(filter(str.isdigit, line))
                if digits:
                    target_user_id = int(digits)

    if not target_user_id:
        return

    try:
        await bot.send_message(target_user_id, "💬 <b>Ответ от техподдержки:</b>")
        await message.copy_to(target_user_id)
        await message.reply("✅ Ответ доставлен пользователю.")
    except Exception as e:
        await message.reply(f"❌ Не удалось доставить ответ (возможно, юзер заблокировал бота): {e}")


# ================= АДМИН-ПАНЕЛЬ =================
@router.message(Command("admin"))
@router.message(F.text == "👑 Админ-панель")
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pending_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status = 'pending'").fetchone()[0]
        approved_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status = 'approved'").fetchone()[0]
        rejected_apps = conn.execute("SELECT COUNT(*) FROM applications WHERE status = 'rejected'").fetchone()[0]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏳ Заявки на проверке ({pending_apps})", callback_data="admin_list_pending")]
        ]
    )

    await message.answer(
        f"👑 <b>Админ-панель StandRazval</b>\n\n"
        f"👥 Пользователей в боте: <b>{total_users}</b>\n"
        f"⏳ Ожидают проверки: <b>{pending_apps}</b>\n"
        f"✅ Одобрено: <b>{approved_apps}</b>\n"
        f"❌ Отклонено: <b>{rejected_apps}</b>",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin_list_pending")
async def admin_list_pending(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, user_id, username, answers FROM applications WHERE status = 'pending' ORDER BY id ASC LIMIT 5"
        ).fetchall()

    if not rows:
        await callback.answer("Нет заявок на проверке!", show_alert=True)
        return

    await callback.answer()
    for app_id, user_id, username, answers in rows:
        await callback.message.answer(
            f"📋 <b>Заявка #{app_id}</b> (@{username} | <code>{user_id}</code>)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n{answers}",
            reply_markup=admin_review_kb(app_id, user_id),
        )


# ================= ЗАПУСК =================
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
