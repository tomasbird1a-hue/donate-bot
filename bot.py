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
# Токен берем из переменных среды (безопасность) или вставляем жестко для теста
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ВСТАВЬ_СЮДА_ЕСЛИ_НЕ_РАБОТАЕТ_ENV")
ADMIN_ID = 1831662688 # Твой ID цифрами
GITHUB_URL = "https://tomasbird1a-hue.github.io/donate-bot/" # Твоя ссылка на GitHub
MANAGER_USERNAME = "tombirdi" 
# =============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ФЕЙКОВЫЙ СЕРВЕР ДЛЯ RENDER ---
async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render выдает порт через переменную окружения PORT, или используем 8080
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect('store.db') as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            item_name TEXT,
            price INTEGER,
            status TEXT DEFAULT 'wait'
        )''')
        # Создаем админа
        await db.execute('INSERT OR IGNORE INTO users (user_id, balance, is_admin) VALUES (?, 999999, 1)', (ADMIN_ID,))
        await db.commit()

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Guest"

    async with aiosqlite.connect('store.db') as db:
        await db.execute('INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0)', (user_id, username))
        await db.commit()
        
        async with db.execute('SELECT balance, is_admin FROM users WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance, is_admin = row if row else (0, 0)
        
        # Мои заказы
        my_orders = []
        async with db.execute('SELECT item_name, price, status FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 5', (user_id,)) as cursor:
            async for r in cursor:
                my_orders.append({'item': r[0], 'price': r[1], 'status': r[2]})
        
        # Заказы для админа
        admin_orders = []
        if is_admin:
            async with db.execute('SELECT id, username, item_name, user_id FROM orders WHERE status = "wait"') as cursor:
                async for r in cursor:
                    admin_orders.append({'id': r[0], 'user': r[1], 'item': r[2], 'uid': r[3]})

    data_payload = {
        'bal': balance,
        'admin': bool(is_admin),
        'manager': MANAGER_USERNAME,
        'orders': my_orders,
        'admin_orders': admin_orders
    }
    
    encoded = urllib.parse.quote(json.dumps(data_payload))
    link = f"{GITHUB_URL}?data={encoded}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Открыть Магазин", web_app=WebAppInfo(url=link))]])
    await bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=link)))
    await message.answer(f"Привет! Твой баланс: {balance} ₽", reply_markup=kb)

@dp.message(F.web_app_data)
async def web_app_handler(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get('action')
        user_id = message.from_user.id
        
        async with aiosqlite.connect('store.db') as db:
            
            # 1. ПОКУПКА
            if action == 'buy':
                price = int(data['price'])
                item = data['item']
                
                async with db.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)) as cursor:
                    bal = (await cursor.fetchone())[0]
                
                if bal >= price:
                    new_bal = bal - price
                    await db.execute('UPDATE users SET balance = ? WHERE user_id = ?', (new_bal, user_id))
                    await db.execute('INSERT INTO orders (user_id, username, item_name, price) VALUES (?, ?, ?, ?)', 
                                     (user_id, message.from_user.username, item, price))
                    await db.commit()
                    await message.answer(f"✅ Куплено: {item}\nОстаток: {new_bal} ₽")
                    if user_id != ADMIN_ID:
                        await bot.send_message(ADMIN_ID, f"🔔 Новый заказ от {message.from_user.first_name}: {item}")
                else:
                    await message.answer("❌ Недостаточно денег!")

            # 2. АДМИН: ВЫДАЧА ДЕНЕГ
            elif action == 'give_money':
                # ПРОВЕРКА НА АДМИНА СТРОГО В КОДЕ
                if user_id == ADMIN_ID: 
                    target = int(data['target'])
                    amount = int(data['amount'])
                    await db.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, target))
                    await db.commit()
                    await message.answer(f"✅ Баланс игрока {target} пополнен на {amount}₽")
                    try:
                        await bot.send_message(target, f"💰 Вам начислено {amount} ₽")
                    except: pass
                else:
                    await message.answer("❌ Вы не админ.")

            # 3. АДМИН: ВЫДАЧА ЗАКАЗА
            elif action == 'order_done':
                if user_id == ADMIN_ID:
                    oid = int(data['order_id'])
                    target = int(data['target'])
                    await db.execute('UPDATE orders SET status = "done" WHERE id = ?', (oid,))
                    await db.commit()
                    await message.answer(f"✅ Заказ #{oid} закрыт.")
                    try:
                        await bot.send_message(target, "✅ Ваш заказ выдан! Проверьте в магазине.")
                    except: pass

    except Exception as e:
        logging.error(f"Error: {e}")

async def main():
    await init_db()
    # Запускаем и веб-сервер (для Render), и бота
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":

    asyncio.run(main())
