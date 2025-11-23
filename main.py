"""
Telegram bot for selling access to online ЕГЭ schools.
Features:
- /start sends a nice promo message with "💸 Купить доступ" button
- Subjects with prices; choose subject -> choose school ("стобальный", "пифагор") -> show product info with price, card number and recipient FIO
- After payment instruction: send чек менеджеру @qwuzinw
- Admin panel via /admin (only admin_id from config.json) with two buttons: "рассылка" and "указать номер карты"
  - Рассылка: admin provides text, confirms, and bot sends message to all users with safe handling and reporting
  - Указать номер карты: admin can change card number and FIO without editing code
- Stores users and settings in SQLite (data.db)
- Basic anti-abuse: admin-only actions, rate-limited broadcast, validation of inputs

Dependencies:
  pip install aiogram aiosqlite

How to configure:
  1) Create config.json next to this file with the following content:
     {
       "BOT_TOKEN": "<your-bot-token>",
       "ADMIN_ID": 123456789
     }
  2) Run: python tg_school_bot.py

Manager username is set by MANAGER_USERNAME constant in code (default: "qwuzinw").

"""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from typing import Dict

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# ----------------- CONFIG -----------------
CONFIG_FILE = 'config.json'
DB_FILE = 'data.db'
MANAGER_USERNAME = 'qwuzinw'  # manager to whom users should send чек

# Load config
with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
    cfg = json.load(f)
BOT_TOKEN = cfg['BOT_TOKEN']
ADMIN_ID = int(cfg['ADMIN_ID'])

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------- BOT SETUP -----------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ----------------- DB UTIL -----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    # default card settings if missing
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", ("card_number", "0000 0000 0000 0000"))
    cur.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", ("recipient_fio", "Ф.И.О. Получателя"))
    conn.commit()
    conn.close()


def db_set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('REPLACE INTO settings(key, value) VALUES(?, ?)', (key, value))
    conn.commit()
    conn.close()


def db_get_setting(key: str) -> str:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ''


def db_add_user(user: types.User):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('REPLACE INTO users(user_id, username, first_name, last_name, registered_at) VALUES(?,?,?,?,?)', (
        user.id, user.username or '', user.first_name or '', user.last_name or '', datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()


def db_get_all_user_ids():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute('SELECT user_id FROM users')
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]

# ----------------- PRODUCTS -----------------
SUBJECTS = {
    'math_p': ('Профильная математика', 499),
    'rus': ('Русский язык', 499),
    'bio': ('Биология', 349),
    'info': ('Информатика', 349),
    'hist': ('История', 349),
    'soc': ('Обществознание', 349),
    'chem': ('Химия', 329),
    'phys': ('Физика', 329),
}
SCHOOLS = ['стобальный', 'пифагор']

# ----------------- FSM -----------------
class AdminStates(StatesGroup):
    waiting_broadcast_text = State()
    waiting_broadcast_confirm = State()
    waiting_card_number = State()
    waiting_recipient_fio = State()

class PurchaseStates(StatesGroup):
    waiting_subject = State()
    waiting_school = State()

# ----------------- UTIL UI -----------------

def make_start_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton('💸 Купить доступ', callback_data='buy'))
    return kb


def make_subjects_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    for key, (title, price) in SUBJECTS.items():
        kb.insert(InlineKeyboardButton(f"{title} — {price}₽", callback_data=f'subj|{key}'))
    kb.add(InlineKeyboardButton('⬅️ Назад', callback_data='back_start'))
    return kb


def make_schools_keyboard(subject_key: str):
    kb = InlineKeyboardMarkup(row_width=2)
    for s in SCHOOLS:
        kb.insert(InlineKeyboardButton(s.capitalize(), callback_data=f'school|{subject_key}|{s}'))
    kb.add(InlineKeyboardButton('⬅️ Назад', callback_data='back_subjects'))
    return kb


def make_admin_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton('📣 Рассылка', callback_data='admin_broadcast'))
    kb.add(InlineKeyboardButton('💳 Указать номер карты', callback_data='admin_set_card'))
    return kb

# ----------------- HANDLERS -----------------
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    # register user
    db_add_user(message.from_user)
    text = (
        "🎓 Добро пожаловать в *ЕГЭ Школу Онлайн* — быстрые и понятные курсы для уверенной подготовки к экзаменам!\n\n"
        "Здесь вы можете купить доступ к видеоурокам, авторским заданиям и разбору задач от опытных преподавателей.\n\n"
        "📚 Доступна подготовка по профильной и базовой программе, персональные чек-листы и рекомендации.\n\n"
        "Выберите предмет и программу — получите готовую дорожную карту подготовки и материалы сразу после оплаты.")
    await message.answer(text, reply_markup=make_start_keyboard(), parse_mode='Markdown')

@dp.callback_query_handler(lambda c: c.data == 'buy')
async def process_buy(cb: types.CallbackQuery):
    await cb.answer()
    await bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                                text='Выберите предмет:', reply_markup=make_subjects_keyboard())

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('subj|'))
async def process_subject(cb: types.CallbackQuery):
    await cb.answer()
    _, subj_key = cb.data.split('|', 1)
    await bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                                text=f"Предмет: *{SUBJECTS[subj_key][0]}*\nВыберите программу:", reply_markup=make_schools_keyboard(subj_key), parse_mode='Markdown')

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('school|'))
async def process_school(cb: types.CallbackQuery):
    await cb.answer()
    _, subj_key, school = cb.data.split('|', 2)
    subj_title, price = SUBJECTS[subj_key]
    # fetch card info
    card = db_get_setting('card_number')
    fio = db_get_setting('recipient_fio')
    text = (
        f"*Товар:* {subj_title} — {school}\n"
        f"*Цена:* {price}₽\n\n"
        f"*Реквизиты для оплаты:*\n{card}\n{fio}\n\n"
        f"После оплаты пришлите, пожалуйста, чек менеджеру @{MANAGER_USERNAME}.\n"
        "Мы пришлем доступ в течение рабочего времени."
    )
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton('Связаться с менеджером', url=f'https://t.me/{MANAGER_USERNAME}'))
    kb.add(InlineKeyboardButton('⬅️ Назад к предметам', callback_data='back_subjects'))
    await bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                                text=text, parse_mode='Markdown', reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data == 'back_subjects')
async def back_subjects(cb: types.CallbackQuery):
    await cb.answer()
    await bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                                text='Выберите предмет:', reply_markup=make_subjects_keyboard())

@dp.callback_query_handler(lambda c: c.data == 'back_start')
async def back_start(cb: types.CallbackQuery):
    await cb.answer()
    text = (
        "🎓 Добро пожаловать в *ЕГЭ Школу Онлайн* — быстрые и понятные курсы для уверенной подготовки к экзаменам!\n\n"
        "Здесь вы можете купить доступ к видеоурокам..."
    )
    await bot.edit_message_text(chat_id=cb.message.chat.id, message_id=cb.message.message_id,
                                text=text, reply_markup=make_start_keyboard(), parse_mode='Markdown')

# ----------------- ADMIN -----------------
@dp.message_handler(commands=['admin'])
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.reply('Доступ запрещён.')
        return
    await message.reply('Панель администратора:', reply_markup=make_admin_keyboard())

@dp.callback_query_handler(lambda c: c.data == 'admin_broadcast')
async def admin_broadcast(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer('Нет доступа', show_alert=True)
        return
    await cb.answer()
    await bot.send_message(ADMIN_ID, 'Отправьте текст для рассылки (макс 4000 символов).')
    await AdminStates.waiting_broadcast_text.set()

@dp.message_handler(state=AdminStates.waiting_broadcast_text, content_types=types.ContentTypes.TEXT)
async def receive_broadcast_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply('Нет доступа.')
        return
    text = message.text[:4000]
    await state.update_data(broadcast_text=text)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('✅ Начать рассылку', callback_data='broadcast_confirm'))
    kb.add(InlineKeyboardButton('❌ Отмена', callback_data='broadcast_cancel'))
    await message.reply('Предпросмотр рассылки:\n\n' + text, reply_markup=kb)
    await AdminStates.waiting_broadcast_confirm.set()

@dp.callback_query_handler(lambda c: c.data in ('broadcast_cancel', 'broadcast_confirm'), state=AdminStates.waiting_broadcast_confirm)
async def broadcast_confirm_or_cancel(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer('Нет доступа', show_alert=True)
        return
    if cb.data == 'broadcast_cancel':
        await state.finish()
        await cb.answer('Рассылка отменена')
        await bot.send_message(ADMIN_ID, 'Отменено.')
        return
    # confirm
    data = await state.get_data()
    text = data.get('broadcast_text', '')
    await cb.answer('Запуск рассылки...')
    user_ids = db_get_all_user_ids()
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)  # small delay to be polite
        except Exception as e:
            logger.exception(f'Failed to send to {uid}: {e}')
            failed += 1
            await asyncio.sleep(0.05)
    await bot.send_message(ADMIN_ID, f'Готово. Отправлено: {sent}. Не доставлено: {failed}.')
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'admin_set_card')
async def admin_set_card(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer('Нет доступа', show_alert=True)
        return
    await cb.answer()
    await bot.send_message(ADMIN_ID, 'Введите номер карты (или реквизиты) — отправьте одним сообщением:')
    await AdminStates.waiting_card_number.set()

@dp.message_handler(state=AdminStates.waiting_card_number, content_types=types.ContentTypes.TEXT)
async def receive_card_number(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply('Нет доступа.')
        return
    card = message.text.strip()
    await state.update_data(card_number=card)
    await message.reply('Теперь укажите ФИО получателя:')
    await AdminStates.waiting_recipient_fio.set()

@dp.message_handler(state=AdminStates.waiting_recipient_fio, content_types=types.ContentTypes.TEXT)
async def receive_recipient_fio(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.reply('Нет доступа.')
        return
    fio = message.text.strip()
    data = await state.get_data()
    card = data.get('card_number', '')
    db_set_setting('card_number', card)
    db_set_setting('recipient_fio', fio)
    await message.reply(f'Реквизиты обновлены:\n{card}\n{fio}')
    await state.finish()

# ----------------- SAFETY / MISC -----------------
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def catch_all(message: types.Message):
    # polite fallback for unknown messages
    if message.text and message.text.startswith('/'):
        return  # unknown commands ignored
    await message.reply('Команда не распознана. Нажмите /start чтобы вернуться в начало.')

# ----------------- START -----------------
if __name__ == '__main__':
    init_db()
    print('Bot is starting...')
    executor.start_polling(dp, skip_updates=True)
