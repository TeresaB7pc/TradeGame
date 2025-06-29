import asyncio
import aiosqlite
from contextlib import asynccontextmanager
from telegram.ext import ApplicationBuilder, MessageQueue, ContextTypes, CallbackContext
import logging

# تنظیمات اولیه
TOKEN = "YOUR_BOT_TOKEN"
DB_PATH = 'trade.db'
logging.basicConfig(level=logging.INFO)

# Connection Pooling
class AsyncConnectionPool:
    def __init__(self, max_connections=10):
        self.pool = []
        self.max_connections = max_connections
        
    async def acquire(self):
        if not self.pool:
            conn = await aiosqlite.connect(DB_PATH)
            self.pool.append(conn)
        return self.pool.pop()
    
    async def release(self, conn):
        self.pool.append(conn)
    
    async def close_all(self):
        for conn in self.pool:
            await conn.close()

DB_POOL = AsyncConnectionPool()

# تابع مدیریت session
@asynccontextmanager
async def db_session():
    conn = await DB_POOL.acquire()
    try:
        yield conn
    finally:
        await DB_POOL.release(conn)

# توابع اصلی
async def fetch_and_cache_prices():
    async with db_session() as conn:
        async with conn.execute("SELECT * FROM cached_prices") as cursor:
            return await cursor.fetchall()

async def add_user(user_id, username):
    async with db_session() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        await conn.commit()

# هندلرهای بات
async def start(update, context):
    user_id = update.effective_user.id
    username = update.effective_user.username
    await add_user(user_id, username)
    await update.message.reply_text("ثبت نام شما با موفقیت انجام شد!")

# تنظیمات اصلی
async def main():
    application = ApplicationBuilder() \
        .token(TOKEN) \
        .concurrent_updates(True) \
        .build()
        
    application.add_handler(CommandHandler("start", start))
    
    # ایجاد جداول اولیه
    async with db_session() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        ''')
        await conn.commit()
    
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
