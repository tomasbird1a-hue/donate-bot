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

# ================= НАСТРОЙКИ (ТВОИ ДАННЫЕ) =================

# 1. Токен бота
BOT_TOKEN = "7884895293:AAGWVIopZzALxl5zT6rFX1-WaDlwxyOXa2U"

# 2. База данных (Render Internal URL)
# Я добавил авто-исправление, чтобы Python точно её понял
RAW_DB_URL = "postgresql://donate_db_573d_user:YnThVqWCSTGGzrhxvAeEmyATjwJ3WjaM@dpg-d646up75r7bs73a97kk0-a/donate_db_573d"
DATABASE_URL = RAW_DB_URL.replace("postgres://", "postgresql://")

# 3. Твой ID админа (цифрами)
ADMIN_ID = 1831662688

# 4. Ссылка на сайт (GitHub Pages)
# Я исправил ссылку на репозиторий на ссылку САЙТА
GITHUB_URL = "https://tomasbird1a-hue.github.io/donate-bot/index.html"

# 5. Ник менеджера
MANAGER_USERNAME = "tombirdi"

# ===========================================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
pool = None 

# --- WEB SERVER (ЧТОБЫ RENDER НЕ ВЫКЛЮЧАЛ БОТА) ---
async def health_check(request):
    return web.Response(text="Bot is running OK!")

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
    try:
        print(f"🔌 Подключение к базе данных...")
        pool = await asyncpg.create_pool(DATABASE_URL)
        
        async with pool.acquire() as conn:
            # Создаем таблицу пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance INTEGER DEFAULT 0
                )
            ''')
            # Создаем таблицу заказов
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
            # Создаем админа (тебя) с деньгами
            await conn.execute('''
                INSERT INTO users (user_id, username, balance) 
                VALUES ($1, 'Admin', 999999) 
                ON CONFLICT (user_id) DO NOTHING
            ''', ADMIN_ID)
            
        print("✅ База данных успешно подключена!")
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА БД: {e}")

# --- КОМАНДА /START ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Guest"

    try:
        async with pool.acquire() as conn:
            # 1. Записываем юзера в базу (если новый)
            await conn.execute('''
                INSERT INTO users (user_id, username, balance) VALUES ($1, $2, 0)
                ON CONFLICT (user_id) DO UPDATE SET username = $2
            ''', user_id, username)
            
            # 2. Получаем баланс
            balance = await conn.fetchval('SELECT balance FROM users WHERE user_id = $1', user_id)
            
            # 3. Получаем последние заказы юзера
            rows = await conn.fetch('SELECT item_name, price, status FROM orders WHERE user_id = $1 ORDER BY id DESC LIMIT 10', user_id)
            my_orders = [{'item': r['item_name'], 'price': r['price'], 'status': r['status']} for r in rows]
            
            # 4. Если это ты (Админ) — получаем чужие заказы для обработки
            admin_orders = []
            if user_id == ADMIN_ID:
                rows_adm = await conn.fetch("SELECT id, username, item_name, user_id, price FROM orders WHERE status = 'wait'")
                admin_orders = [{'id': r['id'], 'user': r['username'], 'item': r['item_name'], 'uid': r['user_id'], 'price': r['price']} for r in rows_adm]

        # Собираем ссылку с данными
        data_payload = {
            'bal': balance,
            'admin': (user_id == ADMIN_ID),
            'manager': MANAGER_USERNAME,
            'orders': my_orders,
            'admin_orders': admin_orders
        }
        
        # Кодируем данные в URL
        encoded = urllib.parse.quote(json.dumps(data_payload))
        link = f"{GITHUB_URL}?data={encoded}"

        # Кнопки
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Открыть Магазин", web_app=WebAppInfo(url=link))]])
        await bot.set_chat_menu_button(chat_id=message.chat.id, menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=link)))
        
        await message.answer(f"👋 Привет, {message.from_user.first_name}!\n💰 Твой баланс: {balance} ₽", reply_markup=kb)

    except Exception as e:
        print(f"Ошибка в /start: {e}")
        await message.answer("⚠️ Бот перезагружается, подождите 5 секунд и нажмите /start снова.")

# --- ОБРАБОТКА ДЕЙСТВИЙ ИЗ МАГАЗИНА ---
@dp.message(F.web_app_data)
async def web_app_handler(message: types.Message):
    print(f"📥 ДАННЫЕ ОТ САЙТА: {message.web_app_data.data}")
    
    try:
        data = json.loads(message.web_app_data.data)
        action = data.get('action')
        user_id = message.from_user.id
        username = message.from_user.username or "Guest"

        async with pool.acquire() as conn:
            
            # === ПОКУПКА ===
            if action == 'buy':
                price = int(data['price'])
                item = data['item']
                
                # Используем транзакцию для безопасности денег
                async with conn.transaction():
                    balance = await conn.fetchval('SELECT balance FROM users WHERE user_id = $1', user_id)
                    if balance is None: balance = 0

                    if balance >= price:
                        new_bal = balance - price
                        # Списываем деньги
                        await conn.execute('UPDATE users SET balance = $1 WHERE user_id = $2', new_bal, user_id)
                        # Создаем заказ
                        await conn.execute('INSERT INTO orders (user_id, username, item_name, price) VALUES ($1, $2, $3, $4)', 
                                         user_id, username, item, price)
                        
                        await message.answer(f"✅ Успешно куплено: <b>{item}</b>\n💰 Остаток: {new_bal} ₽", parse_mode="HTML")
                        
                        # Уведомляем админа (тебя)
                        if user_id != ADMIN_ID:
                            try:
                                await bot.send_message(ADMIN_ID, f"🔔 <b>Новый заказ!</b>\nОт: @{username}\nТовар: {item}", parse_mode="HTML")
                            except: pass
                    else:
                        await message.answer(f"❌ Недостаточно средств! Ваш баланс: {balance} ₽")

            # === АДМИН: ВЫДАТЬ ДЕНЬГИ ===
            elif action == 'give_money':
                if user_id == ADMIN_ID:
                    target = int(data['target'])
                    amount = int(data['amount'])
                    
                    # Если юзера нет в базе, создаем его
                    exists = await conn.fetchval('SELECT 1 FROM users WHERE user_id = $1', target)
                    if not exists:
                        await conn.execute('INSERT INTO users (user_id, username, balance) VALUES ($1, $2, 0)', target, 'Unknown')
                    
                    await conn.execute('UPDATE users SET balance = balance + $1 WHERE user_id = $2', amount, target)
                    await message.answer(f"✅ Выдано {amount}₽ игроку {target}")
                    
                    try:
                        await bot.send_message(target, f"💰 Ваш баланс пополнен на {amount} ₽")
                    except: pass
                else:
                    await message.answer("❌ Вы не админ!")

            # === АДМИН: ВЫДАТЬ ЗАКАЗ ===
            elif action == 'order_done':
                if user_id == ADMIN_ID:
                    oid = int(data['order_id'])
                    target = int(data['target'])
                    await conn.execute("UPDATE orders SET status = 'done' WHERE id = $1", oid)
                    await message.answer(f"✅ Заказ #{oid} помечен выданным.")
                    try:
                        await bot.send_message(target, "✅ Ваш заказ выдан! Спасибо за покупку.")
                    except: pass

    except Exception as e:
        print(f"❌ ОШИБКА В HANDLER: {e}")
        await message.answer("Ошибка обработки данных.")

async def main():
    await init_db()
    # Запускаем веб-сервер (для Render) и бота параллельно
    await asyncio.gather(start_web_server(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())