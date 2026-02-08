import asyncio
import logging
import json
import urllib.parse
import os
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp

# ================= НАСТРОЙКИ =================
# Вставь сюда токен, если не настроил ENV на Render
BOT_TOKEN = os.getenv("BOT_TOKEN", "7884895293:AAGWVIopZzALxl5zT6rFX1-WaDlwxyOXa2U")
ADMIN_ID = 1831662688  # <--- ОБЯЗАТЕЛЬНО ЗАМЕНИ НА СВОЙ ЦИФРОВОЙ ID
GITHUB_URL = "https://tomasbird1a-hue.github.io/donate-bot/"
MANAGER_USERNAME = "admin_username" 
# =============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ФЕЙКОВЫЙ СЕРВЕР (ЧТОБЫ RENDER НЕ УСНУЛ) ---
async def health_check(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    async with aiosqlite.connect('store.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            item_name TEXT,
            price INTEGER,
            status TEXT DEFAULT 'wait',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        await db.commit()

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: ПРОВЕРКА ЮЗЕРА ---
# Если база стерлась, эта функция вернет юзера обратно в базу
async def ensure_user_exists(user_id, username):
    async with aiosqlite.connect('store.db') as db:
        await db.execute('INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)', (user_id, username))
        await db.commit()

# --- ОБРАБОТЧИК /START ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Guest"

    # 1. Гарантируем, что юзер есть в БД
    await ensure_user_exists(user_id, username)

    # 2. Получаем баланс
    async with aiosqlite.connect('store.db') as db:
        async with db.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = row[0] if row else 0
        
        # 3. Загружаем заказы юзера
        my_orders = []
        async with db.execute('SELECT item_name, price, status FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10', (user_id,)) as cursor:
            async for r in cursor:
                my_orders.append({'item': r[0], 'price': r[1], 'status': r[2]})
        
        # 4. (Если Админ) Загружаем чужие заказы
        admin_orders = []
        if user_id == ADMIN_ID:
            async with db.execute('SELECT id, username, item_name, user_id, price FROM orders WHERE status = "wait"') as cursor:
                async for r in cursor:
                    admin_orders.append({'id': r[0], 'user': r[1], 'item': r[2], 'uid': r[3], 'price': r[4]})

    # 5. Формируем ссылку
    data_payload = {
        'bal': balance,
        'admin': (user_id == ADMIN_ID), # Проверка строго по ID в коде
        'manager': MANAGER_USERNAME,
        'orders': my_orders,
        'admin_orders': admin_orders
    }
    
    encoded = urllib.parse.quote(json.dumps(data_payload))
    link = f"{GITHUB_URL}?data={encoded}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Открыть Магазин", web_app=WebAppInfo(url=link))]])
    await bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=link)))
    
    await message.answer(f"Привет! Баланс: {balance} ₽", reply_markup=kb)


# --- ОБРАБОТКА WEB APP ---
@dp.message(F.web_app_data)
async def web_app_handler(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get('action')
        user_id = message.from_user.id
        username = message.from_user.username or "Guest"

        # СТРАХОВКА: Если база стерлась, создаем юзера на лету
        await ensure_user_exists(user_id, username)

        async with aiosqlite.connect('store.db') as db:
            
            # === ПОКУПКА ===
            if action == 'buy':
                price = int(data['price'])
                item = data['item']
                
                async with db.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    bal = row[0]
                
                if bal >= price:
                    new_bal = bal - price
                    await db.execute('UPDATE users SET balance = ? WHERE user_id = ?', (new_bal, user_id))
                    await db.execute('INSERT INTO orders (user_id, username, item_name, price) VALUES (?, ?, ?, ?)', 
                                     (user_id, username, item, price))
                    await db.commit()
                    
                    await message.answer(f"✅ Успешно куплено: <b>{item}</b>\n💰 Остаток: {new_bal} ₽", parse_mode="HTML")
                    
                    if user_id != ADMIN_ID:
                        await bot.send_message(ADMIN_ID, f"🔔 <b>Новый заказ!</b>\nОт: @{username}\nТовар: {item}", parse_mode="HTML")
                else:
                    await message.answer("❌ Ошибка: Недостаточно средств на счете бота.")

            # === АДМИН: ВЫДАТЬ ДЕНЬГИ ===
            elif action == 'give_money':
                if user_id == ADMIN_ID:
                    target_id = int(data['target'])
                    amount = int(data['amount'])
                    
                    # Страховка для получателя (если его нет в базе)
                    await ensure_user_exists(target_id, "Unknown")
                    
                    await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, target_id))
                    await db.commit()
                    
                    await message.answer(f"✅ Пользователю {target_id} начислено {amount} ₽")
                    try:
                        await bot.send_message(target_id, f"💰 Ваш баланс пополнен на {amount} ₽")
                    except: 
                        await message.answer("⚠️ Пользователь получил деньги, но у него закрыта личка.")
                else:
                    await message.answer("❌ У вас нет прав админа.")

            # === АДМИН: ВЫДАТЬ ЗАКАЗ ===
            elif action == 'order_done':
                if user_id == ADMIN_ID:
                    order_id = int(data['order_id'])
                    target_id = int(data['target'])
                    
                    await db.execute('UPDATE orders SET status = "done" WHERE id = ?', (order_id,))
                    await db.commit()
                    
                    await message.answer(f"✅ Заказ #{order_id} закрыт.")
                    try:
                        await bot.send_message(target_id, "✅ Ваш заказ был выдан! Спасибо за покупку.")
                    except: pass

    except Exception as e:
        logging.error(f"CRITICAL ERROR: {e}")
        await message.answer("Произошла ошибка обработки. Попробуйте нажать /start")

async def main():
    await init_db()
    # Запускаем всё вместе
    await asyncio.gather(start_web_server(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())