import asyncio
import logging
import json
import urllib.parse
import os
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp

# ================= НАСТРОЙКИ =================
# Берем токен и URL базы из переменных среды (настроим на Render)
BOT_TOKEN = os.getenv("BOT_TOKEN") 
DATABASE_URL = os.getenv("DATABASE_URL") # Сюда Render сам подставит ссылку

# !!! ЗАМЕНИ ЭТИ ДВЕ СТРОЧКИ НА СВОИ !!!
ADMIN_ID = 1831662688  
GITHUB_URL = "https://твое-имя.github.io/donate-bot/" 
MANAGER_USERNAME = "tombirdi" 
# =============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pool = None # Пул соединений с БД

# --- ВЕБ-СЕРВЕР (Health Check) ---
async def health_check(request):
    return web.Response(text="Bot is alive & DB connected!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# --- БАЗА ДАННЫХ (POSTGRESQL) ---
async def init_db():
    global pool
    # Подключаемся к внешней базе
    pool = await asyncpg.create_pool(DATABASE_URL)
    
    async with pool.acquire() as conn:
        # Создаем таблицы (Postgres синтаксис)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                item_name TEXT,
                price INTEGER,
                status TEXT DEFAULT 'wait',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Создаем админа, если его нет (ON CONFLICT DO NOTHING)
        await conn.execute('''
            INSERT INTO users (user_id, username, balance) 
            VALUES ($1, 'Admin', 999999) 
            ON CONFLICT (user_id) DO NOTHING
        ''', ADMIN_ID)
        logging.info("База данных успешно подключена!")

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Guest"

    async with pool.acquire() as conn:
        # 1. Регистрируем или обновляем юзера
        await conn.execute('''
            INSERT INTO users (user_id, username, balance) VALUES ($1, $2, 0)
            ON CONFLICT (user_id) DO UPDATE SET username = $2
        ''', user_id, username)
        
        # 2. Получаем баланс
        balance = await conn.fetchval('SELECT balance FROM users WHERE user_id = $1', user_id)
        
        # 3. Мои заказы
        rows = await conn.fetch('SELECT item_name, price, status FROM orders WHERE user_id = $1 ORDER BY id DESC LIMIT 10', user_id)
        my_orders = [{'item': r['item_name'], 'price': r['price'], 'status': r['status']} for r in rows]
        
        # 4. Админские заказы
        admin_orders = []
        if user_id == ADMIN_ID:
            rows_adm = await conn.fetch("SELECT id, username, item_name, user_id, price FROM orders WHERE status = 'wait'")
            admin_orders = [{'id': r['id'], 'user': r['username'], 'item': r['item_name'], 'uid': r['user_id'], 'price': r['price']} for r in rows_adm]

    data_payload = {
        'bal': balance,
        'admin': (user_id == ADMIN_ID),
        'manager': MANAGER_USERNAME,
        'orders': my_orders,
        'admin_orders': admin_orders
    }
    
    encoded = urllib.parse.quote(json.dumps(data_payload))
    link = f"{GITHUB_URL}?data={encoded}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Открыть Магазин", web_app=WebAppInfo(url=link))]])
    await bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=link)))
    await message.answer(f"Привет! Баланс: {balance} ₽", reply_markup=kb)

@dp.message(F.web_app_data)
async def web_app_handler(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get('action')
        user_id = message.from_user.id
        username = message.from_user.username or "Guest"

        async with pool.acquire() as conn:
            
            # --- ПОКУПКА ---
            if action == 'buy':
                price = int(data['price'])
                item = data['item']
                
                # Транзакция (чтобы деньги не списались, если ошибка)
                async with conn.transaction():
                    balance = await conn.fetchval('SELECT balance FROM users WHERE user_id = $1', user_id)
                    
                    if balance >= price:
                        new_bal = balance - price
                        await conn.execute('UPDATE users SET balance = $1 WHERE user_id = $2', new_bal, user_id)
                        await conn.execute('INSERT INTO orders (user_id, username, item_name, price) VALUES ($1, $2, $3, $4)', 
                                         user_id, username, item, price)
                        
                        await message.answer(f"✅ Успешно куплено: <b>{item}</b>\n💰 Остаток: {new_bal} ₽", parse_mode="HTML")
                        if user_id != ADMIN_ID:
                            try:
                                await bot.send_message(ADMIN_ID, f"🔔 Новый заказ от @{username}: {item}")
                            except: pass
                    else:
                        await message.answer("❌ Недостаточно средств.")

            # --- АДМИН: ВЫДАЧА ДЕНЕГ ---
            elif action == 'give_money':
                if user_id == ADMIN_ID:
                    target = int(data['target'])
                    amount = int(data['amount'])
                    
                    # Проверяем, есть ли такой юзер в базе
                    exists = await conn.fetchval('SELECT 1 FROM users WHERE user_id = $1', target)
                    if not exists:
                        # Если нет, создаем пустышку, чтобы начислить
                        await conn.execute('INSERT INTO users (user_id, username, balance) VALUES ($1, $2, 0)', target, 'Unknown')
                    
                    await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, target)
                    await message.answer(f"✅ Выдано {amount}₽ игроку {target}")
                    try:
                        await bot.send_message(target, f"💰 Вам начислено {amount} ₽")
                    except: pass

            # --- АДМИН: ВЫДАЧА ЗАКАЗА ---
            elif action == 'order_done':
                if user_id == ADMIN_ID:
                    oid = int(data['order_id'])
                    target = int(data['target'])
                    await conn.execute("UPDATE orders SET status = 'done' WHERE id = $1", oid)
                    await message.answer(f"✅ Заказ #{oid} закрыт.")
                    try:
                        await bot.send_message(target, "✅ Ваш заказ выдан!")
                    except: pass

    except Exception as e:
        logging.error(f"Error: {e}")

async def main():
    await init_db()
    await asyncio.gather(start_web_server(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())