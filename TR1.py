import logging
import asyncio
import sqlite3
import re  # برای اعتبارسنجی ورودی عددی
import telegram.error # برای مدیریت خطای BadRequest
import math
import datetime  # مطمئن شوید این ایمپورت در بالای فایل شما وجود دارد
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
import aiohttp

from collections import defaultdict

from datetime import timedelta  # این خط رو اضافه کنید

TOKEN = "7761784273:AAHMij_oEZl61UuGkjVpfKBpH26Geb05pGA"
ADMIN_IDS = [34447737, 6610020566, 7083388087]
# --- ثابت‌ها برای کمیسیون و مقادیر بازی ---
INITIAL_USER_BALANCE = 1000.0  # موجودی اولیه چیپ برای کاربران جدید
COMMISSION_RATE = 0.001  # 0.1% کارمزد برای هر تراکنش (خرید و فروش)
PRICE_CHANGE_THRESHOLD_PERCENT = 0.1  # 0.1% آستانه تغییر قیمت برای درخواست تایید مجدد
MIN_BUY_AMOUNT = 10.0  # حداقل مقدار چیپ برای خرید
MAX_BUY_AMOUNT_PERCENTAGE = 0.99  # حداکثر 99 درصد از موجودی قابل استفاده برای خرید
PRICE_CACHE_UPDATE_INTERVAL_SECONDS = 200  # فاصله زمانی برای بروزرسانی کش قیمت‌ها (200 ثانیه)
SELECTING_TRADE_TYPE = 0

# --- استیت‌های مکالمه ادمین ---
ADMIN_STATS  = range(5) # این خط باید با شروع استیت‌های ادمین شما هماهنگ باشد
MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD = 0.1  # دلار (مقدار حداقل پورتفو برای نمایش)

# حذف تعریف گلوبال conn و cursor در اینجا
# conn = sqlite3.connect('trade.db', check_same_thread=False) # حذف شد
# cursor = conn.cursor() # حذف شد

EPSILON = 0.00000001 # یا یک مقدار بسیار کوچک دیگر، برای مقایسه اعداد اعشاری

TOP_N_COINS = 100 # تعداد N ارز برتر
TOP_COINS_SYMBOLS = [] # لیست نمادهای ارزهای برتر
SYMBOL_TO_SLUG_MAP = {} # دیکشنری برای نگاشت نماد به اسلاگ Coingecko

# --- تعریف استیت‌های مکالمه اصلی (اعداد را بر اساس STATES فعلی خود تنظیم کنید) ---
CHOOSING_COIN = 0
ENTERING_AMOUNT = 1
CONFIRM_BUY = 2
RECONFIRM_BUY = 3
ASKING_TP_SL = 4  # حالت جدید برای پرسش TP/SL
ENTERING_TP_PRICE = 5  # حالت جدید برای دریافت قیمت TP
ENTERING_SL_PRICE = 6  # حالت جدید برای دریافت قیمت SL

CHOOSING_COIN_TO_SELL = 7
ENTERING_SELL_AMOUNT = 8
CONFIRM_SELL = 9
RECONFIRM_SELL = 10

CACHED_PRICES_MAP = {} # برای ذخیره قیمت‌های کش شده در حافظه

# --- سایر استیت‌های مربوط به پنل ادمین ---
ADMIN_PANEL = 200 # فرض می‌کنیم این قبلاً تعریف شده
ADMIN_MANAGE_BALANCE_USER_ID = 201 # برای دریافت User ID جهت مدیریت موجودی
ADMIN_MANAGE_BALANCE_AMOUNT = 202 # برای دریافت مقدار تغییر موجودی
ADMIN_BROADCAST_MESSAGE = 203 # برای دریافت پیام همگانی

# استیت‌های جدید برای مدیریت کاربران توسط ادمین
ADMIN_SELECT_USER = 204 # برای نمایش لیست کاربران و انتخاب توسط ادمین
ADMIN_SELECTED_USER_ACTIONS = 205 # برای منوی عملیات روی کاربر انتخاب شده
ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT = 206 # برای دریافت مقدار تغییر موجودی (برای افزودن/کسر)


# تعریف سطوح VIP
# کلیدها (مثلاً 1, 2, 3...) نشان‌دهنده سطح VIP هستند.
# مقادیر، دیکشنری‌هایی هستند که شامل 'threshold' (آستانه چیپ) و 'name' (نام سطح) می‌باشند.
VIP_LEVELS = {
    0: {'threshold': 0.0, 'name': "کاربر عادی"}, # سطح پیش‌فرض
    1: {'threshold': 500.0, 'name': "سطح 1 (نقره‌ای)"}, # حد سود/ضرر از اینجا فعال می‌شود.
    2: {'threshold': 5000.0, 'name': "سطح 2 (طلایی)"},
    3: {'threshold': 10000.0, 'name': "سطح 3 (پلاتینیوم)"},
    4: {'threshold': 20000.0, 'name': "سطح 4 (الماس)"},
    5: {'threshold': 50000.0, 'name': "سطح 5 (مگا چیپ)"} # بالاترین سطح
}

# برای دسترسی راحت‌تر به بالاترین سطح VIP موجود
MAX_VIP_LEVEL = max(VIP_LEVELS.keys())



def get_db_connection():
    # این تابع باید اتصال به دیتابیس sqlite را برگرداند
    # مثال:
    try:
        conn = sqlite3.connect('trade.db')
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to database: {e}")
        return None


async def fetch_and_cache_all_prices_internal(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """
    قیمت‌های کش شده را از دیتابیس 'cached_prices' واکشی کرده
    و آن‌ها را در CACHED_PRICES_MAP ذخیره می‌کند.
    """
    global CACHED_PRICES_MAP

    conn = None
    new_prices = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # فرض می‌کنیم جدول cached_prices دارای ستون‌های coin_slug و price است.
        cursor.execute("SELECT coin_slug, price FROM cached_prices")

        for row in cursor.fetchall():
            slug, price = row
            new_prices[slug] = price

        CACHED_PRICES_MAP = new_prices
        logging.info(f"Updated global CACHED_PRICES_MAP from database with {len(new_prices)} entries.")

        return CACHED_PRICES_MAP

    except Exception as e:
        logging.error(f"Error fetching cached prices from database: {e}", exc_info=True)
        return {}
    finally:
        if conn:
            conn.close()

async def post_init(application: Application) -> None:
    logging.info("Bot starting up. Performing initial data setup...")

    global top_coins, SYMBOL_TO_SLUG_MAP, CACHED_PRICES_MAP  # اینها همچنان گلوبال هستند

    try:
        logging.info("Fetching top coins and populating global variables...")

        # 1. فراخوانی fetch_top_coins که مقادیر را برمی‌گرداند.
        fetched_coins, fetched_symbol_to_slug_map = await fetch_top_coins()

        # 2. اختصاص مقادیر به متغیرهای گلوبال
        top_coins = fetched_coins
        SYMBOL_TO_SLUG_MAP = fetched_symbol_to_slug_map

        if not top_coins or not SYMBOL_TO_SLUG_MAP:
            logging.error("Failed to fetch top coins or SYMBOL_TO_SLUG_MAP. Bot cannot proceed without essential data.")
            await application.stop()
            return

        logging.info(f"Successfully fetched {len(top_coins)} top coins and populated global SYMBOL_TO_SLUG_MAP.")

        # **مهم:** اینجا داده‌های لازم برای fetch_and_cache_all_prices را در context.bot_data ذخیره می‌کنیم.
        # این همان 'top_coins_list_full_data' است که fetch_and_cache_all_prices انتظار دارد.
        application.bot_data['top_coins_list_full_data'] = top_coins
        application.bot_data[
            'symbol_to_slug_map'] = SYMBOL_TO_SLUG_MAP  # اگر SYMBOL_TO_SLUG_MAP هم در bot_data لازم است

        # === قدم 2: به روزرسانی coin_slugs در user_positions (این بخش با SYMBOL_TO_SLUG_MAP گلوبال کار می‌کند) ===
        logging.info("Starting one-off task: Updating missing coin_slugs in user_positions...")
        await update_missing_coin_slugs_in_user_positions()
        logging.info("Finished one-off task: Updating missing coin_slugs in user_positions.")

        # === قدم 3: اولین بار قیمت‌ها را به صورت مستقیم و بدون تأخیر کش می‌کنیم. ===
        logging.info("Performing initial fetch and caching of all coin prices directly.")

        # **مهم:** برای فراخوانی در post_init، باید یک context dummy بسازیم یا تابعی که application را می گیرد.
        # اما ساده ترین راه، همانطور که لاگ آخر شما نشان داد، فقط دادن application.bot_data به آن است
        # یا استفاده از یک context ساختگی.

        # راه حل پیشنهادی: ساخت یک ContextTypes.DEFAULT_TYPE ساده برای فراخوانی اولیه
        # (نیاز به ایمپورت ContextTypes)
        from telegram.ext import ContextTypes, Application

        class DummyContext:
            def __init__(self, bot_data):
                self.bot_data = bot_data

            # اگر fetch_and_cache_all_prices از self.job استفاده می‌کند، ممکن است نیاز باشد job را هم شبیه‌سازی کنید.
            # برای سادگی، فرض می‌کنیم فعلاً فقط bot_data لازم است.
            # اگر خطا گرفتید، باید بخش job را هم به این کلاس اضافه کنید.

        dummy_context = DummyContext(application.bot_data)
        await fetch_and_cache_all_prices(dummy_context)  # فراخوانی با context ساختگی

        logging.info("Initial price caching completed.")

        if not CACHED_PRICES_MAP:  # CACHED_PRICES_MAP باید توسط fetch_and_cache_all_prices پر شود.
            logging.error("CACHED_PRICES_MAP is empty after initial fetch. Price dependent features may fail.")
            await application.stop()
            return

        # === قدم 4: زمانبندی Jobها ===
        # اینها کاملاً صحیح هستند، چون context را به Job پاس می‌دهند.
        application.job_queue.run_repeating(
            fetch_and_cache_all_prices,
            interval=timedelta(seconds=200),
            first=timedelta(seconds=200),
            data=application  # این `data` در زمان اجرای Job به context.job.data تبدیل می‌شود
        )
        logging.info("Scheduled price caching job to run every 200 seconds, with first run in 200 seconds.")

        application.job_queue.run_repeating(
            update_cached_buy_data,
            interval=timedelta(seconds=60),
            first=timedelta(seconds=10),
            data=application
        )
        logging.info("Scheduled cached buy data update job to run every 60 seconds, starting in 10 seconds.")

        application.job_queue.run_repeating(
            monitor_tpsl_jobs,
            interval=timedelta(seconds=60),
            first=timedelta(seconds=20),
            data=application
        )
        logging.info("Scheduled TP/SL monitor job to run every 60 seconds, starting in 20 seconds.")

        logging.info("Bot startup tasks completed and jobs scheduled.")

    except Exception as e:
        logging.error(f"An unexpected error occurred during bot startup: {e}")
        await application.stop()
        logging.info("Bot stopped due to startup error.")


# این تابع را قبلاً ارائه داده‌ام، مطمئن شوید در کد شما هست و در دسترس است
# تابع برای دریافت پوزیشن‌های باز یک کاربر خاص
# فرض بر این است که ستون 'status' در جدول 'user_positions' وجود دارد
# و برای پوزیشن‌های باز مقدار آن 'open' (یا 'OPEN') است.
def get_user_positions_from_db(user_id: int):
    """
    پوزیشن‌های باز یک کاربر خاص را از دیتابیس بازیابی می‌کند.
    بر اساس ستون 'status' که مقدار 'open' دارد.
    :param user_id: ID عددی کاربر تلگرام.
    :return: لیستی از دیکشنری‌ها که هر کدام یک پوزیشن باز را نشان می‌دهند.
             اگر پوزیشن بازی یافت نشد، لیست خالی برمی‌گرداند.
    """
    conn = None
    positions = []
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row  # برای دسترسی به ستون‌ها با نام
        cursor = conn.cursor()

        # کوئری برای واکشی پوزیشن‌های باز بر اساس ستون 'status'
        # فرض بر این است که مقدار 'status' برای پوزیشن‌های باز 'open' است.
        # اگر مقدار دیگری (مثلاً 'OPEN' با حروف بزرگ) دارید، آن را اصلاح کنید.
        cursor.execute("""
            SELECT 
                position_id, user_id, symbol, amount, buy_price, open_timestamp,
                coin_slug, tp_price, sl_price, closed_price, status -- <<<< اضافه کردن status به SELECT
            FROM user_positions
            WHERE user_id = ? AND status = 'open' -- <<<< تغییر اینجا!
            ORDER BY symbol
        """, (user_id,))

        for row in cursor.fetchall():
            positions.append(dict(row))
            # می‌توانید برای دیباگ، لاگ زیر را فعال کنید تا ببینید چه پوزیشن‌هایی واکشی می‌شوند.
            # logging.info(f"Fetched open position for user {user_id}: {dict(row)}")
    except Exception as e:
        logging.error(f"Error fetching user positions for user {user_id} using 'status' column: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
    return positions

def get_all_users_data():
    """
    اطلاعات کاربری شامل username, user_commission_balance (موجودی اصلی), total_realized_pnl
    و bot_commission_balance (کمیسیون پرداختی به ربات) را از دیتابیس بازیابی می‌کند.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    users_data = []
    try:
        cursor.execute("""
            SELECT username, balance, total_realized_pnl, bot_commission_balance, user_id
            FROM users
            WHERE user_id != 0 -- کاربر ربات را مستثنی می‌کند
            ORDER BY username ASC
        """)
        rows = cursor.fetchall()
        for row in rows:
            users_data.append({
                "username": row[0] if row[0] else f"User {row[4]}", # اگر username نبود، User ID را نمایش دهد
                "balance": row[1],
                "pnl": row[2],
                "commission_paid": row[3],
                "user_id": row[4]
            })
    except Exception as e:
        logging.error(f"Error fetching all users data: {e}")
    finally:
        conn.close()
    return users_data

def get_user_info_by_id(user_id):
    """
    اطلاعات یک کاربر خاص را بر اساس user_id از دیتابیس بازیابی می‌کند.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    user_data = None
    try:
        cursor.execute("""
            SELECT username, balance, total_realized_pnl, bot_commission_balance
            FROM users
            WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if row:
            user_data = {
                "username": row[0] if row[0] else f"User {user_id}",
                "balance": row[1],
                "total_realized_pnl": row[2],
                "bot_commission_balance": row[3]
            }
    except Exception as e:
        logging.error(f"Error fetching user info for user_id {user_id}: {e}")
    finally:
        conn.close()
    return user_data

async def invite_friends_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    referral_link = f"https://t.me/YourBotUsername?start=ref_{user_id}" # نام کاربری ربات خود را جایگزین کنید

    # بررسی کنید که آیا آپدیت از یک پیام (دستور) است یا از یک کال‌بک کوئری (دکمه)
    if update.message:
        # اگر از پیام است (مثلاً /invitefriends)
        await update.message.reply_text(
            f"برای دعوت دوستان خود، می‌توانید این لینک را با آنها به اشتراک بگذارید:\n{referral_link}\n\n"
            "وقتی دوست شما با این لینک وارد ربات شود، شما پاداش دریافت خواهید کرد.",
            reply_markup=get_main_menu_keyboard()
        )
    elif update.callback_query:
        # اگر از دکمه است
        query = update.callback_query
        await query.answer() # پاسخ به کال‌بک کوئری
        await query.message.reply_text( # استفاده از query.message
            f"برای دعوت دوستان خود، می‌توانید این لینک را با آنها به اشتراک بگذارید:\n{referral_link}\n\n"
            "وقتی دوست شما با این لینک وارد ربات شود، شما پاداش دریافت خواهید کرد.",
            reply_markup=get_main_menu_keyboard()
        )

    # اگر این تابع در یک ConversationHandler نیست، نیازی به return ConversationHandler.END نیست.



def parse_date_robustly(date_string):
    """
    Parses a date string, attempting to handle different formats including microseconds.
    """
    if not date_string:
        return None

    formats_to_try = [
        '%Y-%m-%d %H:%M:%S.%f',  # With microseconds
        '%Y-%m-%d %H:%M:%S',    # Without microseconds
    ]
    for fmt in formats_to_try:
        try:
            return datetime.datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    logging.warning(f"Failed to parse date string with known formats: {date_string}")
    return None

# --- End of Helper Function ---


def format_price(price):
    if price is None:
        return "نامعلوم"

    # اگر قیمت خیلی کوچیک باشه، از تعداد اعشار بیشتری استفاده کن
    if price < 0.0001:  # مثلاً کمتر از 0.0001
        # برای مثال، 10 رقم اعشار برای قیمت‌های خیلی کوچک
        return f"{price:.10f}"
    elif price < 1.0: # برای قیمت‌های بین 0.0001 و 1
        # مثلاً 6 رقم اعشار
        return f"{price:.6f}"
    else: # برای قیمت‌های بالاتر از 1
        # مثلاً 2 رقم اعشار
        return f"{price:.2f}"

def get_user(user_id):
    """
    اطلاعات کامل کاربر را از دیتابیس دریافت می‌کند.
    اگر کاربر وجود نداشت، آن را با مقادیر پیش‌فرض کامل ایجاد می‌کند.
    همچنین PnL ماهانه کاربر را در صورت شروع ماه جدید ریست می‌کند.
    """
    try:
        cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        user_data_tuple = cursor.fetchone()

        if user_data_tuple is None:
            logging.info(f"کاربر {user_id} یافت نشد. در حال اضافه کردن کاربر جدید با مقادیر پیش‌فرض.")
            # تغییر: برای کاربران جدید هم از datetime.datetime.now().isoformat() استفاده می‌کنیم
            # تا فرمت تاریخ همیشه شامل زمان و میکروثانیه باشد و یکپارچگی حفظ شود.
            today_iso = datetime.datetime.now().isoformat()

            cursor.execute("""
                           INSERT INTO users(user_id, username, first_name, balance, user_commission_balance,
                                             total_realized_pnl, monthly_realized_pnl, last_monthly_reset_date,
                                             bot_commission_balance)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (user_id, None, None, INITIAL_USER_BALANCE, 0.0, 0.0, 0.0, today_iso, 0.0)
                           )
            conn.commit()
            logging.info(f"کاربر {user_id} با موفقیت به دیتابیس اضافه شد.")

            cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            user_data_tuple = cursor.fetchone()

        column_names = [description[0] for description in cursor.description]
        user_dict = dict(zip(column_names, user_data_tuple))

        # --- منطق ریست PnL ماهانه ---
        today = datetime.date.today()
        last_reset_date_str = user_dict.get("last_monthly_reset_date")

        if last_reset_date_str:
            # اینجا از parse_date_robustly استفاده می‌کنیم
            parsed_dt_object = parse_date_robustly(last_reset_date_str)

            if parsed_dt_object:
                last_reset_date = parsed_dt_object.date() # فقط قسمت تاریخ را استخراج می‌کنیم
            else:
                # اگر parse_date_robustly هم نتوانست تاریخ را پردازش کند، لاگ اخطار خودمان را چاپ می‌کنیم
                logging.warning(
                    f"فرمت تاریخ نامعتبر برای last_monthly_reset_date برای کاربر {user_id}: {last_reset_date_str}. به عنوان ریست اولیه در نظر گرفته می‌شود.")
                last_reset_date = None # این باعث می‌شود که شرط پایین‌تر فعال شود

            if last_reset_date and (today.month != last_reset_date.month or today.year != last_reset_date.year):
                logging.info(
                    f"در حال ریست PnL ماهانه برای کاربر {user_id}. PnL قبلی: {user_dict['monthly_realized_pnl']:.2f} چیپ")
                user_dict['monthly_realized_pnl'] = 0.0  # ریست به صفر
                # تغییر: هنگام به‌روزرسانی هم از فرمت کامل با زمان استفاده می‌کنیم
                user_dict['last_monthly_reset_date'] = datetime.datetime.now().isoformat()

                cursor.execute("""
                               UPDATE users
                               SET monthly_realized_pnl    = ?,
                                   last_monthly_reset_date = ?
                               WHERE user_id = ?
                               """, (user_dict['monthly_realized_pnl'], user_dict['last_monthly_reset_date'], user_id))
                conn.commit()
                logging.info(f"PnL ماهانه برای کاربر {user_id} با موفقیت ریست شد.")
        else:
            # اگر last_monthly_reset_date هنوز NULL است (مثلاً برای کاربران خیلی قدیمی قبل از اضافه شدن این ستون)
            # آن را به تاریخ امروز (با زمان) تنظیم می‌کنیم تا از این به بعد منطق ریست کار کند.
            user_dict['last_monthly_reset_date'] = datetime.datetime.now().isoformat()
            cursor.execute("UPDATE users SET last_monthly_reset_date = ? WHERE user_id = ?",
                           (user_dict['last_monthly_reset_date'], user_id))
            conn.commit()
            logging.info(f"تنظیم تاریخ ریست اولیه last_monthly_reset_date برای کاربر {user_id}.")

        return user_dict
    except Exception as e:
        logging.error(f"خطا در تابع get_user برای کاربر {user_id}: {e}")
        return None

def update_balance(user_id, new_balance):
    cursor.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, user_id))
    conn.commit()


def add_bot_commission(amount):
    # user_id 0 برای ذخیره کارمزد ربات استفاده می شود
    cursor.execute("UPDATE users SET bot_commission_balance = bot_commission_balance + ? WHERE user_id = ?",
                   (amount, 0))
    conn.commit()
def add_user_commission(user_id, amount):
    """
    این تابع مقدار کمیسیون پرداخت شده توسط یک کاربر خاص را به ستون
    user_commission_balance (یا هر نامی که برای آن انتخاب کردی) اضافه می‌کند.

    Args:
        user_id (int): شناسه کاربری که کمیسیون را پرداخت کرده است.
        amount (float): مقدار کمیسیونی که باید به موجودی کمیسیون کاربر اضافه شود.
    """
    # فرض می‌کنیم ستونی به نام 'user_commission_balance' در جدول users برای هر کاربر وجود دارد
    # یا 'user_commission_earned' اگر قبلاً از آن استفاده کردی
    try:
        cursor.execute("UPDATE users SET user_commission_balance = user_commission_balance + ? WHERE user_id = ?",
                       (amount, user_id))
        conn.commit()
        logging.info(f"Added {amount:.2f} commission to user {user_id}'s balance.")
    except sqlite3.Error as e:
        logging.error(f"Error adding commission to user {user_id}: {e}")
        # اینجا می‌تونی مدیریت خطای مناسب‌تری انجام بدی، مثلاً پیام به ادمین یا تلاش مجدد

def get_bot_commission_balance():
    cursor.execute("SELECT bot_commission_balance FROM users WHERE user_id = ?", (0,))
    res = cursor.fetchone()
    if res == None:
        # اگر کاربر 0 (ربات) وجود نداشت، ایجادش می کنیم.
        cursor.execute("INSERT INTO users(user_id, balance, bot_commission_balance) VALUES (?, 0.0, 0.0)", (0,))
        conn.commit()
        return 0.0
    return res[0]

# مثال: تابعی برای دریافت کمیسیون کاربر
def get_user_commission_balance(user_id):
    cursor.execute("SELECT user_commission_balance FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if res is None:
        # اگر کاربر هنوز در دیتابیس نیست (اگر قبلاً در start command ایجاد نشده)
        # باید اینجا مدیریت بشه یا فرض کنید در start command ایجاد میشه.
        # برای مثال، اگر ستون user_commission_earned رو اضافه کردی
        return 0.0 # یا یک مقدار پیش‌فرض دیگر
    return res[0]

# مثال: تابعی برای اضافه کردن کمیسیون به موجودی کاربر
def add_commission_to_user(user_id, commission_amount):
    cursor.execute("UPDATE users SET user_commission_earned = user_commission_earned + ? WHERE user_id = ?",
                   (commission_amount, user_id))
    conn.commit()

# تابع جدید: ثبت پوزیشن خرید
def save_buy_position(user_id, username, symbol, amount, buy_price, commission_paid, coin_slug):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO user_positions (user_id, username, symbol, amount, buy_price, open_timestamp, status, commission_paid, coin_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, symbol, amount, buy_price, datetime.datetime.now(), 'open', commission_paid, coin_slug)
        )
        conn.commit()
        logging.info(f"Position saved for user {user_id} ({username}).")
        return cursor.lastrowid
    except Exception as e:
        logging.error(f"Error saving buy position for user {user_id} ({username}): {e}")
        return None
    finally:
        conn.close()

def get_user_available_balance(user_id):
    base_balance = get_user(user_id)["balance"]
    # در حال حاضر هیچ موجودی قفل شده‌ای برای معاملات قدیمی (حدس بالا/پایین) نداریم
    return base_balance

# در TR1.py
# تابع add_user_if_not_exists شما (با تغییرات)
def add_user_if_not_exists(user_id, chat_id, username, first_name, referrer_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user_data = cursor.fetchone()

    if user_data:
        # کاربر از قبل وجود دارد، فقط اطلاعات را بروزرسانی کنید
        # مطمئن شوید که referrer_id فقط یک بار و هنگام ثبت نام اولیه تنظیم می‌شود
        # اگر referrer_id فعلی NULL است و یک referrer_id جدید داریم، آن را بروزرسانی کنید
        if user_data[11] is None and referrer_id is not None:  # فرض می‌کنیم referrer_id ستون ۱۱ است
            cursor.execute('''
                           UPDATE users
                           SET username    = ?,
                               first_name  = ?,
                               chat_id     = ?,
                               referrer_id = ?
                           WHERE user_id = ?
                           ''', (username, first_name, chat_id, referrer_id, user_id))
            logging.info(f"User {user_id} updated with new referrer_id: {referrer_id}")
        else:
            # فقط اطلاعات عادی را بروزرسانی کنید
            cursor.execute('''
                           UPDATE users
                           SET username   = ?,
                               first_name = ?,
                               chat_id    = ?
                           WHERE user_id = ?
                           ''', (username, first_name, chat_id, user_id))

        conn.commit()
        conn.close()
        return False  # کاربر جدید نیست

    else:
        # کاربر جدید است، او را اضافه کنید و پاداش اولیه را بدهید
        logging.info(f"Adding new user: {user_id}, referrer_id: {referrer_id}")
        initial_balance = INITIAL_USER_BALANCE
        if referrer_id:
            initial_balance += 100  # پاداش 100 چیپ برای کاربر دعوت شده

        cursor.execute("""
                       INSERT INTO users (user_id, balance, bot_commission_balance, user_commission_balance,
                                          total_realized_pnl, monthly_realized_pnl, last_monthly_reset_date,
                                          vip_level, chat_id, username, first_name, referrer_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       """, (user_id, initial_balance, 0.0, 0.0, 0.0, 0.0,
                             datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 0,
                             chat_id, username, first_name, referrer_id))
        conn.commit()
        conn.close()
        logging.info(f"New user {user_id} added with balance {initial_balance} and referrer_id {referrer_id}.")
        return True  # کاربر جدید است


# تابع update_user_balance
def update_user_balance(user_id, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error updating balance for user {user_id}: {e}")
        return False
    finally:
        # مطمئن شوید که اتصال در هر صورت بسته شود
        # اگر تابع get_db_connection اتصالات را در یک Pool مدیریت می کند،
        # ممکن است این close لازم نباشد و حتی مشکل ایجاد کند.
        # اما با روش فعلی که هر بار یک اتصال جدید می گیریم، لازم است.
        conn.close()

# تابع جدید: دریافت پوزیشن‌های باز گروه‌بندی شده و میانگین‌گیری شده
def get_open_positions_grouped(user_id):
    """
    Fetches open positions for a user, groups them by symbol,
    and calculates the weighted average buy price for each symbol.
    Returns a list of dictionaries, each representing a grouped position.
    """
    cursor.execute("SELECT symbol, amount, buy_price FROM user_positions WHERE user_id=? AND status='open'", (user_id,))
    raw_positions = cursor.fetchall()

    grouped_positions = {}
    for symbol, amount, buy_price in raw_positions:
        if symbol not in grouped_positions:
            grouped_positions[symbol] = {'total_amount': 0.0, 'total_cost': 0.0, 'symbol': symbol}

        grouped_positions[symbol]['total_amount'] += amount
        grouped_positions[symbol]['total_cost'] += (amount * buy_price)

    result = []
    for symbol, data in grouped_positions.items():
        if data['total_amount'] > 0:
            average_buy_price = data['total_cost'] / data['total_amount']
            result.append({
                'symbol': symbol,
                'amount': data['total_amount'],
                'buy_price': average_buy_price
            })
    return result


# --- Global Data Storage and Initialization ---
top_coins = []

# لیست‌های فیلتر (بدون تغییر)
STABLE_COINS_SYMBOLS = {
    'usdt', 'usdc', 'busd', 'dai', 'ust', 'usdp', 'frax', 'tusd', 'gusd', 'paxg', 'eurs',
    'usds', 'usde', 'susd', 'usdtb', 'usdt0', 'pyusd', 'usdc.e', 'usd1', 'fdusd', 'susds'
}
STABLE_COINS_NAMES = {
    'tether', 'usd coin', 'binance usd', 'dai', 'terrausd', 'pax dollar', 'frax', 'trueusd',
    'gemini dollar', 'pax gold', 'euro stablecoin', 'decentralized usd', 'binance stable coin',
    'e-money usd', 'synth sUSD', 'tether gold', 'tether btc', 'paypal usd', 'usd coin bridged',
    'sUSD', 'first digital usd', 'synthetix usd'
}
WRAPPED_TOKENS_SYMBOLS = {
    'wbtc', 'weth', 'wsol', 'wbnb', 'wavax', 'maticx', 'steth', 'cbeth', 'reth', 'lido steth',
    'xbt', 'wrapped'
}
WRAPPED_TOKENS_NAMES_KEYWORDS = {
    'wrapped', 'staked', 'liquid staked', 'ether token', 'bitcoin token', 'wrapped version', 'bridged',
    'tokenized', 'peg'
}
# لیست نمادهای سفارشی برای فیلتر
CUSTOM_FILTER_SYMBOLS = {
    'solve',
    'solvebtc'
}


async def fetch_top_coins():
    """
    Fetches top 100 coins from Coingecko API, filtering out stablecoins and wrapped tokens.
    Returns a tuple: (list of filtered top coins, dictionary of symbol to slug map).
    Returns ([], {}) on error.
    """
    logging.info("Fetching top coins from Coingecko API...")
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        'vs_currency': 'usd',
        'order': 'market_cap_desc',
        'per_page': 100,
        'page': 1,
        'sparkline': 'false'
    }

    # متغیرهای محلی برای نگهداری نتایج
    local_top_coins = []
    local_symbol_to_slug_map = {}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status() # این خط خطا را در صورت HTTP Bad Status پرتاب می کند
                data = await resp.json()

                for coin in data:
                    coin_symbol_upper = coin.get('symbol', '').upper()
                    coin_name_lower = coin.get('name', '').lower()
                    coin_id = coin.get('id', '').lower()  # این همون slug هست

                    # Skip if symbol or id is empty
                    if not coin_symbol_upper or not coin_id:
                        logging.warning(f"Skipping coin with empty symbol or id: {coin.get('name')}")
                        continue

                    # Check for stablecoins
                    # استفاده از متغیرهای از پیش تعریف شده برای لیست‌های فیلترینگ مهم است
                    if (coin_symbol_upper in STABLE_COINS_SYMBOLS or
                            coin_name_lower in STABLE_COINS_NAMES or
                            ('usd' in coin_symbol_upper.lower() and coin_symbol_upper.lower() != 'usd' and len(
                                coin_symbol_upper) > 1)):
                        logging.info(f"Filtering out stablecoin: {coin.get('name')} ({coin_symbol_upper})")
                        continue

                    # Check for wrapped tokens
                    if (coin_symbol_upper in WRAPPED_TOKENS_SYMBOLS or
                            any(keyword in coin_name_lower for keyword in WRAPPED_TOKENS_NAMES_KEYWORDS) or
                            (coin_symbol_upper.startswith('W') and len(coin_symbol_upper) > 1 and coin_symbol_upper[
                                                                                                  1:].isalnum())):
                        logging.info(f"Filtering out wrapped token: {coin.get('name')} ({coin_symbol_upper})")
                        continue

                    # Check for custom filtered symbols
                    if coin_symbol_upper in CUSTOM_FILTER_SYMBOLS:
                        logging.info(f"Filtering out custom symbol: {coin.get('name')} ({coin_symbol_upper})")
                        continue

                    # اگر کوین فیلتر نشد، به لیست اضافه می‌کنیم و local_symbol_to_slug_map را پر می‌کنیم
                    local_top_coins.append({
                        'id': coin_id,  # این همان slug است
                        'name': coin.get('name'),
                        'symbol': coin_symbol_upper,
                        'image': coin.get('image')
                    })

                    local_symbol_to_slug_map[coin_symbol_upper] = coin_id

                logging.info(f"Fetched {len(local_top_coins)} non-stable/non-wrapped top coins.")
                logging.info(f"Filtered top coins list: {[c['symbol'] for c in local_top_coins]}")
                logging.debug(f"SYMBOL_TO_SLUG_MAP populated with {len(local_symbol_to_slug_map)} entries.")

                # مقادیر را return می‌کنیم
                return local_top_coins, local_symbol_to_slug_map

        except aiohttp.ClientError as e:
            logging.error(f"HTTP error fetching top coins: {e}")
            return [], {} # **مهم: در صورت خطای ClientError هم باید مقادیر خالی برگردانید**
        except Exception as e:
            logging.error(f"An unexpected error occurred during fetch_top_coins: {e}")
            return [], {} # **مهم: در صورت خطای عمومی هم باید مقادیر خالی برگردانید**

        # این خط return [] , {} دیگر نیازی نیست، چون بلوک های except آن را پوشش می‌دهند.
        # می‌توانید آن را حذف کنید.
        # return [], {}

async def get_price_from_cache(coin_slug: str) -> float | None: # <--- async حذف شد، Type Hint اضافه شد
    """Retrieves a single coin price from the cache."""
    try:
        cursor.execute("SELECT price FROM cached_prices WHERE coin_slug=?", (coin_slug,)) # <--- تغییر به coin_slug
        res = cursor.fetchone()
        if res:
            logging.debug(f"Price for {coin_slug} found in cache: ${res[0]:.8f}")
            return res[0]
        logging.warning(f"Price for {coin_slug} not found in cache. Returning 0.") # پیام هشدار بهتر
        return 0 # یا None اگر ترجیح می‌دهید عدم یافتن با None نشان داده شود
    except Exception as e:
        logging.error(f"Error fetching price for {coin_slug} from cache: {e}")
        return 0 # در صورت خطا هم 0 برگردانده شود یا None


async def get_user_total_portfolio_value(user_id: int) -> float:
    """
    مجموع ارزش پورتفوی کاربر (موجودی نقدی + ارزش دلاری پوزیشن‌های باز) را محاسبه می‌کند.
    این تابع هیچ پیامی ارسال نمی‌کند و فقط مقدار عددی را برمی‌گرداند.
    """
    total_value = 0.0
    try:
        # 1. دریافت موجودی نقدی کاربر
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        user_balance_data = cursor.fetchone()
        if user_balance_data:
            total_value += user_balance_data[0]
        else:
            logging.warning(f"User {user_id} not found when calculating portfolio value. Returning 0.")
            return 0.0 # اگر کاربر پیدا نشد، 0 برمی‌گرداند

        # 2. دریافت پوزیشن‌های باز کاربر
        # مطمئن بشید که ستون 'coin_slug' در user_positions وجود داره.
        cursor.execute("SELECT symbol, amount, coin_slug FROM user_positions WHERE user_id = ? AND status = 'open'", (user_id,))
        open_positions = cursor.fetchall()

        for symbol, amount, coin_slug in open_positions:
            current_price = await get_price_from_cache(coin_slug)
            if current_price > 0: # اگر قیمت معتبر بود
                total_value += (amount * current_price)
            else:
                logging.warning(f"Could not get current price for {symbol} ({coin_slug}) for user {user_id}'s portfolio calculation. Skipping this position.")
                # اگر قیمت لحظه‌ای رو نتونستیم بگیریم، این پوزیشن رو از محاسبات ارزش کلی حذف می‌کنیم.

    except Exception as e:
        logging.error(f"Error calculating total portfolio value for user {user_id}: {e}")
        return 0.0 # در صورت خطا، 0 برمی‌گرداند

    return total_value



# ... (تعریف VIP_LEVELS) ...
# ... (تابع get_user_total_portfolio_value) ...

async def check_and_upgrade_vip_level(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    سطح VIP کاربر را بر اساس ارزش کلی پورتفوی بررسی و در صورت نیاز ارتقاء می‌دهد.
    وقتی یک سطح VIP آنلاک شد، حتی اگر ارزش پورتفوی کاهش پیدا کند، کاربر آن سطح را حفظ می‌کند.
    پیام تبریک VIP را به کاربر ارسال می‌کند.
    """
    try:
        cursor.execute("SELECT vip_level, chat_id FROM users WHERE user_id = ?", (user_id,))
        user_data = cursor.fetchone()
        if not user_data:
            logging.error(f"User {user_id} not found in DB during VIP check.")
            return

        current_vip_level = user_data[0]
        user_chat_id = user_data[1]

        # محاسبه ارزش کلی پورتفوی کاربر با تابع جدید
        total_portfolio_value = await get_user_total_portfolio_value(user_id)

        new_potential_vip_level = current_vip_level  # شروع با سطح فعلی کاربر

        # پیدا کردن بالاترین سطح VIP که کاربر با پورتفوی فعلی واجد شرایط آن است
        # حلقه از بالاترین سطح به پایین‌ترین سطح مرتب شده تا بالاترین threshold مطابق پیدا شود
        for level, data in sorted(VIP_LEVELS.items(), key=lambda item: item[0], reverse=True):
            if total_portfolio_value >= data['threshold']:
                new_potential_vip_level = level
                break  # بالاترین سطحی که واجد شرایطشه رو پیدا کردیم، دیگه نیازی نیست ادامه بدیم

        # بررسی نهایی: آیا کاربر به سطح بالاتری از سطح فعلیش رسیده و این سطح جدید نیست؟
        if new_potential_vip_level > current_vip_level:
            # کاربر ارتقاء یافته!
            cursor.execute("UPDATE users SET vip_level = ? WHERE user_id = ?", (new_potential_vip_level, user_id))
            conn.commit()
            logging.info(f"User {user_id} upgraded from VIP level {current_vip_level} to {new_potential_vip_level}.")

            old_vip_name = VIP_LEVELS.get(current_vip_level, {}).get('name', 'ناشناس')
            new_vip_name = VIP_LEVELS.get(new_potential_vip_level, {}).get('name', 'ناشناس')

            upgrade_message = (
                f"🥳 **تبریک می‌گوییم!** شما به سطح VIP جدید ارتقاء یافتید!\n\n"
                f"از **{old_vip_name}** به **{new_vip_name}** ارتقاء پیدا کردید.\n"
                f"به عنوان کاربر **{new_vip_name}**، شما اکنون از مزایای ویژه‌ای برخوردار هستید (مثلاً دسترسی به قابلیت حد سود/ضرر، تخفیف در کارمزد و ...).\n\n"
                "به معامله‌گری ادامه دهید تا به سطوح بالاتر دست یابید!"
            )

            if user_chat_id:
                try:
                    await context.bot.send_message(
                        chat_id=user_chat_id,
                        text=upgrade_message,
                        parse_mode='Markdown'
                    )
                except telegram.error.BadRequest as e:
                    logging.error(
                        f"Failed to send VIP upgrade message to user {user_id} (chat_id: {user_chat_id}): {e}")
                except Exception as e:
                    logging.error(f"Unexpected error sending VIP upgrade message to user {user_id}: {e}")
            else:
                logging.warning(f"Could not send VIP upgrade message for user {user_id} as chat_id is missing.")

    except Exception as e:
        logging.error(f"Error in check_and_upgrade_vip_level for user {user_id}: {e}")

def get_total_profit_loss(user_id):
    """Calculates the total profit/loss for all closed positions for a user since inception."""
    cursor.execute(
        "SELECT SUM(profit_loss) FROM user_positions WHERE user_id = ? AND status = 'closed'",
        (user_id,)
    )
    result = cursor.fetchone()[0]
    return result if result is not None else 0.0



# مطمئن شوید این متغیر گلوبال در بالای فایل TR1.py تعریف شده است:
# CACHED_PRICES_MAP = {}

async def fetch_and_cache_all_prices(context: ContextTypes.DEFAULT_TYPE):
    """
    Fetches prices for all top_coins (from bot_data) and caches them in the database AND global CACHED_PRICES_MAP.
    """
    global CACHED_PRICES_MAP # این خط بسیار مهم است

    logging.info("Running price caching job.")

    top_coins_for_pricing = context.bot_data.get('top_coins_list_full_data')
    # SYMBOL_TO_SLUG_MAP = context.bot_data.get('symbol_to_slug_map') # اگر لازم است، از اینجا بگیرید

    if not top_coins_for_pricing:
        logging.warning("No top coins found in bot_data to fetch prices for. Skipping caching.")
        return

    coin_ids = [coin['id'] for coin in top_coins_for_pricing]

    chunk_size = 200
    current_prices_from_api = {} # تغییر نام برای جلوگیری از تداخل با متغیر نهایی
    for i in range(0, len(coin_ids), chunk_size):
        chunk_ids = coin_ids[i:i + chunk_size]
        joined_ids = ','.join(chunk_ids)
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={joined_ids}&vs_currencies=usd"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    # ذخیره قیمت‌ها بر اساس slug (که اینجا همان id کوین است)
                    current_prices_from_api.update({slug: data.get(slug, {}).get("usd", 0) for slug in chunk_ids})
            except aiohttp.ClientError as e:
                logging.error(f"Error fetching batch prices for chunk {i}: {e}")
            except Exception as e:
                logging.error(f"An unexpected error occurred during fetch_and_cache_all_prices for chunk {i}: {e}")

    if not current_prices_from_api:
        logging.warning("No prices fetched for caching.")
        return

    # **مهم:** اینجا CACHED_PRICES_MAP گلوبال را آپدیت می‌کنیم.
    CACHED_PRICES_MAP = current_prices_from_api
    logging.info(f"Updated global CACHED_PRICES_MAP with {len(CACHED_PRICES_MAP)} entries.")


    now = datetime.datetime.now()
    updated_count = 0

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("BEGIN TRANSACTION")

        for coin_data in top_coins_for_pricing:
            coin_slug = coin_data['id']
            # از قیمت‌های تازه واکشی شده استفاده می‌کنیم که در current_prices_from_api هستند.
            price = current_prices_from_api.get(coin_slug)

            if price is not None and price > 0:
                cursor.execute(
                    "INSERT OR REPLACE INTO cached_prices (coin_slug, price, last_updated) VALUES (?, ?, ?)",
                    (coin_slug, price, now.isoformat())
                )
                updated_count += 1
        conn.commit()
        logging.info(f"Cached prices in database for {updated_count} coins at {now.isoformat()}.")
    except Exception as e:
        logging.error(f"Error updating cached_prices in database: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

# get_prices_for_portfolio_from_cache قبلاً درست شده، اما در اینجا دوباره قرار می‌گیرد برای تکمیل بودن
async def get_prices_for_portfolio_from_cache(coin_slugs):
    """Retrieves multiple coin prices from the cache."""
    # این تابع می‌تواند به جای دیتابیس، مستقیماً از CACHED_PRICES_MAP گلوبال استفاده کند
    # که سرعت بیشتری خواهد داشت و همیشه شامل آخرین قیمت‌های کش شده در حافظه است.
    if not coin_slugs:
        return {}

    prices = {}
    for slug in coin_slugs:
        price = CACHED_PRICES_MAP.get(slug) # از متغیر گلوبال استفاده می‌کنیم
        if price is not None and price > 0:
            prices[slug] = price
        else:
            logging.warning(f"Price for {slug} not found or zero in global cache for portfolio.")
            prices[slug] = 0 # اطمینان از مقداردهی صفر برای قیمت‌های گم شده

    logging.debug(f"Portfolio prices fetched from global cache for {len(prices)} coins.")
    return prices

    # اگر همچنان اصرار دارید از دیتابیس بخوانید:
    # slugs_tuple = tuple(coin_slugs)
    # placeholders = ','.join(['?' for _ in slugs_tuple])
    # prices = {}
    # conn = None
    # try:
    #     conn = get_db_connection()
    #     cursor = conn.cursor()
    #     cursor.execute(f"SELECT coin_slug, price FROM cached_prices WHERE coin_slug IN ({placeholders})", slugs_tuple)
    #     results = cursor.fetchall()
    #     prices = {row[0]: row[1] for row in results}
    #     for slug in coin_slugs:
    #         if slug not in prices or prices[slug] == 0:
    #             logging.warning(f"Price for {slug} not found or zero in DB cache for portfolio.")
    #             prices[slug] = 0
    # except Exception as e:
    #     logging.error(f"Error fetching prices from DB cache for portfolio: {e}")
    # finally:
    #     if conn:
    #         conn.close()
    # return prices

async def get_prices_for_portfolio_from_cache(coin_slugs):
    """Retrieves multiple coin prices from the cache."""
    if not coin_slugs:
        return {}

    slugs_tuple = tuple(coin_slugs)
    placeholders = ','.join(['?' for _ in slugs_tuple])
    prices = {}
    conn = None
    try:
        conn = get_db_connection()  # فرض می‌کنیم get_db_connection() را تعریف کرده‌اید
        cursor = conn.cursor()

        cursor.execute(f"SELECT coin_slug, price FROM cached_prices WHERE coin_slug IN ({placeholders})", slugs_tuple)

        results = cursor.fetchall()
        prices = {row[0]: row[1] for row in results}

        # Check if all requested slugs were found in cache
        for slug in coin_slugs:
            if slug not in prices or prices[slug] == 0:
                logging.warning(f"Price for {slug} not found or zero in cache for portfolio.")
                prices[slug] = 0  # Ensure missing prices are represented as 0

        logging.debug(f"Portfolio prices fetched from cache for {len(prices)} coins.")

    except Exception as e:
        logging.error(f"Error fetching prices from cache for portfolio: {e}")
    finally:
        if conn:
            conn.close()

    return prices

# --- Inline Keyboard Builders ---
def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 موجودی و پورتفو", callback_data='show_balance_portfolio')],
        [InlineKeyboardButton("⚡ شروع معامله", callback_data='start_trade')],
        [InlineKeyboardButton("📈 فروش پورتفو", callback_data='sell_portfolio_entry')],  # NEW: Sell Portfolio Button
        [InlineKeyboardButton("ℹ️ درباره ربات", callback_data='about_bot')],
        [InlineKeyboardButton("💌 دعوت از دوستان", callback_data="invite_friends")] # دکمه جدید برای دعوت
    ]
    return InlineKeyboardMarkup(keyboard)


def get_action_buttons_keyboard(full_amount_to_sell_units=None):
    keyboard = []

    # اگر مقداری برای دکمه "فروش تمام موجودی" ارائه شده باشد (برای مراحل وارد کردن مقدار)
    if full_amount_to_sell_units is not None:
        button_text = "کلیک برای فروش کامل (قبل از اینتر اسم ربات دستی پاک شود)"
        # استفاده از str() برای اطمینان از حفظ بالاترین دقت اعشاری در مقدار
        query_value = str(full_amount_to_sell_units)

        keyboard.append([
            InlineKeyboardButton(button_text, switch_inline_query_current_chat=query_value)
        ])

    # دکمه‌های عمومی (بازگشت و لغو) که همیشه نمایش داده می‌شوند
    keyboard.append([InlineKeyboardButton("بازگشت", callback_data="back_to_previous_step")])
    keyboard.append([InlineKeyboardButton("لغو", callback_data="cancel_trade")])

    return InlineKeyboardMarkup(keyboard)

def get_trade_active_keyboard():
    """
    این تابع یک کیبورد برای زمانی که معامله فعال است یا پس از لغو عملیات برمی‌گرداند،
    و امکان دسترسی به موجودی، معامله جدید و فروش پورتفو را فراهم می‌کند.
    """
    keyboard = [
        [InlineKeyboardButton("💰 موجودی و پورتفو", callback_data='show_balance_portfolio_from_trade')],
        [InlineKeyboardButton("📈 فروش پورتفو", callback_data='sell_portfolio_entry')], # اضافه شدن این دکمه
        [InlineKeyboardButton("⚡ شروع معامله جدید", callback_data='start_trade_new')],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_tpsl_choice_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد برای انتخاب تنظیم حد سود/حد ضرر.
    """
    keyboard = [
        [InlineKeyboardButton("✅ بله، تنظیم TP/SL", callback_data='set_tpsl')],
        [InlineKeyboardButton("⬅️ خیر، نیازی نیست (بازگشت به منو اصلی)", callback_data='back_to_main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_tpsl_cancel_keyboard() -> InlineKeyboardMarkup:
    """
    کیبورد برای لغو تنظیم حد سود/حد ضرر و بازگشت به منوی اصلی.
    """
    keyboard = [
        [InlineKeyboardButton("بازگشت به منو اصلی", callback_data='back_to_main_menu')],
    ]
    return InlineKeyboardMarkup(keyboard)


# فرض بر این است که ConversationHandler, ASKING_TP_SL, ENTERING_TP_PRICE, ENTERING_SL_PRICE
# get_main_menu_keyboard, get_tpsl_cancel_keyboard, get_trade_active_keyboard,
# conn, cursor, logging و دیگر توابع مورد نیاز در دسترس هستند.


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو مکالمه جاری و بازگشت به منوی اصلی."""
    user = update.effective_user
    logging.info(f"User {user.id} canceled the conversation.")
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "مکالمه لغو شد. به منوی اصلی بازگشتید.",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "مکالمه لغو شد. به منوی اصلی بازگشتید.",
            reply_markup=get_main_menu_keyboard()
        )

    # پاک کردن تمام user_data مربوط به مکالمه
    context.user_data.clear()  # یا context.user_data.pop('some_key', None) برای پاک کردن موارد خاص

    return ConversationHandler.END  # پایان مکالمه



# فرض بر این است که get_main_menu_keyboard, cursor, conn و ENTERING_TP_PRICE
# در فایل شما تعریف شده‌اند.

async def handle_tpsl_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    هندلر برای دکمه "بله، تنظیم TP/SL" در مرحله ASKING_TP_SL.
    از کاربر می‌خواهد قیمت حد سود را وارد کند.
    """
    query = update.callback_query
    await query.answer()  # پاسخ به CallbackQuery برای جلوگیری از بارگذاری بی‌پایان

    user_id = update.effective_user.id
    position_id = context.user_data.get('current_position_id_for_tpsl')

    if not position_id:
        logging.error(
            f"No position_id found in context for TP/SL setup for user {user_id}. Attempting to retrieve from DB.")
        # تلاش برای بازیابی position_id از آخرین پوزیشن باز کاربر
        # این یک fallback است، بهتر است current_position_id_for_tpsl همیشه ست شود
        cursor.execute(
            "SELECT position_id, buy_price FROM user_positions WHERE user_id = ? AND status = 'open' ORDER BY open_timestamp DESC LIMIT 1",
            (user_id,))
        last_position_data = cursor.fetchone()
        if last_position_data:
            position_id = last_position_data[0]
            buy_price = last_position_data[1]
            context.user_data['current_position_id_for_tpsl'] = position_id
            context.user_data['current_buy_price_for_tpsl'] = buy_price
            logging.info(f"Recovered position_id {position_id} for TP/SL setup for user {user_id}.")
        else:
            logging.error(f"No active position found for TP/SL setup for user {user_id}.")
            await query.edit_message_text(
                "خطا در تنظیم TP/SL. هیچ پوزیشن بازی یافت نشد. لطفاً دوباره تلاش کنید.",
                reply_markup=get_main_menu_keyboard(),
                parse_mode='Markdown'
            )
            # پاک کردن context.user_data مربوط به TP/SL
            context.user_data.pop('current_position_id_for_tpsl', None)
            context.user_data.pop('current_buy_price_for_tpsl', None)
            return ConversationHandler.END

    # دریافت قیمت خرید فعلی پوزیشن برای راهنمایی کاربر
    # اگر از context.user_data نبود، از دیتابیس بخونیم
    buy_price = context.user_data.get('current_buy_price_for_tpsl')
    if not buy_price:
        cursor.execute("SELECT buy_price FROM user_positions WHERE position_id = ?", (position_id,))
        buy_price_data = cursor.fetchone()
        buy_price = buy_price_data[0] if buy_price_data else 0.0
        context.user_data['current_buy_price_for_tpsl'] = buy_price

    # تنظیم مرحله فعلی به TP
    context.user_data['tpsl_step'] = 'tp'

    # --- ارسال پیام درخواست قیمت TP ---
    message_text = (
        "لطفاً **قیمت حد سود (Take Profit)** مورد نظر خود را وارد کنید.\n"
        f"قیمت خرید فعلی این پوزیشن: **${format_price(buy_price)}**\n"
        "قیمت حد سود باید **بزرگتر از قیمت خرید** باشد.\n"
        "برای لغو، از دکمه زیر استفاده کنید."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("بازگشت به منو اصلی (لغو)", callback_data='back_to_main_menu')]
    ])

    try:
        await query.edit_message_text(
            text=message_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        logging.info(f"User {user_id} prompted for TP price for position {position_id}.")
    except Exception as e:
        logging.error(f"Error editing message to ask for TP price for user {user_id}: {e}")
        await query.message.reply_text(  # اگر edit_message_text با خطا مواجه شد، یک پیام جدید ارسال می‌کنیم
            text=message_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )

    # بازگشت حالت مکالمه به ENTERING_TP_PRICE
    return ENTERING_TP_PRICE


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """هندلر کامند /trade - کاربر را به منوی انتخاب نوع معامله هدایت می‌کند."""
    # فرض بر این است که get_trade_type_keyboard() در فایل شما وجود دارد
    # و SELECTING_TRADE_TYPE یک State معتبر است.
    await update.message.reply_text("لطفاً نوع معامله خود را انتخاب کنید:", reply_markup=get_trade_active_keyboard())
    return SELECTING_TRADE_TYPE # این همان استیتی است که پس از کلیک روی trade_menu وارد می‌شوید


def get_coin_selection_keyboard(page=0, coins_per_page=12):
    if not top_coins:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("خطا: ارزها بارگذاری نشدند. دوباره تلاش کنید.", callback_data='back_to_main_menu')]])

    start_index = page * coins_per_page
    end_index = start_index + coins_per_page
    current_coins = top_coins[start_index:end_index]

    keyboard = []
    row = []
    for i, coin in enumerate(current_coins):
        row.append(
            InlineKeyboardButton(f"{coin['symbol']} ({coin['name']})", callback_data=f"select_coin_{coin['id']}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    pagination_row = []
    total_pages = math.ceil(len(top_coins) / coins_per_page)

    if page > 0:
        pagination_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"coins_page_{page - 1}"))

    pagination_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="no_op"))

    if end_index < len(top_coins):
        pagination_row.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"coins_page_{page + 1}"))

    if pagination_row:
        keyboard.append(pagination_row)

    keyboard.append([
        InlineKeyboardButton("❌ لغو معامله", callback_data='cancel_trade'),
        InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data='back_to_main_menu')
    ])
    return InlineKeyboardMarkup(keyboard)


def get_confirm_buy_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ تأیید خرید", callback_data='confirm_buy')],
        [InlineKeyboardButton("❌ لغو معامله", callback_data='cancel_trade')],
        [InlineKeyboardButton("🔙 بازگشت", callback_data='back_to_previous_step')]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_confirm_sell_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ تأیید فروش", callback_data='confirm_sell_final')],
        [InlineKeyboardButton("❌ لغو معامله", callback_data='cancel_trade')],
        [InlineKeyboardButton("🔙 بازگشت", callback_data='back_to_previous_step')]
    ]
    return InlineKeyboardMarkup(keyboard)


# --- توابعی که باید اضافه شوند ---

# تابع کمکی برای کیبورد بازگشت به منوی اصلی
# این تابع را اگر قبلاً اضافه نکرده‌اید، اضافه کنید.
# ... (کدهای موجود شما قبل از توابع ادمین) ...

# --- Admin Panel Helper Keyboard (ensure this is present) ---
def get_back_to_admin_panel_keyboard():
    keyboard = [[InlineKeyboardButton("⬅️ بازگشت به پنل ادمین", callback_data="admin_panel")]]
    return InlineKeyboardMarkup(keyboard)


# --- Admin Placeholder Functions (Now with actual logic or conversation starters) ---

# دکوراتور برای محدود کردن دسترسی به دستورات فقط برای ادمین‌ها
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text("شما اجازه دسترسی به این دستور/عملیات را ندارید.")
            elif update.message:
                await update.message.reply_text("شما اجازه دسترسی به این دستور/عملیات را ندارید.")
            logging.warning(f"Unauthorized access attempt to admin command by user {user_id}")
            return ConversationHandler.END  # اگر غیر ادمین بود، مکالمه را پایان دهد
        return await func(update, context)

    return wrapper






# فرض بر این است که توابع get_db_connection و get_back_to_admin_panel_keyboard
# و ADMIN_PANEL در دسترس هستند.

# تابع کمکی برای فرمت PnL
def format_pnl_percentage(pnl_amount: float, invested_amount: float) -> str:
    """Calculates and formats PnL percentage."""
    if invested_amount == 0:
        return "0.00%"
    percentage = (pnl_amount / invested_amount) * 100
    return f"{percentage:+.2f}%"

@admin_only
async def admin_all_open_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    logging.info(f"Admin {update.effective_user.id} requested all open positions summary.")

    conn = None
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row  # برای دسترسی به ستون‌ها با نام
        cursor = conn.cursor()

        # 1. از cached_prices برای گرفتن اطلاعات تجمیعی خرید و قیمت فعلی استفاده می‌کنیم.
        # این شامل total_buy_amount و average_buy_price است که توسط update_cached_buy_data
        # پر شده‌اند و قیمت فعلی که توسط fetch_and_cache_all_prices پر شده است.
        cursor.execute("""
                       SELECT coin_slug, price, total_buy_amount, average_buy_price
                       FROM cached_prices
                       WHERE total_buy_amount > 0
                         AND average_buy_price > 0
                       ORDER BY coin_slug -- این مرتب سازی فقط برای خوانایی اولیه است، ما بعدا بر اساس ارزش چیپ مرتب میکنیم
                       """)
        active_cached_positions_raw = cursor.fetchall()  # نام متغیر را تغییر دادم برای وضوح

        if not active_cached_positions_raw:  # از نام جدید استفاده میکنیم
            await query.edit_message_text(
                "هیچ پوزیشن بازی در حال حاضر وجود ندارد (بر اساس داده‌های کش‌شده).",
                reply_markup=get_back_to_admin_panel_keyboard()
            )
            logging.info("No open positions found in cached_prices with total_buy_amount > 0.")
            return ADMIN_PANEL

        # --- اضافه کردن کد برای محاسبه current_value و مرتب‌سازی ---
        # تبدیل Row objects به دیکشنری برای دسترسی آسان‌تر و اضافه کردن current_value
        processed_positions = []
        for row in active_cached_positions_raw:
            pos_dict = dict(row)  # تبدیل Row به دیکشنری
            total_buy_amount = pos_dict['total_buy_amount']
            current_price = pos_dict['price']

            # اطمینان حاصل کنید total_buy_amount و current_price معتبر باشند
            if total_buy_amount > 0 and current_price > 0:
                pos_dict['current_value'] = total_buy_amount * current_price
            else:
                pos_dict[
                    'current_value'] = 0.0  # اگر مقداری نداشت، ارزش را صفر در نظر می‌گیریم (این موارد نباید از WHERE رد شوند ولی برای اطمینان)

            # اینجا می توانیم PnL و درصد آن را هم محاسبه کنیم تا برای مرتب سازی احتمالی در آینده آماده باشد
            invested_value = total_buy_amount * pos_dict['average_buy_price']
            pos_dict['pnl_amount'] = pos_dict['current_value'] - invested_value
            pos_dict['pnl_percent'] = (pos_dict['pnl_amount'] / invested_value) * 100 if invested_value > 0 else 0

            processed_positions.append(pos_dict)

        # مرتب‌سازی پوزیشن‌ها بر اساس 'current_value' به صورت نزولی (از بزرگ به کوچک)
        processed_positions.sort(key=lambda x: x['current_value'], reverse=True)
        # --- پایان کد مرتب‌سازی ---

        message_text = "📈 **خلاصه تمام پوزیشن‌های باز (مرتب شده بر اساس ارزش چیپ):**\n\n"  # تغییر عنوان برای وضوح
        total_overall_pnl = 0.0

        # برای هر کوین_اسلاگ که پوزیشن باز دارد (حالا این لیست مرتب شده است)
        for pos in processed_positions:  # اینجا روی processed_positions حلقه می‌زنیم
            coin_slug = pos['coin_slug']
            current_price = pos['price']
            # total_buy_amount و average_buy_price را مستقیماً از pos می‌خوانیم
            total_buy_amount = pos['total_buy_amount']
            average_buy_price = pos['average_buy_price']
            current_value = pos['current_value']  # حالا current_value از قبل محاسبه شده است
            pnl_amount = pos['pnl_amount']  # PnL هم از قبل محاسبه شده
            pnl_percent = pos['pnl_percent']  # درصد PnL هم از قبل محاسبه شده

            # این چک هنوز لازم است اگر مقدار اولیه صفر باشد (که با WHERE total_buy_amount > 0 نباید اتفاق بیفتد)
            # if total_buy_amount <= 0:
            #     continue

            # total_overall_pnl را با pnl_amount هر پوزیشن جمع می‌کنیم
            total_overall_pnl += pnl_amount

            # تلاش برای پیدا کردن نماد (symbol) از coin_slug
            slug_to_symbol_map = {v: k for k, v in context.application.bot_data.get('symbol_to_slug_map', {}).items()}
            display_symbol = slug_to_symbol_map.get(coin_slug, coin_slug.capitalize())

            pnl_emoji = "📈" if pnl_amount >= 0 else "📉"  # ایموجی بر اساس سود/زیان

            message_text += (
                f"📊 **{display_symbol}:**\n"
                f"  قیمت فعلی: `{format_price(current_price)}`\n"
                f"  حجم کل (Buy): `{current_value:,.2f}$`\n"
                f"  میانگین قیمت خرید: `${average_buy_price:,.4f}`\n"
                f"  {pnl_emoji} سود/زیان (PnL): `{pnl_amount:,.2f}$ ({pnl_percent:+.2f}%)`\n\n"
            )

        message_text += f"-----------------------------------------\n"
        message_text += f"💰 **سود/زیان کل تمامی پوزیشن‌های باز: `{total_overall_pnl:,.2f}$`**"

        reply_markup = get_back_to_admin_panel_keyboard()

        await query.edit_message_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=telegram.constants.ParseMode.MARKDOWN
        )
        return ADMIN_PANEL

    except Exception as e:
        logging.error(f"Error in admin_all_open_positions: {e}", exc_info=True)
        await query.edit_message_text(
            "متاسفم، مشکلی در نمایش معاملات باز رخ داد.",
            reply_markup=get_back_to_admin_panel_keyboard()
        )
        return ADMIN_PANEL
    finally:
        if conn:
            conn.close()
@admin_only
async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("📊 نمایش آمار ربات", callback_data="admin_stats")],
        # تغییر اینجا:
        [InlineKeyboardButton("💰 مدیریت موجودی کاربر", callback_data="admin_manage_balance")],
        [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📈 مشاهده تمام معاملات باز", callback_data="admin_all_open_positions")],
        [InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="back_to_main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    logging.info(f"Admin {update.effective_user.id} accessed admin panel.")

    message = "به پنل ادمین خوش آمدید. لطفا یک گزینه را انتخاب کنید:"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=message, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=message, reply_markup=reply_markup)

    return ADMIN_PANEL

# تابع show_user_list_for_admin
@admin_only
async def show_user_list_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    logging.info(f"Admin {update.effective_user.id} requested user list for management.")

    users_data = get_all_users_data()  # اطلاعات همه کاربران را از دیتابیس می‌گیریم

    if not users_data:
        await query.edit_message_text("هیچ کاربری در سیستم یافت نشد.", reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL  # بازگشت به پنل ادمین

    keyboard = []
    for user in users_data:
        # نام کاربری، موجودی، PNL و کمیسیون رو در دکمه نمایش میدیم
        # **تغییر: از HTML برای فرمت‌بندی استفاده می‌کنیم**
        username = user.get('username', 'N/A')
        # اگر username خالی یا None بود، از user_id استفاده کن
        display_username = f"@{username}" if username and username != "N/A" else f"User {user['user_id']}"

        button_text = (
            f"{display_username} | موجودی: {user['balance']:.2f} چیپ | "
            f"PNL: {user['pnl']:.2f} | کمیسیون: {user['commission_paid']:.2f}"
        )

        # اطلاعات user_id رو در callback_data ذخیره می‌کنیم
        callback_data = f"admin_select_user:{user['user_id']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("⬅️ بازگشت به پنل ادمین", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text="<b>لطفا کاربری را برای مدیریت انتخاب کنید:</b>",  # **تغییر: استفاده از <b> برای بولد کردن در HTML**
        reply_markup=reply_markup,
        parse_mode=telegram.constants.ParseMode.HTML  # **تغییر اصلی: استفاده از HTML**
    )
    return ADMIN_SELECT_USER  # استیت جدید برای انتخاب کاربر

# تابع admin_selected_user_action (وقتی ادمین کاربری را از لیست انتخاب می‌کند)
@admin_only
async def admin_selected_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # استخراج user_id از callback_data
    user_id = int(query.data.split(':')[1])
    context.user_data['selected_admin_user_id'] = user_id  # ذخیره user_id در context

    user_info = get_user_info_by_id(user_id)  # اطلاعات کاربر انتخاب شده را می‌گیریم

    if not user_info:
        await query.edit_message_text("خطا: کاربر مورد نظر یافت نشد. لطفا دوباره تلاش کنید.",
                                      reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL

    logging.info(
        f"Admin {update.effective_user.id} selected user {user_id} ({user_info.get('username')}) for management.")

    # حالا منوی مدیریت کاربر انتخاب شده را نمایش می‌دهیم
    keyboard = [
        [InlineKeyboardButton("➕ افزودن موجودی", callback_data=f"admin_change_balance:add:{user_id}")],
        [InlineKeyboardButton("➖ کسر موجودی", callback_data=f"admin_change_balance:deduct:{user_id}")],
        [InlineKeyboardButton("📊 مشاهده پوزیشن ها", callback_data=f"admin_view_user_positions:{user_id}")],
        [InlineKeyboardButton("⬅️ بازگشت به لیست کاربران", callback_data="admin_back_to_user_list")],
        # بازگشت به تابع show_user_list_for_admin
        [InlineKeyboardButton("🏠 بازگشت به پنل ادمین اصلی", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=f"شما کاربر @{user_info.get('username', 'N/A')} (ID: {user_id}) را انتخاب کردید.\n"
             f"موجودی: **{user_info.get('balance', 0):.2f} چیپ**\n"  # مطمئن شوید نام ستون balance است
             f"PNL کل: **{user_info.get('total_realized_pnl', 0):.2f}**\n"
             f"کمیسیون پرداختی ربات: **{user_info.get('bot_commission_balance', 0):.2f}**\n\n"
             "چه عملیاتی می‌خواهید انجام دهید؟",
        reply_markup=reply_markup,
        parse_mode=telegram.constants.ParseMode.MARKDOWN
    )
    return ADMIN_SELECTED_USER_ACTIONS  # استیت جدید برای منوی عملیات روی کاربر


# تابع جدید برای شروع فرآیند تغییر موجودی
@admin_only
async def admin_initiate_balance_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # اطلاعات از callback_data: admin_change_balance:TYPE:USER_ID
    parts = query.data.split(':')
    action_type = parts[1]  # 'add' or 'deduct'
    user_id = int(parts[2])

    context.user_data['selected_admin_user_id'] = user_id
    context.user_data['balance_change_type'] = action_type

    user_info = get_user_info_by_id(user_id)
    if not user_info:
        await query.edit_message_text("خطا: کاربر مورد نظر یافت نشد.", reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL

    message_text = ""
    if action_type == 'add':
        message_text = f"لطفاً **مقدار چیپی** که می‌خواهید به موجودی کاربر @{user_info.get('username', 'N/A')} (ID: {user_id}) **اضافه** کنید را وارد کنید."
    elif action_type == 'deduct':
        message_text = f"لطفاً **مقدار چیپی** که می‌خواهید از موجودی کاربر @{user_info.get('username', 'N/A')} (ID: {user_id}) **کسر** کنید را وارد کنید."

    message_text += "\n\n**(فقط عدد وارد کنید، مثال: `100.50` یا /cancel برای لغو)**"

    await query.edit_message_text(
        text=message_text,
        reply_markup=get_back_to_admin_panel_keyboard(),
        # اینجا می توانید یک دکمه بازگشت به منوی کاربر انتخاب شده قرار دهید
        parse_mode=telegram.constants.ParseMode.MARKDOWN
    )
    return ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT  # استیت جدید برای دریافت مقدار


# تابع جدید برای پردازش مقدار تغییر موجودی
@admin_only
async def admin_process_balance_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount_str = update.message.text
    target_user_id = context.user_data.get('selected_admin_user_id')
    action_type = context.user_data.get('balance_change_type')

    if not target_user_id or not action_type:
        await update.message.reply_text("خطا: اطلاعات کاربر یا نوع عملیات پیدا نشد. لطفاً از ابتدا شروع کنید.",
                                        reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL

    try:
        amount = float(amount_str)
        if amount <= 0:
            await update.message.reply_text("مقدار باید یک عدد مثبت باشد. لطفاً دوباره وارد کنید.",
                                            reply_markup=get_back_to_admin_panel_keyboard())
            return ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT balance, username FROM users WHERE user_id = ?", (target_user_id,))
        result = cursor.fetchone()
        if not result:
            await update.message.reply_text(f"خطا: کاربر با User ID `{target_user_id}` یافت نشد.",
                                            parse_mode=telegram.constants.ParseMode.MARKDOWN,
                                            reply_markup=get_back_to_admin_panel_keyboard())
            conn.close()
            return ADMIN_PANEL

        current_balance, username = result[0], result[1]
        new_balance = current_balance

        if action_type == 'add':
            new_balance += amount
            action_verb = "اضافه"
        elif action_type == 'deduct':
            new_balance -= amount
            action_verb = "کسر"
            if new_balance < 0:
                await update.message.reply_text("موجودی نمی‌تواند منفی شود. لطفاً مقدار کمتری وارد کنید.",
                                                reply_markup=get_back_to_admin_panel_keyboard())
                conn.close()
                return ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT
        else:
            await update.message.reply_text("خطای داخلی: نوع عملیات نامشخص. لطفاً دوباره تلاش کنید.",
                                            reply_markup=get_back_to_admin_panel_keyboard())
            conn.close()
            return ADMIN_PANEL

        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, target_user_id))
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"موجودی کاربر @{username} (ID: {target_user_id}) با موفقیت **{amount:.2f} چیپ** {action_verb} شد.\n"
            f"موجودی جدید: **{new_balance:.2f} چیپ**",
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=get_back_to_admin_panel_keyboard()
        )
        logging.info(
            f"Admin {update.effective_user.id} {action_verb}ed {amount:.2f} to user {target_user_id}'s balance. New balance: {new_balance:.2f}.")

        # بعد از اتمام عملیات، به منوی عملیات روی کاربر بازگردیم
        # برای این کار، باید یک دکمه CallbackQuery به admin_selected_user_action بسازیم که به استیت ADMIN_SELECTED_USER_ACTIONS برمی‌گردد
        # یا به ADMIN_PANEL برگردیم
        return ADMIN_PANEL  # فعلاً به پنل ادمین اصلی برمی‌گردیم
    except ValueError:
        await update.message.reply_text(
            "مقدار نامعتبر است. لطفاً یک عدد وارد کنید (مثال: `100.50`).\n"
            "\n\n**(یا /cancel برای لغو)**",
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=get_back_to_admin_panel_keyboard()
        )
        return ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT
    except Exception as e:
        logging.error(f"Error processing balance change for user {target_user_id}: {e}")
        await update.message.reply_text("خطایی در به‌روزرسانی موجودی کاربر رخ داد. لطفاً دوباره امتحان کنید.",
                                        reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL


# تابع جدید برای مشاهده پوزیشن‌های کاربر (شبیه به admin_manage_balance_entry)
@admin_only
async def admin_view_user_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    conn = None  # تعریف connection در اینجا
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row  # برای دسترسی به ستون‌ها با نام
        cursor = conn.cursor()  # تعریف cursor در اینجا

        # استخراج user_id از callback_data
        # callback_data فرمت "admin_select_user:USER_ID" یا مشابه آن را دارد
        # لاگ شما "admin_view_user_positions_USER_ID" را نشان می‌دهد.
        # پس باید بررسی کنیم که callback_data دقیقاً چیست
        # اگر فرمت شما admin_view_user_positions_USER_ID است:
        # admin_target_user_id = int(query.data.split('_')[3])
        # اگر فرمت شما admin_select_user:USER_ID است (که از دکمه "بازگشت به منوی عملیات کاربر" می‌آید):
        admin_target_user_id = int(query.data.split(':')[1])  # این خط را بر اساس فرمت دقیق دکمه شما تنظیم کنید.
        # طبق لاگ قبلی شما، این هندلر برای admin_view_user_positions_XYZ فراخوانی شده بود،
        # پس اگر هنوز همان فرمت است، خط بالا را کامنت کرده و خط زیر را فعال کنید:
        # admin_target_user_id = int(query.data.split('_')[3])

        context.user_data['target_user_id'] = admin_target_user_id  # ذخیره برای توابع بعدی در صورت نیاز

        # واکشی اطلاعات کاربر هدف برای نمایش نام/یوزرنیم
        cursor.execute("SELECT username, first_name FROM users WHERE user_id = ?", (admin_target_user_id,))
        target_user_data = cursor.fetchone()
        if not target_user_data:
            await query.edit_message_text("خطا: کاربر مورد نظر یافت نشد.",
                                          reply_markup=get_back_to_admin_panel_keyboard())
            return ADMIN_PANEL

        target_username = target_user_data['username'] if target_user_data['username'] else "بدون یوزرنیم"
        target_first_name = target_user_data['first_name'] if target_user_data['first_name'] else "کاربر ناشناس"

        logging.info(
            f"Admin {update.effective_user.id} viewing positions for user {admin_target_user_id} ({target_username}).")

        # فراخوانی تابع get_user_positions_from_db
        user_open_positions = get_user_positions_from_db(admin_target_user_id)

        message_text = f"📈 **پوزیشن‌های باز برای کاربر {target_first_name} (@{target_username}):**\n\n"

        if not user_open_positions:
            message_text += "این کاربر در حال حاضر هیچ پوزیشن بازی ندارد."
        else:
            # برای مرتب‌سازی و نمایش PnL نیاز به قیمت‌های لحظه‌ای داریم
            current_prices_map = await fetch_and_cache_all_prices_internal(
                context)  # مطمئن شوید این تابع در دسترس است و یک دیکشنری slug:price برمی‌گرداند

            processed_positions = []
            for pos in user_open_positions:
                symbol = pos['symbol']
                amount = pos['amount']
                buy_price = pos['buy_price']
                coin_slug = pos['coin_slug']

                # قیمت لحظه‌ای را از مپ قیمت‌ها می‌گیریم
                current_price = current_prices_map.get(coin_slug, 0.0)

                # محاسبه PnL و ارزش فعلی
                if current_price > 0:
                    current_value = amount * current_price
                    invested_value = amount * buy_price
                    pnl_amount = current_value - invested_value
                    pnl_percent = (pnl_amount / invested_value) * 100 if invested_value > 0 else 0
                else:
                    # اگر قیمت لحظه‌ای در دسترس نیست، محاسبات را N/A قرار می‌دهیم
                    current_value = 0.0  # برای مرتب سازی
                    pnl_amount = 0.0
                    pnl_percent = 0.0

                pnl_emoji = "📈" if pnl_amount >= 0 else "📉"

                # فقط پوزیشن‌های با ارزش قابل نمایش را اضافه می‌کنیم
                # اگر current_value کمتر از MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD باشد، نمایش داده نمی‌شود.
                if current_value >= MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD or current_price == 0:  # اگر قیمت صفر بود هم نمایش می‌دهیم تا مشخص شود مشکلی در قیمت هست
                    pos_info = {
                        'symbol': symbol,
                        'amount': amount,
                        'buy_price': buy_price,
                        'current_price': current_price,
                        'current_value': current_value,
                        'pnl_amount': pnl_amount,
                        'pnl_percent': pnl_percent,
                        'pnl_emoji': pnl_emoji
                    }
                    processed_positions.append(pos_info)

            # مرتب‌سازی پوزیشن‌ها بر اساس ارزش چیپ (current_value) به صورت نزولی
            # اگر current_value عددی نبود (مثلا N/A)، آن را در انتهای لیست قرار می‌دهد.
            processed_positions.sort(
                key=lambda x: x['current_value'] if isinstance(x['current_value'], (int, float)) else -1, reverse=True)

            for pos_info in processed_positions:
                # فرمت کردن مقادیر برای نمایش
                display_current_price = f"${format_price(pos_info['current_price'])}" if pos_info[
                                                                                             'current_price'] != 0 else "N/A"
                display_current_value = f"{pos_info['current_value']:,.2f}$" if pos_info[
                                                                                    'current_value'] != 0 else "N/A"
                display_pnl_amount = f"{pos_info['pnl_amount']:+.2f}$"
                display_pnl_percent = f"{pos_info['pnl_percent']:+.2f}%"

                message_text += (
                    f"\n💎 **{pos_info['symbol']}**\n"
                    f"  🔢 مقدار: `{pos_info['amount']:.6f}`\n"
                    f"  💲 میانگین قیمت خرید: `${format_price(pos_info['buy_price'])}`\n"
                    f"  💲 قیمت فعلی: `{display_current_price}`\n"
                    f"  💰 ارزش فعلی: `{display_current_value}`\n"
                    f"  {pos_info['pnl_emoji']} سود/زیان: `{display_pnl_amount} ({display_pnl_percent})`\n"
                )

        # دکمه‌های بازگشت
        keyboard = [[InlineKeyboardButton("⬅️ بازگشت به منوی عملیات کاربر",
                                          callback_data=f"admin_select_user:{admin_target_user_id}")],
                    [InlineKeyboardButton("🏠 بازگشت به پنل ادمین اصلی", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=telegram.constants.ParseMode.MARKDOWN
        )
        return ADMIN_SELECTED_USER_ACTIONS  # بازگشت به منوی عملیات روی کاربر

    except Exception as e:
        logging.error(
            f"Error in admin_view_user_positions for user {admin_target_user_id if 'admin_target_user_id' in locals() else 'N/A'}: {e}",
            exc_info=True)
        await query.edit_message_text(
            "متاسفم، مشکلی در نمایش پوزیشن‌های باز کاربر رخ داد.",
            reply_markup=get_back_to_admin_panel_keyboard()  # یا یک کیبورد مناسب دیگر
        )
        return ADMIN_PANEL
    finally:
        if conn:
            conn.close()



@admin_only
async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(user_id) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(balance) FROM users")
    total_chips = cursor.fetchone()[0]
    total_chips = total_chips if total_chips is not None else 0.0  # برای حالتی که موجودی هیچکس 0.0 باشد.

    cursor.execute("SELECT COUNT(position_id) FROM user_positions")
    # یا اگر فقط تعداد کل ردیف‌ها را می‌خواهید، از COUNT(*) استفاده کنید:
    # cursor.execute("SELECT COUNT(*) FROM user_positions")
    total_trades = cursor.fetchone()[0]

    conn.close()

    message = "📊 **آمار ربات:**\n\n" \
              f"👥 تعداد کاربران: **{total_users}**\n" \
              f"💰 کل موجودی چیپ (کاربران): **{total_chips:.2f} چیپ**\n" \
              f"📈 تعداد کل معاملات: **{total_trades}**"

    logging.info(f"Admin {update.effective_user.id} requested bot stats.")

    # edit_message_text برای اطمینان از به روزرسانی پیام قبلی ادمین
    if update.callback_query:
        await update.callback_query.edit_message_text(message, reply_markup=get_back_to_admin_panel_keyboard(),
                                                      parse_mode=telegram.constants.ParseMode.MARKDOWN)
    elif update.message:  # اگر مستقیما با دستور /admin_stats_command فراخوانی شود
        await update.message.reply_text(message, reply_markup=get_back_to_admin_panel_keyboard(),
                                        parse_mode=telegram.constants.ParseMode.MARKDOWN)
    return ADMIN_PANEL  # بازگشت به حالت ADMIN_PANEL


@admin_only
async def admin_manage_balance_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = "💰 **مدیریت موجودی کاربر:**\n" \
              "لطفاً **User ID** کاربری که می‌خواهید موجودی او را تغییر دهید را وارد کنید." \
              "\n\n**(یا /cancel برای لغو)**"
    logging.info(f"Admin {update.effective_user.id} initiated balance management.")

    # از edit_message_text برای اطمینان از اینکه پیام قبلی ادمین به روز می شود استفاده کنید.
    if update.callback_query:
        await update.callback_query.edit_message_text(message, reply_markup=get_back_to_admin_panel_keyboard(),
                                                      parse_mode=telegram.constants.ParseMode.MARKDOWN)
    else:  # اگر از طریق دستور متنی فراخوانی شود (اگر ConversationHandler را به گونه ای تنظیم کرده باشید)
        await update.message.reply_text(message, reply_markup=get_back_to_admin_panel_keyboard(),
                                        parse_mode=telegram.constants.ParseMode.MARKDOWN)
    return ADMIN_MANAGE_BALANCE_USER_ID  # به مرحله بعدی برویم: دریافت User ID


@admin_only
async def admin_get_user_id_for_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id_str = update.message.text
    try:
        target_user_id = int(user_id_str)
        context.user_data['target_user_id'] = target_user_id

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_user_id,))
        result = cursor.fetchone()
        conn.close()

        if result:
            current_balance = result[0]
            await update.message.reply_text(
                f"موجودی فعلی کاربر `{target_user_id}`: **{current_balance:.2f} چیپ**\n\n"
                f"لطفاً **مقدار جدید موجودی** را وارد کنید (مثال: `1500.00` برای افزایش، `500.00` برای کاهش).\n"
                f"اگر می‌خواهید موجودی را اضافه یا کم کنید، می‌توانید با علامت `+` یا `-` وارد کنید (مثال: `+100` یا `-50`)."
                f"\n\n**(یا /cancel برای لغو)**",
                parse_mode=telegram.constants.ParseMode.MARKDOWN,
                reply_markup=get_back_to_admin_panel_keyboard()
            )
            return ADMIN_MANAGE_BALANCE_AMOUNT  # به مرحله بعدی برویم: دریافت مقدار موجودی
        else:
            await update.message.reply_text(
                f"کاربری با User ID `{target_user_id}` یافت نشد. لطفاً یک User ID معتبر وارد کنید.\n"
                f"\n\n**(یا /cancel برای لغو)**",
                parse_mode=telegram.constants.ParseMode.MARKDOWN,
                reply_markup=get_back_to_admin_panel_keyboard()
            )
            return ADMIN_MANAGE_BALANCE_USER_ID  # در همین مرحله بمانیم
    except ValueError:
        await update.message.reply_text(
            "فرمت User ID نامعتبر است. لطفاً یک عدد صحیح وارد کنید.\n"
            "\n\n**(یا /cancel برای لغو)**",
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=get_back_to_admin_panel_keyboard()
        )
        return ADMIN_MANAGE_BALANCE_USER_ID  # در همین مرحله بمانیم


@admin_only
async def admin_set_user_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount_str = update.message.text
    target_user_id = context.user_data.get('target_user_id')

    if not target_user_id:
        await update.message.reply_text("خطا: User ID کاربر پیدا نشد. لطفاً از ابتدا شروع کنید.",
                                        reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL  # بازگشت به پنل ادمین

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # ابتدا موجودی فعلی را دریافت کنید
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_user_id,))
        result = cursor.fetchone()
        if not result:
            await update.message.reply_text(f"خطا: کاربر با User ID `{target_user_id}` یافت نشد.",
                                            parse_mode=telegram.constants.ParseMode.MARKDOWN,
                                            reply_markup=get_back_to_admin_panel_keyboard())
            conn.close()
            return ADMIN_PANEL

        current_balance = result[0]
        new_balance = 0.0

        if amount_str.startswith('+'):
            amount_to_add = float(amount_str[1:])
            new_balance = current_balance + amount_to_add
            action = "افزایش"
        elif amount_str.startswith('-'):
            amount_to_subtract = float(amount_str[1:])
            new_balance = current_balance - amount_to_subtract
            action = "کاهش"
        else:
            new_balance = float(amount_str)
            action = "تنظیم"

        # بررسی حداقل موجودی (می‌توانید منطق خاص خود را اینجا اضافه کنید)
        if new_balance < 0:
            await update.message.reply_text("موجودی نمی‌تواند منفی باشد. لطفاً مقدار معتبری وارد کنید.",
                                            reply_markup=get_back_to_admin_panel_keyboard())
            conn.close()
            return ADMIN_MANAGE_BALANCE_AMOUNT  # در همین مرحله بمانیم

        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, target_user_id))
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"موجودی کاربر `{target_user_id}` با موفقیت به **{new_balance:.2f} چیپ** {action} یافت.",
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=get_back_to_admin_panel_keyboard()
        )
        logging.info(
            f"Admin {update.effective_user.id} updated balance of user {target_user_id} to {new_balance:.2f}. Old balance: {current_balance:.2f}. Action: {action} {amount_str}.")
        return ADMIN_PANEL  # بازگشت به پنل ادمین
    except ValueError:
        await update.message.reply_text(
            "مقدار موجودی نامعتبر است. لطفاً یک عدد (مثال: `1500.00`) یا با `+` یا `-` وارد کنید (مثال: `+100`).\n"
            "\n\n**(یا /cancel برای لغو)**",
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=get_back_to_admin_panel_keyboard()
        )
        return ADMIN_MANAGE_BALANCE_AMOUNT  # در همین مرحله بمانیم
    except Exception as e:
        logging.error(f"Error setting user balance: {e}")
        await update.message.reply_text("خطایی در به‌روزرسانی موجودی کاربر رخ داد. لطفاً دوباره امتحان کنید.",
                                        reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_PANEL


@admin_only
async def admin_broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = "📢 **ارسال پیام همگانی:**\n" \
              "لطفاً پیامی که می‌خواهید برای همه کاربران ارسال شود را وارد کنید." \
              "\n\n**(یا /cancel برای لغو)**"
    logging.info(f"Admin {update.effective_user.id} initiated broadcast.")

    if update.callback_query:
        await update.callback_query.edit_message_text(message, reply_markup=get_back_to_admin_panel_keyboard(),
                                                      parse_mode=telegram.constants.ParseMode.MARKDOWN)
    else:  # اگر از طریق دستور متنی فراخوانی شود
        await update.message.reply_text(message, reply_markup=get_back_to_admin_panel_keyboard(),
                                        parse_mode=telegram.constants.ParseMode.MARKDOWN)
    return ADMIN_BROADCAST_MESSAGE  # به مرحله بعدی برویم: دریافت پیام همگانی


@admin_only
async def admin_send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    broadcast_message = update.message.text
    if not broadcast_message:
        await update.message.reply_text("پیام خالی نمی‌تواند ارسال شود. لطفاً متن پیام را وارد کنید.",
                                        reply_markup=get_back_to_admin_panel_keyboard())
        return ADMIN_BROADCAST_MESSAGE

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, chat_id FROM users WHERE user_id != ?", (0,))  # تمام کاربران به جز ربات
    users = cursor.fetchall()
    conn.close()

    sent_count = 0
    failed_count = 0

    status_message = await update.message.reply_text("در حال ارسال پیام همگانی... ممکن است زمان ببرد.")

    for user_id, chat_id in users:
        try:
            # از chat_id کاربر برای ارسال پیام استفاده می کنیم
            await context.bot.send_message(chat_id=chat_id, text=broadcast_message)
            sent_count += 1
            await asyncio.sleep(0.05)  # تأخیر کوچک برای جلوگیری از Flood Limit
        except telegram.error.TimedOut:
            logging.warning(f"Sending message to user {user_id} timed out.")
            failed_count += 1
        except telegram.error.BadRequest as e:
            # ممکن است کاربر ربات را بلاک کرده باشد
            logging.warning(f"Could not send message to user {user_id} (chat_id: {chat_id}): {e}")
            failed_count += 1
        except Exception as e:
            logging.error(f"An unexpected error occurred while sending message to user {user_id}: {e}")
            failed_count += 1

    await status_message.edit_text(
        f"✅ پیام همگانی با موفقیت ارسال شد.\n"
        f"ارسال شده به: **{sent_count} کاربر**\n"
        f"ناموفق: **{failed_count} کاربر**",
        parse_mode=telegram.constants.ParseMode.MARKDOWN,
        reply_markup=get_back_to_admin_panel_keyboard()
    )
    logging.info(
        f"Admin {update.effective_user.id} sent broadcast message. Sent to {sent_count}, failed for {failed_count}.")
    return ADMIN_PANEL  # بازگشت به پنل ادمین


async def admin_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.effective_user
    logging.info("User %s canceled the admin conversation.", user.first_name)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "عملیات ادمین لغو شد. به منوی اصلی باز می‌گردید.",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "عملیات ادمین لغو شد. به منوی اصلی باز می‌گردید.",
            reply_markup=get_main_menu_keyboard()
        )
    return ConversationHandler.END



# تابع start شما (با تغییرات)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    هندلر دستور /start. کاربر را به دیتابیس اضافه/بروزرسانی می‌کند،
    سطح VIP او را بررسی می‌کند، لینک دعوت را مدیریت می‌کند و پیام خوش‌آمدگویی را نمایش می‌دهد.
    """
    user = update.effective_user
    user_id = user.id
    chat_id = update.effective_chat.id
    username = user.username
    first_name = user.first_name

    referrer_id = None
    # 1. استخراج referrer_id از لینک دعوت
    if context.args and len(context.args) > 0:
        payload = context.args[0]
        if payload.startswith("invite_"):
            try:
                extracted_referrer_id = int(payload.replace("invite_", ""))
                # مطمئن شوید کاربر خودش را دعوت نکرده باشد
                if extracted_referrer_id != user_id:
                    referrer_id = extracted_referrer_id
                    logging.info(f"User {user_id} started with referrer_id: {referrer_id}")
                else:
                    logging.info(f"User {user_id} tried to invite themselves. Referrer ignored.")
            except ValueError:
                logging.warning(f"Invalid referrer ID in start payload: {payload}")
        else:
            logging.info(f"User {user_id} started with unknown payload: {payload}")

    # اضافه کردن یا بروزرسانی کاربر در دیتابیس (شامل chat_id، username، first_name)
    # این تابع خودش مطمئن می‌شود که اطلاعات کاربر بروز است و مقادیر قبلی حفظ شوند.
    # همچنین پاداش 100 چیپ برای کاربر جدید دعوت شده را می‌دهد.
    is_new_user = add_user_if_not_exists(user_id, chat_id, username, first_name, referrer_id)

    # 2. اعمال پاداش به معرف (اگر کاربر جدید است و از طریق لینک دعوت آمده)
    if is_new_user and referrer_id:
        conn = get_db_connection()
        cursor = conn.cursor()

        # بررسی کنیم آیا این دعوت قبلاً پاداش داده شده است یا خیر
        # این برای جلوگیری از پاداش تکراری به معرف است
        cursor.execute('SELECT * FROM referral_rewards WHERE new_user_id = ?', (user_id,))
        existing_referral_reward = cursor.fetchone()

        if not existing_referral_reward:
            # اگر پاداش قبلاً داده نشده، آن را به معرف بدهید
            if update_user_balance(referrer_id, 300):  # 300 چیپ برای معرف
                logging.info(f"Referrer {referrer_id} received 300 chips for inviting {user_id}.")
                try:
                    # ارسال پیام اطلاع‌رسانی به معرف
                    await context.bot.send_message(chat_id=referrer_id,
                                                   text=f"تبریک! 🎊 شما ۳۰۰ چیپ بابت دعوت کاربر {first_name if first_name else 'جدید'} دریافت کردید!")
                except telegram.error.BadRequest as e:
                    logging.warning(f"Could not send referral reward notification to referrer {referrer_id}: {e}")

                # ثبت رکورد پاداش در جدول referral_rewards
                cursor.execute('INSERT INTO referral_rewards (referrer_id, new_user_id) VALUES (?, ?)',
                               (referrer_id, user_id))
                conn.commit()
                logging.info(f"Referral record added for referrer {referrer_id} and new user {user_id}.")
            else:
                logging.error(f"Failed to give 300 chips to referrer {referrer_id} for inviting {user_id}.")
        else:
            logging.info(f"Referral reward for {user_id} by {referrer_id} already given.")

        conn.close()  # بستن اتصال دیتابیس پس از اتمام کار

    # --- بررسی و ارتقاء سطح VIP کاربر ---
    await check_and_upgrade_vip_level(user_id, context)

    # نمایش پیام خوش‌آمدگویی و منو اصلی
    await update.message.reply_text(
        f"سلام {first_name if first_name else 'کاربر عزیز'}! به بازی تریدینگ چیپ خوش آمدید!\n"
        "موجودی اولیه شما برای شروع معاملات: 1000 چیپ.\n"  # این پیام برای همه کاربران جدید و قدیمی نمایش داده می‌شود
        "برای مشاهده گزینه‌ها از دکمه‌های زیر استفاده کنید.",
        reply_markup=get_main_menu_keyboard()
    )

# --- Conversation Handler Entry Point ---
async def trade_entry_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['conv_state_history'] = []
    context.user_data['current_conv_step'] = 'start_trade'
    context.user_data['current_coin_page'] = 0
    # همچنین متغیرهای مربوط به تأیید مجدد قیمت را ریست می کنیم
    context.user_data['reconfirmed_price'] = None
    context.user_data['initial_displayed_price'] = None
    logging.info(f"User {update.effective_user.id} starting new trade conversation.")
    await update.callback_query.edit_message_text(
        "لطفاً یکی از ارزهای دیجیتال زیر را انتخاب کنید:",
        reply_markup=get_coin_selection_keyboard(page=0)
    )
    return CHOOSING_COIN



async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    logging.info(f"User {query.from_user.id} pressed button: {query.data}")

    # --- مدیریت دکمه‌های پنل ادمین (مهم: این بخش باید اولین چیزی باشد که بررسی می‌شود) ---
    # این بخش باید قبل از منطق conv_state_history و سایر if/elif ها باشد
    # زیرا دکمه های ادمین توسط ConversationHandler جداگانه ای مدیریت می شوند
    # و نیازی به دستکاری conv_state_history توسط این تابع ندارند.
    if query.data.startswith("admin_"):
        user_id = query.from_user.id
        if user_id not in ADMIN_IDS: # چک مجدد برای اطمینان از دسترسی ادمین
            await query.edit_message_text("شما اجازه دسترسی به این عمل را ندارید.")
            logging.warning(f"Unauthorized access attempt to admin callback by user {user_id} for data: {query.data}")
            return ConversationHandler.END # پایان مکالمه برای کاربران غیر ادمین

        logging.info(f"Admin {user_id} pressed admin button: {query.data}")

        # مهم: این توابع باید ConversationHandler.END یا STATE مربوطه را برگردانند
        # که در توابع ادمین جدید قبلاً پیاده سازی شده اند.
        if query.data == "admin_stats":
            return await admin_stats_command(update, context)
        elif query.data == "admin_manage_balance":
            return await admin_manage_balance_entry(update, context)
        elif query.data == "admin_broadcast":
            return await admin_broadcast_entry(update, context)
        elif query.data == "admin_panel": # برای بازگشت به منوی اصلی ادمین
            return await admin_panel_command(update, context)
        else:
            await query.edit_message_text("گزینه ادمین نامعتبر.")
            return ConversationHandler.END # پایان مکالمه در صورت گزینه نامعتبر

    # --- logic for managing conversation history (بخش اصلی شما) ---
    # دکمه‌هایی که تاریخچه مکالمه را ریست نمی‌کنند یا در حالت‌های خاصی هستند.
    # دکمه‌های ادمین از این لیست حذف شده‌اند زیرا توسط ConversationHandler خودشان مدیریت می‌شوند.
    if query.data not in ['show_balance_portfolio', 'about_bot', 'back_to_main_menu', 'cancel_trade', 'no_op',
                          'coins_page_', 'show_balance_portfolio_from_trade', 'start_trade_new',
                          'confirm_buy', 'confirm_sell_final', 'sell_portfolio_entry', 'invite_friends']:
        if 'conv_state_history' not in context.user_data:
            context.user_data['conv_state_history'] = []
        # فقط در صورتی که مرحله فعلی با دکمه کلیک شده متفاوت باشد، به تاریخچه اضافه کنید
        if 'current_conv_step' in context.user_data and context.user_data['current_conv_step'] != query.data:
            context.user_data['conv_state_history'].append(context.user_data['current_conv_step'])
            logging.debug(
                f"Pushed state: {context.user_data['current_conv_step']}. History: {context.user_data.get('conv_state_history')}")
        context.user_data['current_conv_step'] = query.data
        logging.debug(f"Current step set to: {context.user_data['current_conv_step']}")

    # --- Main Menu Actions ---
    if query.data == 'show_balance_portfolio':
        # این تابع به ConversationHandler.END ختم می‌شود، پس باید آن را برگردانیم
        return await show_balance_and_portfolio(update, context)
    elif query.data == 'start_trade':
        context.user_data['conv_state_history'] = []
        context.user_data['current_conv_step'] = 'start_trade'
        context.user_data['current_coin_page'] = 0
        context.user_data['reconfirmed_price'] = None
        context.user_data['initial_displayed_price'] = None
        return await trade_entry_point(update, context)
    elif query.data == 'sell_portfolio_entry':
        context.user_data['conv_state_history'] = []
        context.user_data['current_conv_step'] = 'sell_portfolio_entry'
        context.user_data['reconfirmed_price'] = None
        context.user_data['initial_displayed_price'] = None
        return await sell_portfolio_entry_point(update, context)
    elif query.data == 'about_bot':
        await about_bot(update, context)
        # اگر 'about_bot' مکالمه را پایان نمی‌دهد، نیازی به return ConversationHandler.END نیست
        # اگر می خواهید به منوی اصلی برگردد، باید تابع back_to_main_menu را فراخوانی کند و ConversationHandler.END را برگرداند
        # یا اگر در یک ConversationHandler والد است، به STATE والد برگردد.
        # فرض می کنیم این یک پیام ساده است و مکالمه را پایان نمی دهد.
        return None # یا STATE فعلی را برگردانید اگر بخشی از ConversationHandler اصلی است

    elif query.data == 'invite_friends':
        await invite_friends_command(update, context)
        return None # مشابه about_bot

    elif query.data == 'back_to_main_menu':
        logging.info(f"User {query.from_user.id} navigating to main menu.")
        # مهم: برای back_to_main_menu، باید مطمئن شویم که ConversationHandler فعلی پایان می یابد.
        # این تابع در fallback ادمین conv handler هم استفاده شده، پس باید ConversationHandler.END برگرداند.
        if 'conv_state_history' in context.user_data:
            context.user_data['conv_state_history'] = [] # تاریخچه را پاک کنید
        if 'current_conv_step' in context.user_data:
            del context.user_data['current_conv_step'] # مرحله فعلی را پاک کنید

        await back_to_main_menu(update, context)
        return ConversationHandler.END # پایان دادن به هر ConversationHandler فعال


    # --- New actions from active trade keyboard (these send new messages) ---
    elif query.data == 'show_balance_portfolio_from_trade':
        # این تابع به ConversationHandler.END ختم می‌شود، پس باید آن را برگردانیم
        return await show_balance_and_portfolio(update, context)
    elif query.data == 'start_trade_new':
        context.user_data['conv_state_history'] = []
        context.user_data['current_conv_step'] = 'start_trade'
        context.user_data['current_coin_page'] = 0
        context.user_data['reconfirmed_price'] = None
        context.user_data['initial_displayed_price'] = None
        await query.message.reply_text(
            "لطفاً یکی از ارزهای دیجیتال زیر را برای معامله جدید انتخاب کنید:",
            reply_markup=get_coin_selection_keyboard(page=0)
        )
        return CHOOSING_COIN

    # --- Coin Selection Pagination ---
    elif query.data.startswith('coins_page_'):
        page = int(query.data.replace('coins_page_', ''))
        context.user_data['current_coin_page'] = page
        logging.info(f"User {query.from_user.id} changed coin page to {page}.")
        await query.edit_message_text(
            "لطفاً یکی از ارزهای دیجیتال زیر را انتخاب کنید:",
            reply_markup=get_coin_selection_keyboard(page)
        )
        return CHOOSING_COIN

    # --- Trade Conversation Actions ---
    elif query.data.startswith('select_coin_'):
        coin_id = query.data.replace('select_coin_', '')
        # top_coins باید در scope این تابع (یا به عنوان متغیر گلوبال) در دسترس باشد.
        selected_coin = next((c for c in top_coins if c['id'] == coin_id), None)
        if selected_coin:
            context.user_data["coin_name"] = selected_coin['name']
            context.user_data["coin_slug"] = selected_coin['id']
            context.user_data["symbol"] = selected_coin['symbol']
            logging.info(f"User {query.from_user.id} selected coin: {selected_coin['symbol']}.")

            current_price = await get_price_from_cache(selected_coin['id'])
            if current_price == 0:
                await query.edit_message_text(
                    "متاسفانه در حال حاضر امکان دریافت قیمت این ارز وجود ندارد. لطفاً ارز دیگری را امتحان کنید.",
                    reply_markup=get_coin_selection_keyboard(context.user_data['current_coin_page'])
                )
                return CHOOSING_COIN

            context.user_data['initial_displayed_price'] = current_price
            context.user_data['current_price'] = current_price

            user_id = query.from_user.id
            available_bal = get_user_available_balance(user_id)

            max_buy_chips = available_bal * MAX_BUY_AMOUNT_PERCENTAGE

            await query.edit_message_text(
                f"💎 ارز انتخاب شده: **{selected_coin['symbol']}**\n"
                f"📈 قیمت فعلی: **${format_price(current_price)}**\n\n"
                f"💰 موجودی قابل استفاده شما: **{available_bal:.2f} چیپ**\n\n"
                f"لطفاً **مقدار چیپ** را که می‌خواهید برای خرید **{selected_coin['symbol']}** استفاده کنید، وارد کنید.\n"
                f"حداقل خرید: **{MIN_BUY_AMOUNT:.2f} چیپ**\n"
                f"حداکثر خرید (تقریبی): **{max_buy_chips:.2f} چیپ** (به دلیل کارمزد ممکن است کمی متفاوت باشد)\n"
                f"مثال: `{MIN_BUY_AMOUNT:.0f}` یا `{math.floor(max_buy_chips):.0f}` (برای خرید تمام موجودی قابل استفاده)",
                parse_mode=telegram.constants.ParseMode.MARKDOWN
            )
            return ENTERING_AMOUNT

    # NEW: Sell Conversation Actions
    elif query.data.startswith('sell_coin_'):
        return await choose_coin_to_sell(update, context) # این تابع باید یک STATE برگرداند.

    elif query.data == 'cancel_trade':
        logging.info(f"User {query.from_user.id} cancelled trade.")
        await cancel_trade(update, context)
        return ConversationHandler.END # پایان مکالمه جاری

    elif query.data == 'back_to_previous_step':
        logging.info(f"User {query.from_user.id} pressed 'back_to_previous_step'.")
        if 'conv_state_history' in context.user_data and context.user_data['conv_state_history']:
            prev_step_data = context.user_data['conv_state_history'].pop()
            logging.info(
                f"Returning to previous step: {prev_step_data}. History remaining: {context.user_data.get('conv_state_history')}")
            context.user_data['reconfirmed_price'] = None
            context.user_data['initial_displayed_price'] = None
            return await revert_to_previous_state(update, context, prev_step_data)
        else:
            logging.info(f"No previous step for user {query.from_user.id}. Returning to main menu.")
            await query.edit_message_text("هیچ مرحله قبلی برای بازگشت وجود ندارد. به منوی اصلی برمی‌گردید.",
                                          reply_markup=get_main_menu_keyboard())
            return ConversationHandler.END # پایان مکالمه


    elif query.data == 'no_op':
        pass # هیچ عملیاتی انجام نمی شود، فقط برای جلوگیری از اخطار در دکمه‌های غیرفعال.

    # اگر هیچکدام از موارد بالا منطبق نبودند و مکالمه در حال اجرا نیست
    return ConversationHandler.END # اگر این تابع به عنوان فال بک در ConversationHandler استفاده می شود.
                                # یا None اگر صرفا یک CallBackQueryHandler مستقل است و انتظار مدیریت ConversationHandler را ندارد.
                                # با توجه به ساختار شما که از آن در ConvHandler استفاده می کنید، END مناسب تر است.

# --- Handler for user's text input (for amount) ---
async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        amount_to_spend = float(update.message.text.replace(',', ''))  # اجازه استفاده از کاما برای جداکننده هزارگان

        user_balance = get_user_available_balance(user_id)
        max_allowed_spend = user_balance * MAX_BUY_AMOUNT_PERCENTAGE

        if amount_to_spend < MIN_BUY_AMOUNT:
            await update.message.reply_text(
                f"مقدار خرید باید حداقل **{MIN_BUY_AMOUNT:.2f} چیپ** باشد. لطفاً مجدداً وارد کنید.",
                reply_markup=get_action_buttons_keyboard(), parse_mode='Markdown')
            return ENTERING_AMOUNT

        if amount_to_spend > max_allowed_spend:
            await update.message.reply_text(
                f"شما نمی‌توانید بیش از **{MAX_BUY_AMOUNT_PERCENTAGE * 100:.0f}%** از موجودی قابل استفاده خود را یکجا خرج کنید.\n"
                f"حداکثر مقدار مجاز برای این خرید: **{max_allowed_spend:.2f} چیپ**.\n"
                "لطفاً مقدار کمتری وارد کنید یا به منوی اصلی بازگردید.",
                reply_markup=get_action_buttons_keyboard(),
                parse_mode='Markdown'
            )
            return ENTERING_AMOUNT

        context.user_data['amount_to_spend'] = amount_to_spend
        symbol = context.user_data['symbol']
        # این قیمت، قیمتی است که در زمان انتخاب ارز گرفته شده بود
        initial_displayed_price = context.user_data['initial_displayed_price']

        # محاسبه مقدار ارزی که با این چیپ‌ها می‌توان خرید (با قیمت اولیه)
        amount_of_coin = amount_to_spend / initial_displayed_price

        # محاسبه کارمزد برای خرید (با قیمت اولیه)
        buy_commission = amount_to_spend * COMMISSION_RATE
        total_cost_with_commission = amount_to_spend + buy_commission

        if total_cost_with_commission > user_balance:  # باید با کل موجودی مقایسه شود، نه max_allowed_spend
            await update.message.reply_text(
                f"با احتساب کارمزد ({(COMMISSION_RATE * 100):.1f}%): **{buy_commission:.2f} چیپ**، موجودی شما کافی نیست. مجموع هزینه: **{total_cost_with_commission:.2f} چیپ**\n"
                "لطفاً مقدار کمتری وارد کنید.",
                reply_markup=get_action_buttons_keyboard(),
                parse_mode='Markdown'
            )
            return ENTERING_AMOUNT

        # ذخیره این مقادیر موقت در user_data
        context.user_data['amount_of_coin_initial'] = amount_of_coin
        context.user_data['buy_commission_initial'] = buy_commission
        context.user_data['total_cost_with_commission_initial'] = total_cost_with_commission

        confirmation_message = (
            f"شما قصد خرید **{amount_of_coin:.6f} واحد** از **{symbol}** را دارید.\n"
            f"با قیمت فعلی: **${format_price(initial_displayed_price)}**\n"  # نمایش قیمت اولیه
            f"مبلغ مورد استفاده از چیپ شما: **{amount_to_spend:.2f} چیپ**\n"
            f"کارمزد خرید ({(COMMISSION_RATE * 100):.1f}%): **{buy_commission:.2f} چیپ**\n"
            f"مجموع هزینه: **{total_cost_with_commission:.2f} چیپ**\n\n"
            "آیا تأیید می‌کنید؟"
        )
        await update.message.reply_text(
            confirmation_message,
            reply_markup=get_confirm_buy_keyboard(),
            parse_mode='Markdown'
        )
        return CONFIRM_BUY

    except ValueError:
        await update.message.reply_text("لطفاً یک عدد معتبر برای مقدار چیپ وارد کنید.",
                                        reply_markup=get_action_buttons_keyboard())
        return ENTERING_AMOUNT
    except Exception as e:
        logging.error(f"Error handling amount input for user {user_id}: {e}")
        await update.message.reply_text("خطایی رخ داد. لطفاً دوباره تلاش کنید.",
                                        reply_markup=get_action_buttons_keyboard())
        return ConversationHandler.END

def get_top_users(limit=10):
    conn = sqlite3.connect('trade.db')
    c = conn.cursor()
    c.execute('SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?', (limit,))
    results = c.fetchall()
    conn.close()
    return results


# فرض می‌کنیم این‌ها در جای دیگری از کد شما تعریف شده‌اند:
# COMMISSION_RATE
# get_price_from_cache
# get_user (تابعی که اطلاعات کاربر را از دیتابیس می‌خواند)
# add_bot_commission (تابعی برای اضافه کردن کارمزد به موجودی ربات)
# get_trade_active_keyboard (یا هر کیبورد مناسب برای بعد از بسته شدن پوزیشن)
# conn (اتصال به دیتابیس)
# cursor (کورسر دیتابیس)
# EPSILON (یک مقدار کوچک برای مقایسه اعداد فلوت، مثلاً 1e-7)


async def monitor_tpsl_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    وظیفه‌ای که به صورت دوره‌ای اجرا می‌شود تا پوزیشن‌های باز کاربران را برای TP/SL بررسی کند.
    """
    logging.info("Running TP/SL monitor job.")

    try:
        # 1. دریافت تمام پوزیشن‌های باز که TP یا SL برای آن‌ها تنظیم شده است (مقدار > EPSILON)
        # از coin_slug هم در SELECT استفاده می‌کنیم
        cursor.execute(
            "SELECT position_id, user_id, coin_slug, symbol, amount, buy_price, tp_price, sl_price FROM user_positions WHERE status='open' AND (tp_price > ? OR sl_price > ?)",
            (EPSILON, EPSILON,)  # EPSILON را به عنوان پارامتر به کوئری بفرستید
        )
        open_tpsl_positions = cursor.fetchall()

        for pos_id, user_id, coin_slug_from_db, symbol, amount, buy_price, tp_price, sl_price in open_tpsl_positions:
            actual_coin_slug = coin_slug_from_db

            # اگر coin_slug در دیتابیس NULL بود، سعی کنید آن را از SYMBOL_TO_SLUG_MAP بگیرید.
            # این برای سازگاری با رکوردهای قدیمی که شاید slug نداشتند مفید است.
            if not actual_coin_slug:
                logging.warning(
                    f"coin_slug is NULL for position {pos_id} (Symbol: {symbol}). Attempting to derive from SYMBOL_TO_SLUG_MAP.")
                actual_coin_slug = SYMBOL_TO_SLUG_MAP.get(symbol)
                if not actual_coin_slug:
                    logging.error(
                        f"Could not determine coin_slug for {symbol} for position {pos_id}. Skipping TP/SL check.")
                    continue  # به پوزیشن بعدی می‌رویم

            # 2. دریافت قیمت لحظه‌ای ارز از کش (با استفاده از actual_coin_slug)
            current_price = await get_price_from_cache(actual_coin_slug)
            if current_price == 0:
                logging.warning(
                    f"Could not get current price from cache for {actual_coin_slug} during TP/SL monitor for position {pos_id}. Skipping.")
                continue  # به پوزیشن بعدی می‌رویم

            action_taken = None
            pnl = 0.0
            commission = 0.0
            net_revenue = 0.0
            close_price = 0.0  # قیمت بسته‌شدن نهایی

            # 3. بررسی Take Profit
            # اگر TP تنظیم شده و قیمت فعلی >= TP است
            if tp_price is not None and tp_price > EPSILON and current_price >= tp_price:
                action_taken = "حد سود (TP)"
                close_price = tp_price
                logging.info(
                    f"Position {pos_id} for user {user_id} hit Take Profit at ${format_price(tp_price)}. Current price: ${format_price(current_price)}")

                # محاسبه سود/زیان و کارمزد برای بستن پوزیشن
                gross_revenue = amount * tp_price
                commission = gross_revenue * COMMISSION_RATE
                net_revenue = gross_revenue - commission
                pnl = net_revenue - (amount * buy_price)

            # 4. بررسی Stop Loss
            # اگر SL تنظیم شده و قیمت فعلی <= SL است
            elif sl_price is not None and sl_price > EPSILON and current_price <= sl_price:
                action_taken = "حد ضرر (SL)"
                close_price = sl_price
                logging.info(
                    f"Position {pos_id} for user {user_id} hit Stop Loss at ${format_price(sl_price)}. Current price: ${format_price(current_price)}")

                # محاسبه سود/زیان و کارمزد برای بستن پوزیشن
                gross_revenue = amount * sl_price
                commission = gross_revenue * COMMISSION_RATE
                net_revenue = gross_revenue - commission  # درآمد خالص از فروش
                pnl = net_revenue - (amount * buy_price)  # سود یا زیان

            if action_taken:  # اگر پوزیشن به TP یا SL رسید
                try:
                    # 5. بستن پوزیشن در دیتابیس
                    # tp_price و sl_price را به 0.0 تنظیم می‌کنیم (بجای NULL) برای سازگاری بهتر با DEFAULT 0.0
                    cursor.execute(
                        "UPDATE user_positions SET status='closed', closed_price=?, close_timestamp=?, profit_loss=?, commission_paid = COALESCE(commission_paid, 0) + ?, tp_price = ?, sl_price = ? WHERE position_id=?",
                        (close_price, datetime.datetime.now().isoformat(), pnl, commission, 0.0, 0.0, pos_id)
                    )
                    conn.commit()
                    logging.info(f"Position {pos_id} updated to closed, PnL: {pnl:+.2f}. TP/SL prices cleared to 0.0.")

                    # 6. به‌روزرسانی موجودی کاربر و PnL در جدول users
                    user_db_data = get_user(user_id)
                    if user_db_data:
                        current_user_balance = user_db_data["balance"]
                        current_total_realized_pnl = user_db_data["total_realized_pnl"]
                        current_monthly_realized_pnl = user_db_data["monthly_realized_pnl"]
                        current_user_commission_balance = user_db_data["user_commission_balance"]

                        new_user_balance = current_user_balance + net_revenue
                        new_total_realized_pnl = current_total_realized_pnl + pnl
                        new_monthly_realized_pnl = current_monthly_realized_pnl + pnl
                        new_user_commission_balance = current_user_commission_balance + commission

                        cursor.execute("""
                                       UPDATE users
                                       SET balance                 = ?,
                                           total_realized_pnl      = ?,
                                           monthly_realized_pnl    = ?,
                                           user_commission_balance = ?
                                       WHERE user_id = ?
                                       """,
                                       (new_user_balance, new_total_realized_pnl, new_monthly_realized_pnl,
                                        new_user_commission_balance, user_id))
                        conn.commit()
                        logging.info(f"User {user_id} balance and PnL updated. New balance: {new_user_balance:.2f}.")

                        # 7. اضافه کردن کارمزد به موجودی ربات
                        add_bot_commission(commission)
                        logging.info(f"Commission {commission:.2f} added to bot balance.")

                        # --- 8. ارسال نوتیفیکیشن مناسب به کاربر ---
                        message_text = (
                            f"🔔 **پوزیشن شما به صورت خودکار بسته شد!** 🔔\n\n"
                            f"💎 ارز: **{symbol}**\n"
                            f"📊 نوع بسته شدن: **{action_taken}**\n"
                            f"📈 قیمت بسته‌شدن: **${format_price(close_price)}**\n"
                            f"🔢 مقدار: **{amount:.6f} واحد**\n"
                            f"💸 کارمزد: **{commission:.2f} چیپ**\n"
                            f"💰 سود/زیان این پوزیشن: **{pnl:+.2f} چیپ**\n\n"
                            f"موجودی فعلی شما: **{new_user_balance:.2f} چیپ**"
                        )

                        try:
                            # دریافت chat_id کاربر از دیتابیس
                            chat_id_data = cursor.execute("SELECT chat_id FROM users WHERE user_id = ?",
                                                          (user_id,)).fetchone()

                            if chat_id_data and chat_id_data[0]:
                                user_chat_id = chat_id_data[0]
                                logging.debug(
                                    f"Attempting to send message to user {user_id} with chat_id {user_chat_id}.")
                                await context.bot.send_message(
                                    chat_id=user_chat_id,
                                    text=message_text,
                                    parse_mode='Markdown',
                                    # اگر get_trade_active_keyboard تعریف نشده، از get_main_menu_keyboard استفاده کنید
                                    reply_markup=get_trade_active_keyboard()
                                    # یا get_trade_active_keyboard() اگر تعریف شده است
                                )
                                logging.info(
                                    f"Successfully sent TP/SL notification to user {user_id} for position {pos_id}.")
                            else:
                                logging.warning(
                                    f"No valid chat_id found for user {user_id}. Cannot send TP/SL notification for position {pos_id}.")
                        except telegram.error.Unauthorized:
                            logging.warning(
                                f"Bot unauthorized to send message to user {user_id}. User may have blocked the bot. (Position ID: {pos_id})")
                        except telegram.error.BadRequest as e:
                            logging.error(
                                f"BadRequest error sending TP/SL notification to user {user_id} for position {pos_id}: {e}. Check Markdown formatting.")
                        except Exception as e:
                            logging.error(
                                f"Unexpected error sending TP/SL notification to user {user_id} for position {pos_id}: {e}")
                        # --- پایان بخش ارسال نوتیفیکیشن ---

                    else:
                        logging.error(
                            f"User {user_id} not found in DB during TP/SL update for position {pos_id}. Skipping user balance update.")

                except Exception as e:
                    # اگر در پردازش یک پوزیشن خطایی رخ داد، تغییرات مربوط به آن پوزیشن را برگردانید.
                    conn.rollback()
                    logging.error(f"CRITICAL ERROR processing TP/SL for position {pos_id} for user {user_id}: {e}")

    except Exception as e:
        logging.error(f"An error occurred in the main loop of monitor_tpsl_jobs: {e}")

    logging.info("TP/SL monitor job finished.")



async def process_buy_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    سفارش خرید ارز دیجیتال را پردازش و نهایی می‌کند.
    شامل بازبینی قیمت، بررسی موجودی، تأیید مجدد در صورت تغییر قیمت،
    ثبت پوزیشن در دیتابیس و به‌روزرسانی موجودی کاربر.
    همچنین سطح VIP کاربر را پس از خرید بررسی و ارتقاء می‌دهد.
    برای کاربران VIP، به مرحله تنظیم TP/SL هدایت می‌کند.
    """
    query = update.callback_query
    await query.answer()  # مهم: برای پاسخ به CallbackQuery
    user_id = update.effective_user.id
    # --- تغییرات جدید: دریافت username ---
    username = update.effective_user.username if update.effective_user.username else None
    # --- پایان تغییرات جدید ---

    symbol = context.user_data.get('symbol')
    coin_slug = context.user_data.get('coin_slug')
    amount_to_spend_initial = context.user_data.get('amount_to_spend')  # مقدار چیپی که کاربر وارد کرد

    # بررسی وجود داده‌های ضروری در context
    if not all([symbol, coin_slug, amount_to_spend_initial is not None]):
        logging.error(
            f"Missing essential context data for process_buy_order for user {user_id}. Symbol: {symbol}, Slug: {coin_slug}, Amount: {amount_to_spend_initial}")
        await query.edit_message_text(
            "خطا در پردازش اطلاعات خرید شما. لطفاً از ابتدا تلاش کنید.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode='Markdown'
        )
        # پاک کردن context.user_data مربوط به خرید در صورت خطا
        context.user_data.pop('reconfirmed_price', None)
        context.user_data.pop('initial_displayed_price', None)
        context.user_data.pop('amount_to_spend', None)
        context.user_data.pop('symbol', None)
        context.user_data.pop('reconfirmed_price', None)
        context.user_data.pop('initial_displayed_price', None)
        context.user_data.pop('amount_to_spend', None)
        context.user_data.pop('symbol', None)
        context.user_data.pop('coin_slug', None)
        context.user_data.pop('current_state', None) # اضافه شده
        return ConversationHandler.END

    # **گام جدید: بازبینی قیمت لحظه ای از کش**
    rechecked_price = await get_price_from_cache(coin_slug)  # استفاده از کش
    if rechecked_price == 0:
        await query.edit_message_text(
            "متاسفانه در حال حاضر امکان دریافت قیمت این ارز وجود ندارد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_main_menu_keyboard(),  # بازگشت به منوی اصلی در صورت خطا
            parse_mode='Markdown'
        )
        logging.error(f"Failed to re-check price from cache for {coin_slug} during buy order for user {user_id}.")
        # پاک کردن context.user_data مربوط به خرید در صورت خطا
        context.user_data.pop('reconfirmed_price', None)
        context.user_data.pop('initial_displayed_price', None)
        context.user_data.pop('amount_to_spend', None)
        context.user_data.pop('symbol', None)
        context.user_data.pop('coin_slug', None)
        context.user_data.pop('current_state', None) # اضافه شده
        return ConversationHandler.END

    # قیمت جدید برای انجام معامله
    new_current_price = rechecked_price

    # محاسبه مجدد مقدار ارز و کارمزد با قیمت جدید
    new_amount_of_coin = amount_to_spend_initial / new_current_price
    new_buy_commission = amount_to_spend_initial * COMMISSION_RATE
    new_total_cost_with_commission = amount_to_spend_initial + new_buy_commission

    current_user_balance = get_user_available_balance(user_id)  # استفاده از موجودی قابل استفاده

    # اگر موجودی کاربر برای قیمت جدید (با احتساب کارمزد) کافی نبود
    if new_total_cost_with_commission > current_user_balance:
        await query.edit_message_text(
            "متاسفانه موجودی شما برای انجام این معامله کافی نیست (با احتساب کارمزد جدید). لطفاً مجدداً تلاش کنید.",
            reply_markup=get_main_menu_keyboard(),  # بازگشت به منوی اصلی در صورت خطا
            parse_mode='Markdown'
        )
        logging.warning(
            f"User {user_id} tried to buy but balance was insufficient with new price: {current_user_balance:.2f} vs {new_total_cost_with_commission:.2f}")
        # پاک کردن context.user_data مربوط به خرید در صورت خطا
        context.user_data.pop('reconfirmed_price', None)
        context.user_data.pop('initial_displayed_price', None)
        context.user_data.pop('amount_to_spend', None)
        context.user_data.pop('symbol', None)
        context.user_data.pop('coin_slug', None)
        context.user_data.pop('current_state', None) # اضافه شده
        return ConversationHandler.END

    # بررسی اینکه آیا قیمت تغییر کرده یا کاربر قبلاً تأیید کرده (در حالت RECONFIRM_BUY)
    initial_displayed_price = context.user_data.get('initial_displayed_price')

    # اگر initial_displayed_price وجود نداشته باشد یا صفر باشد، تفاوت را بالا در نظر می‌گیریم تا reconfirm شود
    # در این حالت، اگر initial_displayed_price نامعتبر بود، فرض می‌کنیم نیاز به reconfirmation است.
    if initial_displayed_price is None or initial_displayed_price == 0:
        price_diff_percent = 100  # فرض می‌کنیم تغییر قابل توجهی وجود دارد تا به مرحله تایید مجدد برویم
    else:
        # مطمئن شوید PRICE_CHANGE_THRESHOLD_PERCENT تعریف شده است
        # price_diff_percent = abs(new_current_price - initial_displayed_price) / initial_displayed_price * 100 # این خط اگر تعریف نشده باشد، خطا می‌دهد
        # بهتر است بررسی کنید که initial_displayed_price صفر نباشد تا از تقسیم بر صفر جلوگیری کنید.
        if initial_displayed_price != 0:
            price_diff_percent = abs(new_current_price - initial_displayed_price) / initial_displayed_price * 100
        else:
            price_diff_percent = 100 # اگر قیمت اولیه صفر بود، فرض می‌کنیم تغییر زیادی هست تا مجدداً تایید شود.


    price_has_changed = price_diff_percent >= PRICE_CHANGE_THRESHOLD_PERCENT # فرض بر تعریف شدن این ثابت

    # تنها در صورتی که قیمت تغییر کرده و کاربر هنوز تأیید مجدد نکرده بود
    # یا اینکه در مرحله RECONFIRM_BUY نیستیم و قیمت تغییر کرده.
    # و اطمینان حاصل می‌کنیم که reconfirmed_price برابر با new_current_price نباشد (یعنی قبلاً تایید نشده باشد)
    if price_has_changed and context.user_data.get('reconfirmed_price') != new_current_price:
        context.user_data['initial_displayed_price'] = new_current_price  # قیمت را برای نمایش بعدی به روزرسانی می‌کنیم

        # اگر کاربر در حالت RECONFIRM_BUY نیست، آن را به RECONFIRM_BUY تغییر می‌دهیم
        # این جلوگیری می‌کند از اینکه اگر کاربر از دکمه "بله" در RECONFIRM_BUY استفاده کرد، دوباره اینجا گرفتار شود.
        if context.user_data.get('current_state') != RECONFIRM_BUY:  # فرض می‌کنیم یک 'current_state' در context دارید
            reconfirmation_message = (
                f"⚠️ **قیمت {symbol} تغییر کرده است!** ⚠️\n\n"
                f"📈 قیمت قبلی: **${format_price(initial_displayed_price)}**\n"
                f"📈 **قیمت جدید: ${format_price(new_current_price)}**\n"
                f"**تغییر: {format_price(price_diff_percent)}%**\n\n" # فرمت کردن درصد
                f"شما قصد خرید **{new_amount_of_coin:.6f} واحد** از **{symbol}** را دارید.\n"
                f"مبلغ مورد استفاده از چیپ شما: **{amount_to_spend_initial:.2f} چیپ**\n"
                f"کارمزد خرید ({(COMMISSION_RATE * 100):.1f}%): **{new_buy_commission:.2f} چیپ**\n"
                f"مجموع هزینه: **{new_total_cost_with_commission:.2f} چیپ**\n\n"
                "آیا با **قیمت جدید** موافقید و خرید را تأیید می‌کنید؟"
            )

            await query.edit_message_text(
                reconfirmation_message,
                reply_markup=get_confirm_buy_keyboard(), # فرض بر تعریف شدن این تابع
                parse_mode='Markdown'
            )
            context.user_data['reconfirmed_price'] = new_current_price
            context.user_data['current_state'] = RECONFIRM_BUY  # ذخیره حالت فعلی
            return RECONFIRM_BUY
        # اگر در حالت RECONFIRM_BUY بود و همان قیمت تأیید شده بود، ادامه می‌دهیم به مرحله بعدی.
        # این قسمت برای زمانی است که کاربر دکمه "بله" را در مرحله RECONFIRM_BUY فشار می‌دهد.

    logging.info(
        f"User {user_id} proceeding with final BUY for {new_amount_of_coin:.6f} {symbol} at ${new_current_price:.4f} with {amount_to_spend_initial:.2f} chips. Reconfirmed: {context.user_data.get('reconfirmed_price') == new_current_price}.")

    # کسر مبلغ کل (مبلغ خرید + کارمزد) از موجودی کاربر
    new_user_balance = current_user_balance - new_total_cost_with_commission
    update_balance(user_id, new_user_balance) # فرض بر تعریف شدن این تابع

    # اضافه کردن کارمزد به موجودی ربات
    add_bot_commission(new_buy_commission) # فرض بر تعریف شدن این تابع
    add_user_commission(user_id, new_buy_commission) # فرض بر تعریف شدن این تابع

    # ثبت پوزیشن خرید در دیتابیس (TP/SL فعلاً 0.0 خواهند بود، بعداً آپدیت می‌شوند)
    # --- تغییرات جدید: ارسال username به تابع save_buy_position ---
    position_id = save_buy_position(user_id, username, symbol, new_amount_of_coin, new_current_price, new_buy_commission, coin_slug) # تابع save_buy_position را نیز باید به‌روز کنید
    # --- پایان تغییرات جدید ---
    logging.info(
        f"Buy position {position_id} saved for user {user_id} ({username}): {new_amount_of_coin:.6f} {symbol} at ${new_current_price:.4f}")

    # --- ذخیره اطلاعات کوین و مقدار خرید برای استفاده در TP/SL ---
    context.user_data['selected_coin_symbol'] = symbol
    context.user_data['final_bought_amount'] = new_amount_of_coin

    # --- بررسی و ارتقاء سطح VIP کاربر ---
    # این تابع سطح VIP کاربر را بر اساس ارزش پورتفوی بررسی می‌کند
    # و در صورت نیاز، آن را ارتقاء داده و پیام تبریک VIP ارسال می‌کند.
    await check_and_upgrade_vip_level(user_id, context) # فرض بر تعریف شدن این تابع

    # --- تعیین اینکه آیا کاربر VIP است و باید TP/SL را تنظیم کند ---
    # این خط VIP بودن کاربر رو از دیتابیس می‌خونه
    conn = get_db_connection() # اتصال به دیتابیس
    cursor = conn.cursor()
    cursor.execute("SELECT vip_level FROM users WHERE user_id = ?", (user_id,))
    user_vip_level_data = cursor.fetchone()
    conn.close() # بستن اتصال
    # اگر داده‌ای یافت نشد، فرض می‌کنیم کاربر عادی است (سطح 0)
    user_vip_level = user_vip_level_data[0] if user_vip_level_data else 0

    is_vip_user = user_vip_level > 0  # فرض می‌کنیم VIP_LEVELS[0] همان کاربر عادی است. پس هر سطحی بالاتر از 0 VIP است.

    # اضافه کردن لاگ‌های کمکی برای دیباگ
    logging.info(f"User {user_id} has VIP level: {user_vip_level}. Is considered VIP for TP/SL: {is_vip_user}")

    final_message_text = (
        f"✅ **خرید شما با موفقیت انجام شد!** ✅\n\n"
        f"💎 ارز: **{symbol}**\n"
        f"📈 قیمت خرید: **${format_price(new_current_price)}**\n"
        f"🔢 مقدار خریداری شده: **{new_amount_of_coin:.6f} واحد**\n"
        f"💸 کارمزد پرداخت شده: **{new_buy_commission:.2f} چیپ**\n"
        f"💰 موجودی فعلی شما: **{new_user_balance:.2f} چیپ**\n\n"
    )

    if is_vip_user:
        final_message_text += (
            "✨ به عنوان یک کاربر VIP، می‌توانید برای این پوزیشن **حد سود (TP)** و **حد ضرر (SL)** تعیین کنید.\n"
            "آیا می‌خواهید این سطوح را تنظیم کنید؟"
        )
        context.user_data['current_position_id_for_tpsl'] = position_id
        # ذخیره قیمت خرید برای استفاده در مراحل TP/SL
        context.user_data['current_buy_price_for_tpsl'] = new_current_price
        context.user_data['tpsl_step'] = 'tp'  # برای شروع فرآیند TP/SL
        keyboard = get_tpsl_choice_keyboard() # فرض بر تعریف شدن این تابع
        return_state = ASKING_TP_SL  # اینجا حالت مکالمه به ASKING_TP_SL تغییر می‌کند
    else:
        final_message_text += "اکنون می‌توانید موجودی خود را بررسی کنید یا معامله جدیدی شروع کنید."
        keyboard = get_main_menu_keyboard()  # فرض بر تعریف شدن این تابع
        return_state = ConversationHandler.END  # اگر VIP نیست، مکالمه به پایان می‌رسد

    try:
        logging.info(f"Attempting to edit message with final BUY confirmation for user {user_id}. Markup: {keyboard}")
        await query.edit_message_text(
            final_message_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        logging.info(f"Successfully edited message for final BUY confirmation for user {user_id}.")
    except telegram.error.BadRequest as e:
        logging.warning(
            f"Failed to edit message for successful buy order {position_id}: {e}. Sending new message instead. Markup: {keyboard}")
        await query.message.reply_text(  # از query.message.reply_text استفاده کنید
            final_message_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        logging.info(f"Successfully sent new message for final BUY confirmation for user {user_id}.")
    except Exception as e:
        logging.error(f"Unexpected error sending final buy message for user {user_id}: {e}")
        await query.message.reply_text(
            "خرید شما انجام شد، اما مشکلی در ارسال پیام تأیید رخ داد. لطفاً موجودی خود را بررسی کنید.",
            reply_markup=keyboard
        )
    finally:
        # پاک کردن context.user_data مربوط به خرید
        context.user_data.pop('reconfirmed_price', None)
        context.user_data.pop('initial_displayed_price', None)
        context.user_data.pop('amount_to_spend', None)
        context.user_data.pop('symbol', None)
        context.user_data.pop('coin_slug', None)
        context.user_data.pop('current_state', None)  # پاک کردن حالت فعلی

    return return_state  # تابع باید return_state را برگرداند


async def help_command(update: Update, context):
    """هندلر کامند /help"""
    help_text = (
        "لیست دستورات:\n"
        "/start - شروع به کار ربات و پیام خوش‌آمدگویی\n"
        "/mybalance - نمایش موجودی چیپ‌های مجازی شما\n"
        "/trade - شروع یک معامله جدید (خرید/فروش)\n"
        "/help - نمایش همین راهنما\n"
        "/top - نمایش رتبه‌بندی برترین تریدرها\n"
        "/rules - نمایش قوانین و مقررات ربات\n\n"
        "برای هرگونه سوال یا پیشنهاد، با ما در ارتباط باشید."
    )
    await update.message.reply_text(help_text)


async def get_full_portfolio_data(user_id: int):
    """
    تمام داده‌های پورتفو برای یک کاربر را شامل پوزیشن‌ها، قیمت‌های فعلی،
    مجموع ارزش پورتفو و مجموع سرمایه اولیه را محاسبه و برمی‌گرداند.
    """
    total_portfolio_value = 0.0
    total_invested_value = 0.0

    # متغیرها برای ذخیره داده‌های خام جهت نمایش جزئیات
    grouped_positions = get_open_positions_grouped(user_id)
    current_prices = {}  # مقداردهی اولیه

    if grouped_positions:
        coin_slugs_in_portfolio = []
        for pos in grouped_positions:
            # top_coins باید در scope این تابع (یا گلوبال) در دسترس باشد.
            coin_info = next((c for c in top_coins if c['symbol'] == pos['symbol']), None)
            if coin_info:
                coin_slugs_in_portfolio.append(coin_info['id'])

        # get_prices_for_portfolio_from_cache باید در scope این تابع (یا گلوبال) در دسترس باشد.
        current_prices = await get_prices_for_portfolio_from_cache(coin_slugs_in_portfolio)

        for pos in grouped_positions:
            symbol = pos['symbol']
            amount = pos['amount']
            buy_price = pos['buy_price']

            coin_info = next((c for c in top_coins if c['symbol'] == symbol), None)
            coin_slug = coin_info['id'] if coin_info else None

            current_price = current_prices.get(coin_slug, 0) if coin_slug else 0

            # محاسبه مجموع‌ها تنها در صورت وجود قیمت معتبر
            if current_price > 0:
                current_value = amount * current_price
                invested_value = amount * buy_price

                total_portfolio_value += current_value
                total_invested_value += invested_value

    return {
        "grouped_positions": grouped_positions,
        "current_prices": current_prices,
        "total_portfolio_value": total_portfolio_value,
        "total_invested_value": total_invested_value
    }


# --- HELPER FUNCTION FOR DATE PARSING (این قسمت را به بالای فایل خود اضافه کنید) ---
def parse_date_robustly(date_string):
    """
    Parses a date string, attempting to handle different formats including microseconds.
    """
    if not date_string:
        return None

    formats_to_try = [
        '%Y-%m-%d %H:%M:%S.%f',  # With microseconds
        '%Y-%m-%d %H:%M:%S',  # Without microseconds
    ]
    for fmt in formats_to_try:
        try:
            return datetime.datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    logging.warning(f"Failed to parse date string with known formats: '{date_string}'.")
    return None


# --- پایان بخش HELPER FUNCTION ---


# ... (بقیه کدهای شما قبل از show_balance_and_portfolio) ...


async def show_balance_and_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"User {user_id} entered show_balance_and_portfolio.")

    # --- پاک کردن تاریخچه مکالمه و تنظیم وضعیت فعلی ---
    context.user_data['conv_state_history'] = []
    context.user_data['current_conv_step'] = 'main_menu'
    logging.info(f"User {user_id}: conv_state_history and current_conv_step reset in show_balance_and_portfolio.")
    # --- پایان پاکسازی ---

    # --- Fetch user data including realized PnL ---
    cursor.execute(
        "SELECT balance, total_realized_pnl, monthly_realized_pnl, last_monthly_reset_date FROM users WHERE user_id = ?",
        (user_id,))
    user_db_data = cursor.fetchone()

    # اگر کاربر در دیتابیس پیدا نشد (کاربر جدید)، مقادیر پیش‌فرض را تنظیم کنید و کاربر را اضافه کنید
    if not user_db_data:
        add_user_if_not_exists(user_id)
        user_balance = INITIAL_USER_BALANCE
        total_realized_pnl = 0.0
        monthly_realized_pnl = 0.0
        last_reset_date_str = None
    else:
        user_balance = user_db_data[0]
        total_realized_pnl = user_db_data[1] if user_db_data[1] is not None else 0.0
        monthly_realized_pnl = user_db_data[2] if user_db_data[2] is not None else 0.0
        last_reset_date_str = user_db_data[3]

    # --- Logic for Monthly PnL Reset ---
    now = datetime.datetime.now()
    current_month_str = now.strftime('%Y-%m')

    should_reset_monthly = False

    # --- خطایابی و اصلاح فرمت تاریخ برای last_monthly_reset_date ---
    last_reset_date = None
    if last_reset_date_str:
        last_reset_date = parse_date_robustly(last_reset_date_str)
        if last_reset_date is None:  # اگر parse_date_robustly هم نتوانست تاریخ را تشخیص دهد
            logging.warning(
                f"فرمت تاریخ نامعتبر برای last_monthly_reset_date برای کاربر {user_id}: '{last_reset_date_str}'. به عنوان ریست اولیه در نظر گرفته می‌شود.")
            should_reset_monthly = True  # فرض می کنیم تاریخ مشکل دارد و باید ریست شود
        elif last_reset_date.strftime('%Y-%m') != current_month_str:
            should_reset_monthly = True
    else:  # اگر last_monthly_reset_date اصلا وجود نداشت
        should_reset_monthly = True

    if should_reset_monthly:
        logging.info(f"Resetting monthly_realized_pnl for user {user_id} for new month or initial setup.")
        monthly_realized_pnl = 0.0
        # تاریخ جدید را با فرمت استاندارد بدون میلی‌ثانیه ذخیره می‌کنیم
        new_last_reset_date_str = now.strftime('%Y-%m-%d %H:%M:%S')

        cursor.execute("UPDATE users SET monthly_realized_pnl = ?, last_monthly_reset_date = ? WHERE user_id = ?",
                       (monthly_realized_pnl, new_last_reset_date_str, user_id))
        conn.commit()

    # --- End of Monthly PnL Reset Logic ---

    # --- فراخوانی تابع جدید برای دریافت تمام داده‌های پورتفو ---
    portfolio_data = await get_full_portfolio_data(user_id)
    grouped_positions = portfolio_data["grouped_positions"]
    current_prices = portfolio_data["current_prices"]
    total_portfolio_value = portfolio_data["total_portfolio_value"]
    total_invested_value = portfolio_data["total_invested_value"]

    # --- محاسبه موجودی قابل استفاده ---
    available_balance = get_user_available_balance(user_id)

    # --- ساخت متن پاسخ ---
    text = f"💰 **موجودی و پورتفوی شما**\n\n"
    text += f"موجودی فعلی شما: **{user_balance + total_portfolio_value:.2f} چیپ**\n"
    text += f"موجودی قابل استفاده برای معاملات جدید: **{available_balance:.2f} چیپ**\n"

    # --- بخش پورتفوی فعال برای نمایش ---
    MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD = 0.1

    if grouped_positions:
        text += "\n--- **پورتفوی فعال** ---\n"

        displayed_coins_count = 0

        for pos in grouped_positions:
            symbol = pos['symbol']
            amount = pos['amount']
            buy_price = pos['buy_price']

            coin_info = next((c for c in top_coins if c['symbol'] == symbol), None)
            coin_slug = coin_info['id'] if coin_info else None

            current_price = current_prices.get(coin_slug, 0) if coin_slug else 0

            current_value = amount * current_price
            if current_value < MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD and current_value > 0:
                logging.info(
                    f"Skipping display of {symbol} (value: {current_value:.4f}) as it's below display threshold.)"
                )
                continue

            if current_price > 0:
                invested_value = amount * buy_price
                profit_loss = current_value - invested_value
                profit_loss_percent = (profit_loss / invested_value) * 100 if invested_value > 0 else 0

                pnl_emoji = "📈" if profit_loss >= 0 else "📉"

                text += (
                    f"\n💎 **{symbol}**\n"
                    f"  🔢 مقدار: {amount:.6f}\n"
                    f"  💲 میانگین قیمت خرید: ${format_price(buy_price)}\n"
                    f"  💲 قیمت فعلی: ${format_price(current_price)}\n"
                    f"  💰 ارزش فعلی: {current_value:.2f} چیپ\n"
                    f"  {pnl_emoji} سود/زیان: {profit_loss:+.2f} چیپ ({profit_loss_percent:+.2f}%)"
                )
                displayed_coins_count += 1
            else:
                text += (
                    f"\n💎 **{symbol}**\n"
                    f"  🔢 مقدار: {amount:.6f}\n"
                    f"  💲 میانگین قیمت خرید: ${format_price(buy_price)}\n"
                    f"  💲 قیمت فعلی: N/A\n"
                    f"  💰 ارزش فعلی: N/A\n"
                    f"  📈 سود/زیان: N/A (خطا در دریافت قیمت لحظه‌ای از کش)\n"
                )
                displayed_coins_count += 1
                logging.warning(f"Could not get current price for {symbol} ({coin_slug}) for portfolio display.")

        if displayed_coins_count == 0:
            text += "\n*شما هیچ پوزیشن بازی ندارید یا ارزش آن‌ها بسیار ناچیز است.*"

        overall_profit_loss = total_portfolio_value - total_invested_value
        overall_profit_loss_percent = (
                                              overall_profit_loss / total_invested_value) * 100 if total_invested_value > 0 else 0

        overall_pnl_emoji = "📈" if overall_profit_loss >= 0 else "📉"

        text += "\n\n--- **خلاصه پورتفو** ---\n"
        text += f"مجموع سرمایه فعال: **{total_invested_value:.2f} چیپ**\n"
        text += f"مجموع ارزش فعلی: **{total_portfolio_value:.2f} چیپ**\n"
        text += f"{overall_pnl_emoji} **کل سود/زیان پورتفو: {overall_profit_loss:+.2f} چیپ ({overall_profit_loss_percent:+.2f}%)**\n"
    else:
        text += "\n*شما هیچ پوزیشن بازی ندارید. با شروع معامله، پورتفوی شما نمایش داده خواهد شد.*"

    # --- Display Realized PnL ---
    total_realized_pnl_emoji = "📈" if total_realized_pnl >= 0 else "📉"
    monthly_realized_pnl_emoji = "📈" if monthly_realized_pnl >= 0 else "📉"

    text += "\n\n--- **سود/زیان معاملات بسته شده** ---\n"
    text += f"{total_realized_pnl_emoji} سود/زیان کلی (تمام معاملات بسته شده): **{total_realized_pnl:+.2f} چیپ**\n"
    text += f"{monthly_realized_pnl_emoji} سود/زیان ماهانه (از ابتدای {now.strftime('%B')}): **{monthly_realized_pnl:+.2f} چیپ**\n"

    # --- نمایش کارمزد پرداخت شده توسط کاربر ---
    user_commission = get_user_commission_balance(user_id)
    text += f"\n\n**کل کارمزد پرداخت شده توسط کاربر: {user_commission:.2f} چیپ**"

    logging.info(f"User {user_id} finished preparing balance and portfolio message.")

    # تصمیم‌گیری برای ویرایش یا ارسال پیام جدید
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=get_action_buttons_keyboard(),
                parse_mode='Markdown'
            )
            logging.info(f"User {user_id}: Edited message for portfolio display.")
        except Exception as e:
            logging.error(
                f"User {user_id}: Failed to edit portfolio message (likely BadRequest). Sending new message. Error: {e}")
            await update.callback_query.message.reply_text(
                text,
                reply_markup=get_action_buttons_keyboard(),
                parse_mode='Markdown'
            )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=get_action_buttons_keyboard(),
            parse_mode='Markdown'
        )
    else:
        logging.error(f"Cannot send portfolio message: neither callback_query nor message found for user {user_id}")

    # اگر این تابع بخشی از یک ConversationHandler بزرگتر است و هدف آن پایان دادن به یک مکالمه است:
    # return ConversationHandler.END # یا وضعیت مناسب بعدی


async def show_balance_and_portfolio_from_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logging.info(f"User {user_id} entered show_balance_and_portfolio_from_trade.")

    # --- پاک کردن تاریخچه مکالمه و تنظیم وضعیت فعلی ---
    # این بخش اطمینان می‌دهد که هنگام ورود به پورتفو از طریق یک معامله، تاریخچه قبلی پاک شده
    # و دکمه بازگشت از پورتفو همیشه به منوی اصلی هدایت شود.
    context.user_data['conv_state_history'] = []
    # 'main_menu' یک نام وضعیت دلخواه است که نشان می‌دهد کاربر در منوی اصلی یا صفحه مشابه است.
    context.user_data['current_conv_step'] = 'main_menu'
    logging.info(f"User {user_id}: conv_state_history and current_conv_step reset in show_balance_and_portfolio_from_trade.")
    # --- پایان پاکسازی ---

    # 1. گرفتن اطلاعات کاربر (balance)
    user_data = get_user(user_id)  # مطمئن شوید این تابع user_data را از دیتابیس می‌خواند
    user_balance = user_data["balance"]

    # 2. فراخوانی تابع جدید برای دریافت تمام داده‌های پورتفو
    portfolio_data = await get_full_portfolio_data(user_id)
    grouped_positions = portfolio_data["grouped_positions"]
    current_prices = portfolio_data["current_prices"]
    total_portfolio_value = portfolio_data["total_portfolio_value"]
    total_invested_value = portfolio_data["total_invested_value"]

    # 3. محاسبه موجودی قابل استفاده
    available_balance = get_user_available_balance(user_id)

    # 4. شروع ساخت متن پاسخ
    text = f"💰 **موجودی و پورتفوی شما**\n\n"
    text += f"موجودی فعلی شما: **{user_balance + total_portfolio_value:.2f} چیپ**\n"
    text += f"موجودی قابل استفاده برای معاملات جدید: **{available_balance:.2f} چیپ**\n"

    # --- Portfolio Section (با استفاده از داده‌های از تابع جدید) ---
    MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD = 0.1

    if grouped_positions:
        text += "\n--- **پورتفوی فعال** ---\n"

        displayed_coins_count = 0

        for pos in grouped_positions:
            symbol = pos['symbol']
            amount = pos['amount']
            buy_price = pos['buy_price']

            coin_info = next((c for c in top_coins if c['symbol'] == symbol), None)
            coin_slug = coin_info['id'] if coin_info else None

            current_price = current_prices.get(coin_slug, 0) if coin_slug else 0

            current_value = amount * current_price
            if current_value < MIN_PORTFOLIO_VALUE_DISPLAY_THRESHOLD and current_value > 0:
                logging.info(
                    f"Skipping display of {symbol} (value: {current_value:.4f}) as it's below display threshold.)"
                )
                continue

            if current_price > 0:
                invested_value = amount * buy_price
                profit_loss = current_value - invested_value
                profit_loss_percent = (profit_loss / invested_value) * 100 if invested_value > 0 else 0

                pnl_emoji = "📈" if profit_loss >= 0 else "📉"

                text += (
                    f"\n💎 **{symbol}**\n"
                    f"  🔢 مقدار: {amount:.6f}\n"
                    f"  💲 میانگین قیمت خرید: ${format_price(buy_price)}\n"
                    f"  💲 قیمت فعلی: ${format_price(current_price)}\n"
                    f"  💰 ارزش فعلی: {current_value:.2f} چیپ\n"
                    f"  {pnl_emoji} سود/زیان: {profit_loss:+.2f} چیپ ({profit_loss_percent:+.2f}%)"
                )
                displayed_coins_count += 1
            else:
                text += (
                    f"\n💎 **{symbol}**\n"
                    f"  🔢 مقدار: {amount:.6f}\n"
                    f"  💲 میانگین قیمت خرید: ${format_price(buy_price)}\n"
                    f"  💲 قیمت فعلی: N/A\n"
                    f"  💰 ارزش فعلی: N/A\n"
                    f"  📈 سود/زیان: N/A (خطا در دریافت قیمت لحظه‌ای از کش)\n"
                )
                displayed_coins_count += 1
                logging.warning(f"Could not get current price for {symbol} ({coin_slug}) for portfolio display.")

        if displayed_coins_count == 0:
            text += "\n*شما هیچ پوزیشن بازی ندارید یا ارزش آن‌ها بسیار ناچیز است.*"

        overall_profit_loss = total_portfolio_value - total_invested_value
        overall_profit_loss_percent = (
                                              overall_profit_loss / total_invested_value) * 100 if total_invested_value > 0 else 0

        overall_pnl_emoji = "📈" if overall_profit_loss >= 0 else "📉"

        text += "\n\n--- **خلاصه پورتفو** ---\n"
        text += f"مجموع سرمایه فعال: **{total_invested_value:.2f} چیپ**\n"
        text += f"مجموع ارزش فعلی: **{total_portfolio_value:.2f} چیپ**\n"
        f"{overall_pnl_emoji} **کل سود/زیان پورتفو: {overall_profit_loss:+.2f} چیپ ({overall_profit_loss_percent:+.2f}%)**\n"
    else:
        text += "\n*شما هیچ پوزیشن بازی ندارید. با شروع معامله، پورتفوی شما نمایش داده خواهد شد.*"

    # --- نمایش Realized PnL (همانند قبل) ---
    total_realized_pnl = user_data.get("total_realized_pnl", 0.0)
    monthly_realized_pnl = user_data.get("monthly_realized_pnl", 0.0)

    total_realized_pnl_emoji = "📈" if total_realized_pnl >= 0 else "📉"
    monthly_realized_pnl_emoji = "📈" if monthly_realized_pnl >= 0 else "📉"

    text += "\n\n--- **سود/زیان معاملات بسته شده** ---\n"
    text += f"{total_realized_pnl_emoji} سود/زیان کلی (تمام معاملات بسته شده): **{total_realized_pnl:+.2f} چیپ**\n"
    now = datetime.datetime.now()
    text += f"{monthly_realized_pnl_emoji} سود/زیان ماهانه (از ابتدای {now.strftime('%B')}): **{monthly_realized_pnl:+.2f} چیپ**\n"

    # --- نمایش کارمزد پرداخت شده توسط کاربر ---
    user_commission = get_user_commission_balance(user_id)
    text += f"\n\n**کل کارمزد پرداخت شده توسط کاربر: {user_commission:.2f} چیپ**"

    logging.info(f"User {user_id} finished preparing balance and portfolio message from trade context.")

    # اطمینان حاصل کنید که update.callback_query و update.callback_query.message وجود دارند
    if update.callback_query and update.callback_query.message:
        try:
            # از reply_text استفاده می‌کنیم چون ممکن است پیام اصلی تغییر کرده باشد یا به دلیل پایان مکالمه قابل ویرایش نباشد.
            await update.callback_query.message.reply_text(
                text,
                reply_markup=get_action_buttons_keyboard(),
                parse_mode='Markdown'
            )
            logging.info(f"User {user_id}: Replied with portfolio message from trade context.")
        except Exception as e:
            logging.error(f"User {user_id}: Failed to reply with portfolio message (likely BadRequest). Error: {e}")
            # در صورت بروز خطای جدی، می‌توانید پیام خطای جایگزین ارسال کنید
            await update.effective_chat.send_message(
                "مشکلی در نمایش پورتفو شما رخ داد. لطفاً دوباره تلاش کنید.",
                reply_markup=get_action_buttons_keyboard()
            )
    else:
        logging.error(f"Cannot send portfolio message: no valid message source found for user {user_id}.")

    # اگر این تابع بخشی از یک ConversationHandler است و باید آن را پایان دهد:
    # return ConversationHandler.END

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # تاریخچه پوزیشن‌های خرید/فروش (مدل جدید)
    cursor.execute(
        "SELECT symbol, amount, buy_price, closed_price, status, open_timestamp, profit_loss, commission_paid FROM user_positions WHERE user_id=? ORDER BY id DESC LIMIT 10",
        # 10 پوزیشن آخر
        (user_id,)
    )
    position_rows = cursor.fetchall()

    if not position_rows:
        logging.info(f"User {user_id} requested history, but no positions found.")
        await update.callback_query.edit_message_text(
            "تاریخچه معامله‌ای یافت نشد.",
            reply_markup=get_action_buttons_keyboard()
        )
        return

    text = "📜 **تاریخچه معاملات شما:**\n\n"

    text += "**📈 معاملات خرید/فروش:**\n"
    for i, r in enumerate(position_rows):
        sym, amount, buy_p, closed_p, status, open_ts, pnl, commission = r

        pnl_text = ""
        if status == 'closed':
            if pnl is not None:  # Ensure pnl is not None before formatting
                if pnl > 0:
                    pnl_text = f"سود: **+{pnl:.2f} چیپ** 🥳"
                elif pnl < 0:
                    pnl_text = f"ضرر: **{pnl:.2f} چیپ** 😔"
                else:
                    pnl_text = "نتیجه: مساوی"

        text += (f"**{i + 1}. {open_ts.split('.')[0]}**\n"  # فرمت تاریخ را ساده‌تر می‌کنیم
                 f"  💎 ارز: {sym}\n"
                 f"  🔢 مقدار: {amount:.6f}\n"
                 f"  💲 قیمت خرید: ${format_price(buy_p)}\n")
        if status == 'closed' and closed_p is not None:
            text += f"  💲 قیمت فروش: ${format_price(closed_p)}\n"
        text += (f"  وضعیت: **{status}**\n"
                 f"  کارمزد: {commission:.2f} چیپ\n")
        if pnl_text:
            text += f"  {pnl_text}\n"
        text += "\n"

    logging.info(
        f"User {user_id} requested history. {len(position_rows)} positions found.")
    await update.callback_query.edit_message_text(
        text,
        reply_markup=get_action_buttons_keyboard(),
        parse_mode='Markdown'
    )




async def handle_tpsl_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    هندلر برای دریافت ورودی قیمت TP یا SL از کاربر.
    اعتبارسنجی می‌کند، در دیتابیس ذخیره می‌کند و به مرحله بعدی هدایت می‌کند.
    """
    user_id = update.effective_user.id
    input_text = update.message.text.strip()  # گرفتن متن وارد شده توسط کاربر

    # اعتبارسنجی که ورودی عدد هست
    if not re.fullmatch(r'^\d+(\.\d+)?$', input_text):
        # اینجا final_message تعریف نشده است، باید یک پیام خطای مناسب ارسال شود.
        # و کاربر را در همان حالت فعلی نگه داریم.
        await update.message.reply_text(
            "لطفاً یک عدد معتبر برای قیمت وارد کنید. مثال: 15.50 یا 16",
            parse_mode='Markdown'
        )
        # در همین حالت می‌مانیم و از کاربر می‌خواهیم دوباره وارد کند
        # تعیین حالت فعلی بر اساس tpsl_step
        current_step = context.user_data.get('tpsl_step')
        if current_step == 'tp':
            return ENTERING_TP_PRICE
        elif current_step == 'sl':
            return ENTERING_SL_PRICE
        else:  # اگر tpsl_step به هر دلیلی تنظیم نشده بود، مکالمه را خاتمه می‌دهیم.
            logging.error(f"tpsl_step not found for user {user_id} during invalid input. Ending conversation.")
            await update.message.reply_text("خطا در پردازش. لطفاً دوباره تلاش کنید.",
                                            reply_markup=get_main_menu_keyboard())
            # پاک کردن context.user_data مربوط به TP/SL
            context.user_data.pop('current_position_id_for_tpsl', None)
            context.user_data.pop('current_buy_price_for_tpsl', None)
            context.user_data.pop('tpsl_step', None)
            return ConversationHandler.END

    price = float(input_text)

    position_id = context.user_data.get('current_position_id_for_tpsl')
    buy_price = context.user_data.get('current_buy_price_for_tpsl')
    tpsl_step = context.user_data.get('tpsl_step')  # 'tp' یا 'sl'

    if not position_id or not buy_price or not tpsl_step:
        logging.error(
            f"Missing context data for TP/SL input for user {user_id}. Position ID: {position_id}, Buy Price: {buy_price}, Step: {tpsl_step}")
        await update.message.reply_text(
            "خطا در پردازش. اطلاعات معامله شما یافت نشد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_main_menu_keyboard()
        )
        context.user_data.pop('current_position_id_for_tpsl', None)
        context.user_data.pop('current_buy_price_for_tpsl', None)
        context.user_data.pop('tpsl_step', None)
        return ConversationHandler.END

    if tpsl_step == 'tp':
        # بررسی منطقی برای Take Profit: باید از قیمت خرید بالاتر باشد
        if price <= buy_price:
            await update.message.reply_text(
                f"قیمت حد سود (TP) باید **بزرگتر از قیمت خرید** (${format_price(buy_price)}) باشد. لطفاً مجدداً وارد کنید.",
                parse_mode='Markdown'
            )
            return ENTERING_TP_PRICE  # در همین حالت می‌مانیم

        # ذخیره قیمت TP در دیتابیس
        try:
            cursor.execute("UPDATE user_positions SET tp_price = ? WHERE position_id = ?", (price, position_id))
            conn.commit()
            logging.info(f"TP price {price:.4f} set for position {position_id} by user {user_id}.")
            context.user_data['tp_price_set'] = price  # ذخیره برای نمایش نهایی
        except Exception as e:
            logging.error(f"Error updating TP price for position {position_id}: {e}")
            await update.message.reply_text(
                "خطا در ذخیره قیمت حد سود. لطفاً دوباره تلاش کنید.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        # حالا از کاربر می‌خواهیم قیمت حد ضرر (SL) را وارد کند
        message_text = (
            "✅ قیمت حد سود شما با موفقیت ثبت شد.\n"
            "اکنون لطفاً **قیمت حد ضرر (Stop Loss)** مورد نظر خود را وارد کنید.\n"
            f"قیمت خرید فعلی این پوزیشن: **${format_price(buy_price)}**\n"
            "قیمت حد ضرر باید **کوچکتر از قیمت خرید** باشد.\n"
            "برای لغو، از دکمه زیر استفاده کنید."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("بازگشت به منو اصلی (لغو)", callback_data='back_to_main_menu')]
        ])
        await update.message.reply_text(
            text=message_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        context.user_data['tpsl_step'] = 'sl'  # تغییر مرحله به SL
        return ENTERING_SL_PRICE  # رفتن به حالت دریافت SL

    elif tpsl_step == 'sl':
        # بررسی منطقی برای Stop Loss: باید از قیمت خرید پایین‌تر باشد
        if price >= buy_price:
            await update.message.reply_text(
                f"قیمت حد ضرر (SL) باید **کوچکتر از قیمت خرید** (${format_price(buy_price)}) باشد. لطفاً مجدداً وارد کنید.",
                parse_mode='Markdown'
            )
            return ENTERING_SL_PRICE  # در همین حالت می‌مانیم

        # ذخیره قیمت SL در دیتابیس
        try:
            cursor.execute("UPDATE user_positions SET sl_price = ? WHERE position_id = ?", (price, position_id))
            conn.commit()
            logging.info(f"SL price {price:.4f} set for position {position_id} by user {user_id}.")
            context.user_data['sl_price_set'] = price  # ذخیره برای نمایش نهایی
        except Exception as e:
            logging.error(f"Error updating SL price for position {position_id}: {e}")
            await update.message.reply_text(
                "خطا در ذخیره قیمت حد ضرر. لطفاً دوباره تلاش کنید.",
                reply_markup=get_main_menu_keyboard()
            )
            return ConversationHandler.END

        # هر دو TP و SL تنظیم شده‌اند، مکالمه را پایان می‌دهیم
        final_tp = context.user_data.get('tp_price_set', 'تنظیم نشده')
        final_sl = context.user_data.get('sl_price_set', 'تنظیم نشده')

        final_message = (
            "🎉 **حد سود و حد ضرر شما با موفقیت تنظیم شد!** 🎉\n\n"
            f"💎 پوزیشن شما: #{position_id}\n"
            f"📈 قیمت خرید: **${format_price(buy_price)}**\n"
        )

        # اضافه کردن خط حد سود (TP)
        if isinstance(final_tp, float):
            final_message += f"⬆️ حد سود (TP): **${final_tp:.4f}**\n"
        else:
            final_message += f"⬆️ حد سود (TP): **{final_tp}** (تنظیم نشده)\n"

        # اضافه کردن خط حد ضرر (SL)
        if isinstance(final_sl, float):
            final_message += f"⬇️ حد ضرر (SL): **${final_sl:.4f}**\n\n"
        else:
            final_message += f"⬇️ حد ضرر (SL): **{final_sl}** (تنظیم نشده)\n\n"

        final_message += "اکنون می‌توانید معاملات خود را در بخش 'پورتفوی من' بررسی کنید."

        # --- ارسال پیام نهایی و سپس تأخیر کوتاه ---
        await update.message.reply_text(
            final_message,
            reply_markup=get_main_menu_keyboard(),  # تغییر به get_main_menu_keyboard()
            parse_mode='Markdown'
        )
        await asyncio.sleep(0.5)  # یک تأخیر کوتاه برای اطمینان از ارسال کامل پیام

        # پاک کردن context.user_data مربوط به TP/SL
        context.user_data.pop('current_position_id_for_tpsl', None)
        context.user_data.pop('current_buy_price_for_tpsl', None)
        context.user_data.pop('tpsl_step', None)
        context.user_data.pop('tp_price_set', None)
        context.user_data.pop('sl_price_set', None)

        return ConversationHandler.END  # پایان مکالمه

async def about_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} requested about bot info.")
    about_text = (
        "🤖 **درباره ربات ترید (نسخه شبیه‌ساز)**\n\n"
        "این ربات به شما امکان می‌دهد تا با استفاده از چیپ‌های مجازی، مهارت‌های ترید خود را در بازار ارزهای دیجیتال **بدون ریسک مالی واقعی** تمرین کنید.\n"
        "شما می‌توانید ارزهای مجازی را خرید و فروش کنید و سود یا ضرر خود را با چیپ‌های مجازی تجربه کنید.\n\n"
        "💎 **چیپ‌های مجازی هیچ ارزش پولی واقعی ندارند و قابل تبدیل به پول نیستند.**\n"
        "هدف این ربات صرفاً آموزش و سرگرمی است."
    )
    await update.callback_query.edit_message_text(
        about_text,
        reply_markup=get_action_buttons_keyboard(),
        parse_mode='Markdown'
    )


async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} returning to main menu.")
    await update.callback_query.edit_message_text(
        'یکی از گزینه‌های زیر را انتخاب کنید:',
        reply_markup=get_main_menu_keyboard()
    )
    if 'conv_state_history' in context.user_data:
        del context.user_data['conv_state_history']
    if 'current_conv_step' in context.user_data:
        del context.user_data['current_conv_step']
    if 'reconfirmed_price' in context.user_data:  # ریست کردن حالت تایید مجدد
        del context.user_data['reconfirmed_price']
    if 'initial_displayed_price' in context.user_data:  # ریست کردن قیمت اولیه نمایش داده شده
        del context.user_data['initial_displayed_price']
    return ConversationHandler.END


# فرض می‌شود توابع get_action_buttons_keyboard و get_trade_active_keyboard در جای دیگری تعریف شده‌اند.

async def cancel_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"User {update.effective_user.id} cancelled trade conversation.")

    # پیام را با موفقیت‌آمیز بودن لغو ویرایش می‌کنیم
    await update.callback_query.edit_message_text(
        "✅ عملیات معامله لغو شد.",  # می‌توانید پیام را کمی واضح‌تر کنید
        reply_markup=get_trade_active_keyboard()  # <--- تغییر اصلی: استفاده از کیبورد فعالیت‌های معاملاتی
    )

    # پاک کردن دیتاهای مربوط به مکالمه
    if 'conv_state_history' in context.user_data:
        del context.user_data['conv_state_history']
    if 'current_conv_step' in context.user_data:
        del context.user_data['current_conv_step']
    if 'reconfirmed_price' in context.user_data:  # ریست کردن حالت تایید مجدد
        del context.user_data['reconfirmed_price']
    if 'initial_displayed_price' in context.user_data:  # ریست کردن قیمت اولیه نمایش داده شده
        del context.user_data['initial_displayed_price']

    return ConversationHandler.END  # پایان مکالمه


async def handle_tpsl_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    هندلر برای دریافت ورودی قیمت TP یا SL از کاربر.
    اعتبارسنجی می‌کند، در دیتابیس ذخیره می‌کند و به مرحله بعدی هدایت می‌کند.
    """
    user_id = update.effective_user.id
    input_text = update.message.text.strip()  # گرفتن متن وارد شده توسط کاربر

    # اعتبارسنجی که ورودی عدد هست
    if not re.fullmatch(r'^\d+(\.\d+)?$', input_text):
        await update.message.reply_text(
            "لطفاً یک عدد معتبر برای قیمت وارد کنید. مثال: 15.50 یا 16",
            parse_mode='Markdown'
        )
        current_step = context.user_data.get('tpsl_step')
        if current_step == 'tp':
            return ENTERING_TP_PRICE
        elif current_step == 'sl':
            return ENTERING_SL_PRICE
        else:
            logging.error(f"tpsl_step not found for user {user_id} during invalid input. Ending conversation.")
            await update.message.reply_text("خطا در پردازش. لطفاً دوباره تلاش کنید.",
                                            reply_markup=get_main_menu_keyboard())
            # پاک کردن context.user_data مربوط به TP/SL در صورت خطای داخلی
            context.user_data.pop('current_position_id_for_tpsl', None)
            context.user_data.pop('current_buy_price_for_tpsl', None)
            context.user_data.pop('tpsl_step', None)
            context.user_data.pop('tp_price_set', None)
            context.user_data.pop('sl_price_set', None)
            context.user_data.pop('selected_coin_symbol', None)
            context.user_data.pop('final_bought_amount', None)
            return ConversationHandler.END

    price = float(input_text)

    position_id = context.user_data.get('current_position_id_for_tpsl')
    buy_price = context.user_data.get('current_buy_price_for_tpsl')
    tpsl_step = context.user_data.get('tpsl_step')  # 'tp' یا 'sl'

    if not position_id or not buy_price or not tpsl_step:
        logging.error(
            f"Missing context data for TP/SL input for user {user_id}. Position ID: {position_id}, Buy Price: {buy_price}, Step: {tpsl_step}")
        await update.message.reply_text(
            "خطا در پردازش. اطلاعات معامله شما یافت نشد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_main_menu_keyboard()
        )
        # پاک کردن تمامی داده‌های مربوط به مکالمه در صورت خطا
        context.user_data.pop('current_position_id_for_tpsl', None)
        context.user_data.pop('current_buy_price_for_tpsl', None)
        context.user_data.pop('tpsl_step', None)
        context.user_data.pop('tp_price_set', None)
        context.user_data.pop('sl_price_set', None)
        context.user_data.pop('selected_coin_symbol', None)
        context.user_data.pop('final_bought_amount', None)
        return ConversationHandler.END

    if tpsl_step == 'tp':
        # بررسی منطقی برای Take Profit: باید از قیمت خرید بالاتر باشد
        if price <= buy_price:
            await update.message.reply_text(
                f"قیمت حد سود (TP) باید **بزرگتر از قیمت خرید** (${format_price(buy_price)}) باشد. لطفاً مجدداً وارد کنید.",
                parse_mode='Markdown'
            )
            return ENTERING_TP_PRICE  # در همین حالت می‌مانیم

        # ذخیره قیمت TP در دیتابیس
        try:
            cursor.execute("UPDATE user_positions SET tp_price = ? WHERE position_id = ?", (price, position_id))
            conn.commit()
            logging.info(f"TP price {format_price(price)} set for position {position_id} by user {user_id}.")
            context.user_data['tp_price_set'] = price  # ذخیره برای نمایش نهایی
        except Exception as e:
            logging.error(f"Error updating TP price for position {position_id}: {e}")
            await update.message.reply_text(
                "خطا در ذخیره قیمت حد سود. لطفاً دوباره تلاش کنید.",
                reply_markup=get_main_menu_keyboard()
            )
            # پاک کردن تمامی داده‌های مربوط به مکالمه در صورت خطا
            context.user_data.pop('current_position_id_for_tpsl', None)
            context.user_data.pop('current_buy_price_for_tpsl', None)
            context.user_data.pop('tpsl_step', None)
            context.user_data.pop('tp_price_set', None)
            context.user_data.pop('sl_price_set', None)
            context.user_data.pop('selected_coin_symbol', None)
            context.user_data.pop('final_bought_amount', None)
            return ConversationHandler.END

        # حالا از کاربر می‌خواهیم قیمت حد ضرر (SL) را وارد کند
        message_text = (
            "✅ قیمت حد سود شما با موفقیت ثبت شد.\n"
            "اکنون لطفاً **قیمت حد ضرر (Stop Loss)** مورد نظر خود را وارد کنید.\n"
            f"قیمت خرید فعلی این پوزیشن: **${format_price(buy_price)}**\n"
            "قیمت حد ضرر باید **کوچکتر از قیمت خرید** باشد.\n"
            "برای لغو، از دکمه زیر استفاده کنید."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("بازگشت به منو اصلی (لغو)", callback_data='back_to_main_menu')]
        ])
        await update.message.reply_text(
            text=message_text,
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        context.user_data['tpsl_step'] = 'sl'  # تغییر مرحله به SL
        return ENTERING_SL_PRICE  # رفتن به حالت دریافت SL

    elif tpsl_step == 'sl':
        # بررسی منطقی برای Stop Loss: باید از قیمت خرید پایین‌تر باشد
        if price >= buy_price:
            await update.message.reply_text(
                f"قیمت حد ضرر (SL) باید **کوچکتر از قیمت خرید** (${format_price(buy_price)}) باشد. لطفاً مجدداً وارد کنید.",
                parse_mode='Markdown'
            )
            return ENTERING_SL_PRICE  # در همین حالت می‌مانیم

        # ذخیره قیمت SL در دیتابیس
        try:
            cursor.execute("UPDATE user_positions SET sl_price = ? WHERE position_id = ?", (price, position_id))
            conn.commit()
            logging.info(f"SL price {format_price(price)} set for position {position_id} by user {user_id}.")
            context.user_data['sl_price_set'] = price  # ذخیره برای نمایش نهایی
        except Exception as e:
            logging.error(f"Error updating SL price for position {position_id}: {e}")
            await update.message.reply_text(
                "خطا در ذخیره قیمت حد ضرر. لطفاً دوباره تلاش کنید.",
                reply_markup=get_main_menu_keyboard()
            )
            # پاک کردن تمامی داده‌های مربوط به مکالمه در صورت خطا
            context.user_data.pop('current_position_id_for_tpsl', None)
            context.user_data.pop('current_buy_price_for_tpsl', None)
            context.user_data.pop('tpsl_step', None)
            context.user_data.pop('tp_price_set', None)
            context.user_data.pop('sl_price_set', None)
            context.user_data.pop('selected_coin_symbol', None)
            context.user_data.pop('final_bought_amount', None)
            return ConversationHandler.END

        # هر دو TP و SL تنظیم شده‌اند، مکالمه را پایان می‌دهیم
        final_tp = context.user_data.get('tp_price_set', 'تنظیم نشده')
        final_sl = context.user_data.get('sl_price_set', 'تنظیم نشده')

        # اطلاعات کوین و مقدار خرید را از context.user_data دریافت کنید
        # اینها باید در تابع process_buy_order ذخیره شده باشند.
        bought_coin_symbol = context.user_data.get('selected_coin_symbol', 'نامشخص')
        # bought_amount = context.user_data.get('final_bought_amount', 'مقدار نامشخص') # در صورت نیاز به نمایش مقدار خرید


        # ساخت پیام نهایی
        final_message = (
            "🎉 **حد سود و حد ضرر شما با موفقیت تنظیم شد!** 🎉\n\n"
            f"💰 **خرید {bought_coin_symbol} با قیمت ${format_price(buy_price)} انجام شد!**\n"
        )

        # اضافه کردن خط حد سود (TP)
        if isinstance(final_tp, float):
            final_message += f"⬆️ حد سود (TP): **${format_price(final_tp)}**\n"
        else:
            final_message += f"⬆️ حد سود (TP): **{final_tp}** (تنظیم نشده)\n"

        # اضافه کردن خط حد ضرر (SL)
        if isinstance(final_sl, float):
            final_message += f"⬇️ حد ضرر (SL): **${format_price(final_sl)}**\n\n"
        else:
            final_message += f"⬇️ حد ضرر (SL): **{final_sl}** (تنظیم نشده)\n\n"

        final_message += "اکنون می‌توانید معاملات خود را در بخش 'پورتفوی من' بررسی کنید."

        # --- بهبود مدیریت خطای ارسال پیام نهایی ---
        try:
            logging.info(f"Attempting to send final TP/SL confirmation message for user {user_id}.")
            await update.message.reply_text(
                final_message,
                reply_markup=get_main_menu_keyboard(),
                parse_mode='Markdown'
            )
            logging.info(f"Successfully sent final TP/SL confirmation message for user {user_id}.")
        except telegram.error.BadRequest as e:
            logging.warning(
                f"Failed to send final TP/SL message for user {user_id} due to BadRequest: {e}. Attempting to send as new message.")
            # اگر به هر دلیلی ارسال پیام دچار مشکل شد، یک پیام جدید ارسال می‌کنیم.
            await update.message.reply_text(
                "تنظیمات حد سود و ضرر شما انجام شد، اما مشکلی در ارسال پیام تأیید رخ داد. لطفاً پورتفوی خود را بررسی کنید.",
                reply_markup=get_main_menu_keyboard(),
                parse_mode='Markdown' # اضافه کردن parse_mode برای اطمینان از فرمتینگ
            )
            logging.info(f"Successfully sent final TP/SL confirmation as a new message for user {user_id}.")
        except Exception as e:
            logging.error(f"Unexpected error sending final TP/SL message for user {user_id}: {e}")
            await update.message.reply_text(
                "تنظیمات حد سود و ضرر شما انجام شد، اما مشکلی در ارسال پیام تأیید رخ داد. لطفاً پورتفوی خود را بررسی کنید.",
                reply_markup=get_main_menu_keyboard()
            )
        # --- پایان بهبود مدیریت خطای ارسال پیام نهایی ---

        await asyncio.sleep(0.5)  # یک تأخیر کوتاه برای اطمینان از ارسال کامل پیام

        # --- بهینه سازی پاک کردن context.user_data ---
        # تمام داده‌های مربوط به مکالمه TP/SL و همچنین اطلاعات موقت خرید را پاک می‌کنیم
        context.user_data.pop('current_position_id_for_tpsl', None)
        context.user_data.pop('current_buy_price_for_tpsl', None)
        context.user_data.pop('tpsl_step', None)
        context.user_data.pop('tp_price_set', None)
        context.user_data.pop('sl_price_set', None)
        context.user_data.pop('selected_coin_symbol', None) # این رو هم پاک می‌کنیم
        context.user_data.pop('final_bought_amount', None) # این رو هم پاک می‌کنیم

        return ConversationHandler.END # پایان مکالمه

async def revert_to_previous_state(update: Update, context: ContextTypes.DEFAULT_TYPE, prev_step_data: str):
    """
    این تابع مکالمه را به مرحله قبلی برمی‌گرداند.
    با توجه به 'prev_step_data'، پیام و کیبورد مناسب را نمایش می‌دهد.
    در صورت بازگشت به مرحله وارد کردن مقدار فروش، دکمه 'فروش تمام موجودی' را شامل می‌شود
    و دستورالعمل مربوط به پاک کردن نام ربات را اضافه می‌کند.
    """
    query = update.callback_query
    user_id = query.from_user.id
    logging.info(f"User {user_id} reverting to step: {prev_step_data}")

    if prev_step_data.startswith('select_coin_'):
        # اگر در حال بازگشت به مرحله انتخاب ارز برای خرید هستیم
        coin_id = prev_step_data.replace('select_coin_', '')
        selected_coin = next((c for c in top_coins if c['id'] == coin_id), None)
        if selected_coin:
            context.user_data["coin_name"] = selected_coin['name']
            context.user_data["coin_slug"] = selected_coin['id']
            context.user_data["symbol"] = selected_coin['symbol']

            current_price = await get_price_from_cache(selected_coin['id'])
            if current_price == 0:
                await query.edit_message_text(
                    "متاسفانه در حال حاضر امکان دریافت قیمت این ارز وجود ندارد. لطفاً ارز دیگری را امتحان کنید.",
                    reply_markup=get_coin_selection_keyboard(context.user_data['current_coin_page'])
                )
                return CHOOSING_COIN
            context.user_data['initial_displayed_price'] = current_price
            context.user_data['current_price'] = current_price

            user_id = query.from_user.id
            available_bal = get_user_available_balance(user_id)
            max_buy_chips = available_bal * MAX_BUY_AMOUNT_PERCENTAGE

            await query.edit_message_text(
                f"💎 ارز انتخاب شده: **{selected_coin['symbol']}**\n"
                f"📈 قیمت فعلی: **${format_price(current_price)}**\n\n"
                f"💰 موجودی قابل استفاده شما: **{available_bal:.2f} چیپ**\n\n"
                f"لطفاً **مقدار چیپ** را که می‌خواهید برای خرید **{selected_coin['symbol']}** استفاده کنید، وارد کنید.\n"
                f"حداقل خرید: **{MIN_BUY_AMOUNT:.2f} چیپ**\n"
                f"حداکثر خرید (تقریبی): **{max_buy_chips:.2f} چیپ** (به دلیل کارمزد ممکن است کمی متفاوت باشد)\n"
                f"مثال: `{MIN_BUY_AMOUNT:.0f}` یا `{math.floor(max_buy_chips):.0f}` (برای خرید تمام موجودی قابل استفاده)",
                parse_mode='Markdown'
            )
            return ENTERING_AMOUNT
        else:
            await query.edit_message_text("مشکلی در بازگشت به مرحله انتخاب ارز رخ داد. به منوی اصلی برمی‌گردید.",
                                          reply_markup=get_main_menu_keyboard())
            return ConversationHandler.END
    elif prev_step_data == 'start_trade':
        # اگر در حال بازگشت به ابتدای فرآیند خرید هستیم
        await query.edit_message_text(
            "لطفاً یکی از ارزهای دیجیتال زیر را انتخاب کنید:",
            reply_markup=get_coin_selection_keyboard(page=context.user_data.get('current_coin_page', 0))
        )
        return CHOOSING_COIN
    elif prev_step_data.startswith('coins_page_'):
        # اگر در حال بازگشت به صفحه قبلی انتخاب ارز (برای خرید) هستیم
        page = int(prev_step_data.replace('coins_page_', ''))
        context.user_data['current_coin_page'] = page
        await query.edit_message_text(
            "لطفاً یکی از ارزهای دیجیتال زیر را انتخاب کنید:",
            reply_markup=get_coin_selection_keyboard(page)
        )
        return CHOOSING_COIN
    elif prev_step_data == 'sell_portfolio_entry':
        # اگر در حال بازگشت به نقطه ورود به فرآیند فروش هستیم (نمایش پورتفوی)
        await sell_portfolio_entry_point(update, context)
        return CHOOSING_COIN_TO_SELL
    elif prev_step_data.startswith('sell_coin_'):
        # اگر در حال بازگشت به مرحله انتخاب ارز خاص برای فروش هستیم (وارد کردن مقدار)
        parts = prev_step_data.split('_')
        coin_slug = parts[2]
        symbol = parts[3]

        selected_coin_data = next((c for c in top_coins if c['id'] == coin_slug), None)
        coin_name = selected_coin_data['name'] if selected_coin_data else symbol

        pos_data = context.user_data.get(f"sell_pos_data_{coin_slug}_{symbol}")
        if pos_data:
            context.user_data['sell_coin_slug'] = coin_slug
            context.user_data['sell_symbol'] = symbol
            context.user_data['sell_coin_name'] = coin_name
            context.user_data['sell_amount_available'] = pos_data['amount']
            context.user_data['sell_buy_price_avg'] = pos_data['buy_price']
            context.user_data['initial_displayed_price'] = pos_data['current_price']
            context.user_data['current_price'] = pos_data['current_price']

            await query.edit_message_text(
                f"💎 ارز انتخاب شده برای فروش: **{coin_name} ({symbol})**\n"
                f"📈 میانگین قیمت خرید شما: **${format_price(pos_data['buy_price'])}**\n"
                f"💲 قیمت فعلی بازار: **${format_price(pos_data['current_price'])}**\n"
                f"🔢 مقدار موجود برای فروش: **{pos_data['amount']:.6f} واحد**\n\n"
                f"لطفاً **مقداری** از **{coin_name} ({symbol})** را که می‌خواهید بفروشید وارد کنید.\n"
                "می‌توانید هر مقدار اعشاری تا ۶ رقم اعشار وارد کنید.\n\n" # اضافه شدن خط جدید
                "**توجه:**اگر کامل نمیفروشید مقدار دلخواهتان رو در چت دستی وارد کنید و اینتر بزنید.", # اضافه شدن پیام جدید
                reply_markup=get_action_buttons_keyboard(full_amount_to_sell_units=pos_data['amount']),
                parse_mode='Markdown'
            )
            return ENTERING_SELL_AMOUNT
        else:
            await query.edit_message_text(
                "مشکلی در بازگشت به مرحله انتخاب ارز برای فروش رخ داد. به منوی اصلی برمی‌گردید.",
                reply_markup=get_main_menu_keyboard())
            return ConversationHandler.END
    else:
        # بازگشت به منوی اصلی در صورت عدم یافتن وضعیت قبلی مشخص
        await query.edit_message_text(
            'یکی از گزینه‌های زیر را انتخاب کنید:',
            reply_markup=get_main_menu_keyboard()
        )
        return ConversationHandler.END


# --- NEW: Sell Portfolio Handlers ---

async def sell_portfolio_entry_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for selling assets from the portfolio."""
    user_id = update.effective_user.id
    context.user_data['conv_state_history'] = []
    context.user_data['current_conv_step'] = 'sell_portfolio_entry'
    context.user_data['reconfirmed_price'] = None  # Reset reconfirmation state
    context.user_data['initial_displayed_price'] = None  # Reset initial price

    # Get open positions for the user
    user_open_positions = get_open_positions_grouped(user_id)  # Using the grouped positions helper

    if not user_open_positions:
        logging.info(f"User {user_id} tried to sell, but has no open positions.")
        await update.callback_query.edit_message_text(
            "شما هیچ ارزی برای فروش در پورتفوی خود ندارید.",
            reply_markup=get_action_buttons_keyboard() # توجه: شاید اینجا هم بخواهید get_trade_active_keyboard() باشد
        )
        return ConversationHandler.END

    # Fetch current prices for all coins in portfolio from cache
    coin_slugs_in_portfolio = []
    for pos in user_open_positions:
        coin_info = next((c for c in top_coins if c['symbol'] == pos['symbol']), None)
        if coin_info:
            coin_slugs_in_portfolio.append(coin_info['id'])

    current_prices_from_cache = await get_prices_for_portfolio_from_cache(coin_slugs_in_portfolio)

    keyboard_buttons = []
    for pos in user_open_positions:
        symbol = pos['symbol']
        amount = pos['amount']
        buy_price = pos['buy_price']  # This is the weighted average buy price

        coin_info = next((c for c in top_coins if c['symbol'] == symbol), None)
        coin_slug = coin_info['id'] if coin_info else None
        coin_name = coin_info['name'] if coin_info else symbol # برای نمایش نام کامل ارز

        current_price = current_prices_from_cache.get(coin_slug, 0) if coin_slug else 0

        if current_price > 0:
            current_value = amount * current_price
            invested_value = amount * buy_price
            profit_loss = current_value - invested_value
            profit_loss_percent = (profit_loss / invested_value) * 100 if invested_value > 0 else 0

            # **ساخت متن دکمه با اضافه کردن سود/زیان**
            pnl_status_text = ""
            if profit_loss > 0:
                pnl_status_text = f" سود {profit_loss:+.2f}    "
            elif profit_loss < 0:
                pnl_status_text = f" ضرر {profit_loss:+.2f}    "
            else:
                pnl_status_text = "بدون سود/ضرر"

            button_text = (
                f"{coin_name}  به ارزش {current_value:.2f} چیپ. {pnl_status_text}"
            )
            # -------------------------------------------------------------

            keyboard_buttons.append(
                [InlineKeyboardButton(button_text, callback_data=f"sell_coin_{coin_slug}_{symbol}")])

            # Store full position data for later use in user_data
            context.user_data[f"sell_pos_data_{coin_slug}_{symbol}"] = {
                'symbol': symbol,
                'amount': amount,
                'buy_price': buy_price,
                'current_price': current_price  # Store the price at this moment of display
            }
        else:
            logging.warning(f"Could not get current price for {symbol} ({coin_slug}) for selling.")
            keyboard_buttons.append([InlineKeyboardButton(f"{symbol} (خطا در قیمت)", callback_data="no_op")])

    keyboard_buttons.append([
        InlineKeyboardButton("❌ لغو", callback_data='cancel_trade'),
        InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data='back_to_main_menu')
    ])

    reply_markup = InlineKeyboardMarkup(keyboard_buttons)

    await update.callback_query.edit_message_text(
        "💎 **انتخاب ارز برای فروش:**\n"
        "یکی از ارزهای موجود در پورتفوی خود را برای فروش انتخاب کنید.\n"
        "اطلاعات نمایش داده شده شامل:\n"
        "*(نام ارز، نماد) - ارزش فعلی (چیپ). وضعیت سود/زیان (چیپ، درصد)*", # متن توضیحات به‌روز شد
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return CHOOSING_COIN_TO_SELL

# ... (سایر توابع choose_coin_to_sell, handle_sell_amount_input, process_sell_order) ...


async def choose_coin_to_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    این تابع انتخاب یک ارز از لیست پورتفوی برای فروش را مدیریت می‌کند.
    پس از انتخاب، اطلاعات ارز را نمایش داده و از کاربر می‌خواهد مقدار فروش را وارد کند.
    همچنین دکمه "فروش تمام موجودی" را در کیبورد نمایش می‌دهد و دستورالعمل پاک کردن نام ربات را اضافه می‌کند.
    """
    query = update.callback_query
    await query.answer()

    if query.data.startswith('sell_coin_'):
        parts = query.data.split('_')
        coin_slug = parts[2]
        symbol = parts[3]

        selected_coin_data = next((c for c in top_coins if c['id'] == coin_slug), None)
        coin_name = selected_coin_data['name'] if selected_coin_data else symbol

        pos_data = context.user_data.get(f"sell_pos_data_{coin_slug}_{symbol}")

        if not pos_data:
            await query.edit_message_text(
                "خطا در بازیابی اطلاعات ارز. لطفاً دوباره تلاش کنید.",
                reply_markup=get_action_buttons_keyboard()
            )
            return ConversationHandler.END

        context.user_data['sell_coin_slug'] = coin_slug
        context.user_data['sell_symbol'] = symbol
        context.user_data['sell_coin_name'] = coin_name
        context.user_data['sell_amount_available'] = pos_data['amount']
        context.user_data['sell_buy_price_avg'] = pos_data['buy_price']

        context.user_data['initial_displayed_price'] = pos_data['current_price']
        context.user_data['current_price'] = pos_data['current_price']

        await query.edit_message_text(
            f"💎 ارز انتخاب شده برای فروش: **{coin_name} ({symbol})**\n"
            f"📈 میانگین قیمت خرید شما: **${format_price(pos_data['buy_price'])}**\n"
            f"💲 قیمت فعلی بازار: **${format_price(pos_data['current_price'])}**\n"
            f"🔢 مقدار موجود برای فروش: **{pos_data['amount']:.6f} واحد**\n\n"
            f"لطفاً **مقداری** از **{coin_name} ({symbol})** را که می‌خواهید بفروشید وارد کنید.\n"
            "می‌توانید هر مقدار اعشاری تا ۶ رقم اعشار وارد کنید.\n\n" # اضافه شدن خط جدید
                "**توجه:**اگر کامل نمیفروشید مقدار دلخواهتان رو در چت دستی وارد کنید و اینتر بزنید.", # اضافه شدن پیام جدید
            reply_markup=get_action_buttons_keyboard(full_amount_to_sell_units=pos_data['amount']),
            parse_mode='Markdown'
        )
        return ENTERING_SELL_AMOUNT
    else:
        return await button_callback(update, context)



async def handle_sell_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    این تابع ورودی کاربر (مقدار ارز برای فروش) را مدیریت می‌کند.
    اگر ورودی نامعتبر باشد، پیام خطا به همراه کیبوردی شامل دکمه 'فروش تمام موجودی' نمایش می‌دهد
    و دستورالعمل پاک کردن نام ربات را نیز اضافه می‌کند.
    """
    user_id = update.effective_user.id
    try:
        # تبدیل متن ورودی به عدد و حذف کاما احتمالی
        amount_to_sell = float(update.message.text.replace(',', ''))

        # بازیابی اطلاعات لازم از context.user_data
        symbol = context.user_data['sell_symbol']
        available_amount = context.user_data['sell_amount_available'] # مقدار موجودی واحد ارز برای فروش
        avg_buy_price = context.user_data['sell_buy_price_avg']
        initial_displayed_price = context.user_data['initial_displayed_price']

        # --- اعتبارسنجی ورودی کاربر ---
        # بررسی اینکه مقدار وارد شده مثبت و کمتر یا مساوی موجودی قابل فروش باشد
        if amount_to_sell <= 0 or amount_to_sell > available_amount:
            await update.message.reply_text(
                f"مقدار نامعتبر است. لطفاً عددی بین 0 تا **{available_amount:.6f}** واحد وارد کنید.\n\n" # اضافه شدن خط جدید
                "**توجه:**اگر کامل نمیفروشید مقدار دلخواهتان رو در چت دستی وارد کنید و اینتر بزنید.", # اضافه شدن پیام جدید
                reply_markup=get_action_buttons_keyboard(full_amount_to_sell_units=available_amount),
                parse_mode='Markdown'
            )
            return ENTERING_SELL_AMOUNT

        # --- محاسبات مربوط به فروش ---
        potential_revenue = amount_to_sell * initial_displayed_price
        sell_commission = potential_revenue * COMMISSION_RATE
        net_revenue = potential_revenue - sell_commission

        invested_cost_for_this_sell = amount_to_sell * avg_buy_price
        profit_loss = net_revenue - invested_cost_for_this_sell
        profit_loss_percent = (profit_loss / invested_cost_for_this_sell) * 100 if invested_cost_for_this_sell > 0 else 0

        # ذخیره نتایج در context.user_data برای مرحله تأیید نهایی
        context.user_data['amount_to_sell'] = amount_to_sell
        context.user_data['potential_revenue'] = potential_revenue
        context.user_data['sell_commission'] = sell_commission
        context.user_data['net_revenue'] = net_revenue
        context.user_data['profit_loss_on_sell'] = profit_loss

        # ساخت پیام تأییدیه برای کاربر
        confirmation_message = (
            f"شما قصد فروش **{amount_to_sell:.6f} واحد** از **{symbol}** را دارید.\n"
            f"📈 میانگین قیمت خرید شما: **${format_price(avg_buy_price)}**\n"
            f"💲 قیمت فعلی فروش: **${format_price(initial_displayed_price)}**\n"
            f"💵 درآمد ناخالص تخمینی: **{potential_revenue:.2f} چیپ**\n"
            f"💸 کارمزد فروش ({(COMMISSION_RATE * 100):.1f}%): **{sell_commission:.2f} چیپ**\n"
            f"💰 درآمد خالص تخمینی: **{net_revenue:.2f} چیپ**\n"
            f"سود/زیان بر این معامله: **{profit_loss:+.2f} چیپ ({profit_loss_percent:+.2f}%)**\n\n"
            "آیا تأیید می‌کنید؟"
        )
        await update.message.reply_text(
            confirmation_message,
            reply_markup=get_confirm_sell_keyboard(), # نمایش کیبورد تأیید/لغو
            parse_mode='Markdown'
        )
        return CONFIRM_SELL # تغییر وضعیت مکالمه به حالت تأیید

    except ValueError:
        # در صورت عدم امکان تبدیل ورودی به عدد (مثلاً حروف وارد شده باشد)
        # مقدار موجودی برای دکمه "فروش تمام موجودی" را مجدداً ارسال می‌کنیم
        available_amount_for_error_case = context.user_data.get('sell_amount_available')
        await update.message.reply_text("لطفاً یک عدد معتبر برای مقدار فروش وارد کنید.\n\n" # اضافه شدن خط جدید
                "**توجه:**اگر کامل نمیفروشید مقدار دلخواهتان رو در چت دستی وارد کنید و اینتر بزنید.", # اضافه شدن پیام جدید
                                        reply_markup=get_action_buttons_keyboard(full_amount_to_sell_units=available_amount_for_error_case),
                                        parse_mode='Markdown')
        return ENTERING_SELL_AMOUNT # باقی ماندن در همین وضعیت برای تلاش مجدد کاربر
    except Exception as e:
        # مدیریت سایر خطاهای غیرمنتظره
        logging.error(f"Error handling sell amount input for user {user_id}: {e}")
        await update.message.reply_text("خطایی رخ داد. لطفاً دوباره تلاش کنید.",
                                        # برای خطای عمومی، دکمه را بدون مقدار کامل نمایش می‌دهیم یا می‌توانید آن را اضافه کنید
                                        reply_markup=get_action_buttons_keyboard())
        return ConversationHandler.END


async def process_sell_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    سفارش فروش نهایی را پردازش می‌کند، قیمت را مجدداً بررسی و تأیید می‌کند.
    سطح VIP کاربر را پس از فروش بررسی و ارتقاء می‌دهد.
    """
    query = update.callback_query # اضافه کردن این خط برای دسترسی به callback_query
    user_id = update.effective_user.id

    # متغیرهای تجمیعی برای جمع‌آوری PnL و کارمزد از تمام قطعات فروخته شده در این تراکنش
    total_pnl_from_this_sell_transaction = 0.0
    total_commission_from_this_sell_transaction = 0.0

    coin_slug = context.user_data['sell_coin_slug']
    symbol = context.user_data['sell_symbol']
    amount_to_sell = context.user_data['amount_to_sell']
    avg_buy_price = context.user_data['sell_buy_price_avg']  # میانگین قیمت خرید برای محاسبه PnL کلی

    # **گام جدید: بازبینی قیمت لحظه ای از کش**
    rechecked_price = await get_price_from_cache(coin_slug)
    if rechecked_price == 0:
        await query.edit_message_text( # استفاده از query
            "متاسفانه در حال حاضر امکان دریافت قیمت این ارز وجود ندارد. لطفاً دوباره تلاش کنید.",
            reply_markup=get_main_menu_keyboard(), # بازگشت به منوی اصلی در صورت خطا
            parse_mode='Markdown'
        )
        logging.error(f"Failed to re-check price from cache for {coin_slug} during sell order for user {user_id}.")
        context.user_data['reconfirmed_price'] = None  # ریست وضعیت
        context.user_data['initial_displayed_price'] = None  # ریست وضعیت
        return ConversationHandler.END

    new_current_sell_price = rechecked_price

    # محاسبه درآمد ناخالص و کارمزد با قیمت جدید
    new_potential_revenue = amount_to_sell * new_current_sell_price
    new_sell_commission = new_potential_revenue * COMMISSION_RATE
    new_net_revenue = new_potential_revenue - new_sell_commission

    # محاسبه سود/زیان کل این معامله فروش بر اساس میانگین قیمت خرید
    invested_cost_for_total_sell = amount_to_sell * avg_buy_price
    total_pnl_for_this_sell_calculation = new_net_revenue - invested_cost_for_total_sell
    new_profit_loss_percent = (
                                      total_pnl_for_this_sell_calculation / invested_cost_for_total_sell) * 100 if invested_cost_for_total_sell > 0 else 0

    # بررسی تغییر قیمت
    initial_displayed_price = context.user_data.get('initial_displayed_price')
    price_diff_percent = abs(
        new_current_sell_price - initial_displayed_price) / initial_displayed_price * 100 if initial_displayed_price else 100

    # اگر قیمت به طور قابل توجهی تغییر کرده و کاربر هنوز آن را تأیید نکرده است
    price_has_changed = price_diff_percent >= PRICE_CHANGE_THRESHOLD_PERCENT
    if price_has_changed and context.user_data.get('reconfirmed_price') != new_current_sell_price:
        context.user_data['initial_displayed_price'] = new_current_sell_price  # آپدیت برای نمایش بعدی

        reconfirmation_message = (
            f"⚠️ **قیمت {symbol} تغییر کرده است!** ⚠️\n\n"
            f"📈 قیمت قبلی: **${format_price(initial_displayed_price)}**\n"
            f"📈 **قیمت جدید: ${format_price(new_current_sell_price)}**\n"
            f"**تغییر: {price_diff_percent:.2f}%**\n\n"
            f"شما قصد فروش **{amount_to_sell:.6f} واحد** از **{symbol}** را دارید.\n"
            f"💵 درآمد ناخالص جدید: **{new_potential_revenue:.2f} چیپ**\n"
            f"💸 کارمزد فروش جدید: **{new_sell_commission:.2f} چیپ**\n"
            f"💰 درآمد خالص جدید: **{new_net_revenue:.2f} چیپ**\n"
            f"سود/زیان جدید بر این معامله: **{total_pnl_for_this_sell_calculation:+.2f} چیپ ({new_profit_loss_percent:+.2f}%)**\n\n"
            "آیا با **قیمت جدید** موافقید و فروش را تأیید می‌کنید؟"
        )
        await query.edit_message_text( # استفاده از query
            reconfirmation_message,
            reply_markup=get_confirm_sell_keyboard(),
            parse_mode='Markdown'
        )
        context.user_data['reconfirmed_price'] = new_current_sell_price
        return RECONFIRM_SELL

    logging.info(
        f"User {user_id} proceeding with final SELL for {amount_to_sell:.6f} {symbol} at ${new_current_sell_price:.4f}. Reconfirmed: {context.user_data.get('reconfirmed_price') == new_current_sell_price}.")

    # --- دریافت اطلاعات کاربر از دیتابیس (get_user حالا PnL ماهانه را ریست می‌کند) ---
    user_db_data = get_user(user_id) # این تابع باید قبل از تغییرات موجودی فراخوانی شود
    if not user_db_data:
        await query.edit_message_text("خطا: اطلاعات کاربر یافت نشد.", # استفاده از query
                                      reply_markup=get_main_menu_keyboard()) # بازگشت به منوی اصلی در صورت خطا
        return ConversationHandler.END

    # به‌روزرسانی جدول user_positions: بستن پوزیشن‌ها
    amount_remaining_to_sell = amount_to_sell

    cursor.execute(
        "SELECT position_id, amount, buy_price FROM user_positions WHERE user_id=? AND symbol=? AND status='open' ORDER BY open_timestamp ASC",
        (user_id, symbol)
    )
    individual_positions = cursor.fetchall()

    EPSILON = 1e-7  # آستانه کوچک برای مقادیر بسیار ریز

    for pos_id, pos_amount, pos_buy_price in individual_positions:
        if amount_remaining_to_sell <= EPSILON:
            break  # کل مقدار درخواستی فروخته شده است

        if pos_amount <= amount_remaining_to_sell + EPSILON:
            # فروش کل پوزیشن فعلی
            closed_amount = pos_amount
            cost_of_this_chunk = closed_amount * pos_buy_price
            revenue_of_this_chunk = closed_amount * new_current_sell_price
            commission_on_this_chunk = revenue_of_this_chunk * COMMISSION_RATE
            net_revenue_of_this_chunk = revenue_of_this_chunk - commission_on_this_chunk

            pnl_for_this_chunk = net_revenue_of_this_chunk - cost_of_this_chunk

            cursor.execute(
                "UPDATE user_positions SET status='closed', closed_price=?, close_timestamp=?, profit_loss=?, commission_paid = COALESCE(commission_paid, 0) + ? WHERE position_id=?",
                (new_current_sell_price, datetime.datetime.now().isoformat(), pnl_for_this_chunk,
                 commission_on_this_chunk, pos_id)
            )
            logging.info(
                f"Closed full position {pos_id} for user {user_id}. Amount: {closed_amount:.6f}, PnL: {pnl_for_this_chunk:+.2f}")

            total_pnl_from_this_sell_transaction += pnl_for_this_chunk  # جمع‌آوری PnL
            total_commission_from_this_sell_transaction += commission_on_this_chunk  # جمع‌آوری کارمزد
            amount_remaining_to_sell -= closed_amount
        else:
            # فروش جزئی از پوزیشن فعلی
            closed_amount = amount_remaining_to_sell
            remaining_amount_in_pos = pos_amount - closed_amount

            cost_of_this_chunk = closed_amount * pos_buy_price
            revenue_of_this_chunk = closed_amount * new_current_sell_price
            commission_on_this_chunk = revenue_of_this_chunk * COMMISSION_RATE
            net_revenue_of_this_chunk = revenue_of_this_chunk - commission_on_this_chunk

            pnl_for_this_chunk = net_revenue_of_this_chunk - cost_of_this_chunk

            cursor.execute(
                "UPDATE user_positions SET amount=?, profit_loss = COALESCE(profit_loss, 0) + ?, commission_paid = COALESCE(commission_paid, 0) + ? WHERE position_id=?",
                (remaining_amount_in_pos, pnl_for_this_chunk, commission_on_this_chunk, pos_id)
            )
            logging.info(
                f"Partially closed position {pos_id} for user {user_id}. Sold: {closed_amount:.6f}, Remaining: {remaining_amount_in_pos:.6f}, PnL: {pnl_for_this_chunk:+.2f}")

            total_pnl_from_this_sell_transaction += pnl_for_this_chunk  # جمع‌آوری PnL
            total_commission_from_this_sell_transaction += commission_on_this_chunk  # جمع‌آوری کارمزد
            amount_remaining_to_sell = 0
            break

    conn.commit()  # ذخیره تغییرات جدول user_positions

    # --- حالا جدول users را با PnL و موجودی به‌روزرسانی می‌کنیم ---

    # مقادیر فعلی را از user_db_data دریافت می‌کنیم
    current_user_balance = user_db_data["balance"]
    current_total_realized_pnl = user_db_data["total_realized_pnl"]
    current_monthly_realized_pnl = user_db_data["monthly_realized_pnl"]
    current_user_commission_balance = user_db_data["user_commission_balance"]

    # محاسبه مقادیر جدید برای آپدیت
    new_user_balance = current_user_balance + new_net_revenue
    new_total_realized_pnl = current_total_realized_pnl + total_pnl_from_this_sell_transaction
    new_monthly_realized_pnl = current_monthly_realized_pnl + total_pnl_from_this_sell_transaction
    new_user_commission_balance = current_user_commission_balance + total_commission_from_this_sell_transaction

    cursor.execute("""
                   UPDATE users
                   SET balance                 = ?,
                       total_realized_pnl      = ?,
                       monthly_realized_pnl    = ?,
                       user_commission_balance = ?
                   WHERE user_id = ?
                   """,
                   (new_user_balance, new_total_realized_pnl, new_monthly_realized_pnl, new_user_commission_balance,
                    user_id))
    conn.commit()  # ذخیره تغییرات جدول users

    logging.info(
        f"اطلاعات کاربر {user_id} به‌روزرسانی شد: موجودی={new_user_balance:.2f}, PnL کلی={new_total_realized_pnl:.2f}, PnL ماهانه={new_monthly_realized_pnl:.2f}, کارمزد کاربر={new_user_commission_balance:.2f}")

    # اضافه کردن کارمزد به موجودی ربات (اطمینان حاصل کنید که این تابع در دسترس است)
    # total_commission_from_this_sell_transaction شامل کارمزد تمام chunk های فروخته شده است
    add_bot_commission(total_commission_from_this_sell_transaction)

    # --- بررسی و ارتقاء سطح VIP کاربر ---
    # این تابع سطح VIP کاربر را بر اساس ارزش پورتفوی بررسی می‌کند
    # و در صورت نیاز، آن را ارتقاء داده و پیام تبریک VIP ارسال می‌کند.
    await check_and_upgrade_vip_level(user_id, context)

    # --- به‌روزرسانی پیام نهایی برای نمایش PnL ---
    now_for_display = datetime.datetime.now()  # برای نمایش ماه فعلی در پیام

    final_message_text = (
        f"✅ **فروش شما با موفقیت انجام شد!** ✅\n\n"
        f"💎 ارز: **{symbol}**\n"
        f"📈 قیمت فروش: **${format_price(new_current_sell_price)}**\n"
        f"🔢 مقدار فروخته شده: **{amount_to_sell:.6f} واحد**\n"
        f"💸 کارمزد پرداخت شده: **{total_commission_from_this_sell_transaction:.2f} چیپ**\n"
        f"💰 درآمد خالص: **{new_net_revenue:.2f} چیپ**\n"
        f"سود/زیان ناشی از این فروش: **{total_pnl_from_this_sell_transaction:+.2f} چیپ**\n"
        f"💰 موجودی فعلی شما: **{new_user_balance:.2f} چیپ**\n\n"
        f"**📊 سود/زیان کلی معاملات بسته شده: {new_total_realized_pnl:+.2f} چیپ**\n"
        f"**🗓️ سود/زیان ماهانه (از ابتدای {now_for_display.strftime('%B %Y')}): {new_monthly_realized_pnl:+.2f} چیپ**\n\n"
        f"اکنون می‌توانید موجودی خود را بررسی کنید یا معامله جدیدی شروع کنید."
    )

    try:
        await query.edit_message_text( # استفاده از query
            final_message_text,
            reply_markup=get_trade_active_keyboard(),
            parse_mode='Markdown'
        )
    except telegram.error.BadRequest as e:
        logging.warning(
            f"Failed to edit message for successful sell order: {e}. Sending new message instead.")
        await query.message.reply_text( # استفاده از query.message.reply_text
            final_message_text,
            reply_markup=get_trade_active_keyboard(),
            parse_mode='Markdown'
        )
    except Exception as e:
        logging.error(f"Unexpected error sending final sell message for user {user_id}: {e}")
        await query.message.reply_text( # استفاده از query.message.reply_text
            "فروش شما انجام شد، اما مشکلی در ارسال پیام تأیید رخ داد. لطفاً موجودی خود را بررسی کنید.",
            reply_markup=get_trade_active_keyboard()
        )
    context.user_data['reconfirmed_price'] = None  # ریست وضعیت
    context.user_data['initial_displayed_price'] = None  # ریست وضعیت
    return ConversationHandler.END

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """هندلر کامند /rules"""
    rules_text = (
        "📜 **قوانین و مقررات ربات CryptoDemoTrade:**\n\n"
        "1. این ربات صرفاً برای **شبیه‌سازی و آموزش ترید ارز دیجیتال** است و هیچگونه خرید و فروش واقعی انجام نمی‌دهد.\n"
        "2. تمامی معاملات با **چیپ‌های مجازی** انجام می‌شود و فاقد هرگونه ارزش مالی حقیقی است.\n"
        "3. فعالیت‌های مربوط به **قمار، شرط‌بندی، و مسابقات با جوایز واقعی** در این ربات ممنوع است.\n"
        "4. هرگونه سوءاستفاده یا تلاش برای دستکاری سیستم منجر به مسدود شدن کاربر خواهد شد.\n"
        "5. اطلاعات کاربران (مانند موجودی چیپ‌های مجازی) محرمانه تلقی شده و با هیچ شخص ثالثی به اشتراک گذاشته نمی‌شود.\n"
        "6. با استفاده از این ربات، شما موافقت خود را با تمامی قوانین فوق اعلام می‌کنید.\n\n"
        "**تغییرات:** قوانین ممکن است در آینده به‌روزرسانی شوند. لطفاً برای اطلاع از آخرین تغییرات به این بخش مراجعه کنید."
    )
    await update.message.reply_text(rules_text, parse_mode=telegram.constants.ParseMode.MARKDOWN)


# --- وظیفه پس‌زمینه برای بروزرسانی کش قیمت‌ها ---





# --- Main Application Setup ---
async def update_missing_coin_slugs_in_user_positions(): # <--- نام تابع را تغییر دادم
    """
    موقعیت‌های باز کاربر را بررسی کرده و ستون coin_slug آن‌ها را بر اساس SYMBOL_TO_SLUG_MAP پر می‌کند.
    این تابع برای اصلاح رکوردهای قدیمی استفاده می‌شود که coin_slug آن‌ها NULL است.
    """
    global conn, cursor, SYMBOL_TO_SLUG_MAP  # مطمئن شوید که اینها گلوبال هستند

    logging.info("Starting one-off task: Updating missing coin_slugs in user_positions...")

    if not conn or not cursor:
        logging.error("Database connection not available. Cannot update missing coin_slugs.")
        return False

    # اگر SYMBOL_TO_SLUG_MAP خالی بود، یعنی احتمالا fetch_top_coins به درستی اجرا نشده
    if not SYMBOL_TO_SLUG_MAP:
        logging.warning(
            "SYMBOL_TO_SLUG_MAP is empty. Cannot update coin_slugs. Ensure top coins are fetched correctly.")
        # این تابع به هر حال اجرا می‌شود، پس می‌توانیم True برگردانیم تا اجرای بات متوقف نشود
        return True # False را به True تغییر دادم تا در صورت خالی بودن مپ، بات ادامه یابد.

    try:
        # استفاده از 'id' به جای 'position_id' اگر 'id' کلید اصلی پوزیشن‌هاست
        cursor.execute("SELECT position_id, symbol FROM user_positions WHERE coin_slug IS NULL OR coin_slug = ''") # اضافه کردم OR coin_slug = ''
        positions_to_update = cursor.fetchall()

        updated_count = 0
        if not positions_to_update:
            logging.info("No missing coin_slugs found in user_positions. Database is clean for this field.")
            return True

        for pos_id, symbol in positions_to_update: # تغییر position_id به pos_id برای هماهنگی با id دیتابیس
            slug = SYMBOL_TO_SLUG_MAP.get(symbol)

            if slug:
                # استفاده از 'id' به جای 'position_id'
                cursor.execute("UPDATE user_positions SET coin_slug = ? WHERE position_id = ?", (slug, pos_id))
                updated_count += 1
                logging.info(f"Updated position {pos_id} for symbol '{symbol}' with coin_slug '{slug}'.")
            else:
                logging.warning(
                    f"Could not find coin_slug for symbol '{symbol}' (position ID: {pos_id}) in SYMBOL_TO_SLUG_MAP. Skipping update for this position.")

        conn.commit()
        logging.info(f"Finished updating {updated_count} missing coin_slugs in user_positions.")
        return True

    except sqlite3.Error as e:
        logging.error(f"Error updating missing coin_slugs in user_positions: {e}")
        conn.rollback() # در صورت خطای دیتابیس، rollback کنید
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during coin_slug update: {e}")
        conn.rollback() # در صورت خطای عمومی، rollback کنید
        return False


async def update_cached_buy_data(context: ContextTypes.DEFAULT_TYPE):
    """
    Calculates total buy amount and average buy price for all OPEN positions
    and updates the EXISTING rows in the cached_prices table using their correct coin_slug.
    It will NOT insert new rows.
    """
    logging.info("Running job to update cached buy data for open positions.")
    try:
        with sqlite3.connect('trade.db') as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # گام 1: جمع‌آوری اطلاعات خرید از پوزیشن‌های باز در user_positions
            # ما فقط به پوزیشن‌هایی نیاز داریم که status='open' و closed_price IS NULL باشند
            cursor.execute("""
                           SELECT coin_slug, amount, buy_price
                           FROM user_positions
                           WHERE status = 'open'
                             AND closed_price IS NULL
                           """)
            open_positions = cursor.fetchall()

            # دیکشنری برای نگهداری مجموع حجم و مجموع ارزش خرید برای هر coin_slug
            # مثال: {'bitcoin': {'total_amount': 0.0, 'total_value': 0.0}}
            coin_buy_summary = defaultdict(lambda: {'total_amount': 0.0, 'total_value': 0.0})

            # برای هر پوزیشن باز، حجم و ارزش خرید را به coin_buy_summary اضافه می‌کنیم
            for pos in open_positions:
                coin_slug = pos['coin_slug']
                amount = pos['amount']
                buy_price = pos['buy_price']

                # اطمینان حاصل کنید که coin_slug خالی نیست
                if coin_slug:
                    coin_buy_summary[coin_slug]['total_amount'] += amount
                    coin_buy_summary[coin_slug]['total_value'] += (amount * buy_price)
                else:
                    logging.warning(f"Position with missing coin_slug found: {pos}. Skipping for buy data calculation.")

            # گام 2: ابتدا تمام total_buy_amount و average_buy_price را در cached_prices صفر می‌کنیم
            # این کار برای اطمینان از پاک شدن داده‌های پوزیشن‌های بسته‌شده یا غیرفعال است.
            cursor.execute("UPDATE cached_prices SET total_buy_amount = 0.0, average_buy_price = 0.0")

            # گام 3: به‌روزرسانی ردیف‌های مربوطه در cached_prices
            updated_count = 0
            for coin_slug, data in coin_buy_summary.items():
                total_amount_for_slug = data['total_amount']
                total_value_for_slug = data['total_value']

                # محاسبه میانگین قیمت خرید برای این کوین_اسلاگ
                average_buy_price_for_slug = (
                            total_value_for_slug / total_amount_for_slug) if total_amount_for_slug > 0 else 0.0

                # فقط ردیف‌های موجود را در cached_prices بر اساس coin_slug به‌روزرسانی می‌کنیم
                # اگر coin_slug در cached_prices وجود نداشته باشد، هیچ آپدیتی انجام نمی‌شود.
                cursor.execute("""
                               UPDATE cached_prices
                               SET total_buy_amount  = ?,
                                   average_buy_price = ?
                               WHERE coin_slug = ?
                               """, (total_amount_for_slug, average_buy_price_for_slug, coin_slug))

                if cursor.rowcount > 0:
                    updated_count += 1
                else:
                    # این هشدار به ما می‌گوید که یک coin_slug از user_positions
                    # در cached_prices پیدا نشده است. این مشکل باید بررسی شود!
                    logging.warning(
                        f"Coin slug '{coin_slug}' from user_positions not found in cached_prices. Buy data not updated for this slug.")

            logging.info(f"Successfully updated cached buy data for {updated_count} unique coins in cached_prices.")

    except Exception as e:
        logging.error(f"Error updating cached buy data: {e}")




async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    جدول رتبه‌بندی ماهانه را بر اساس سود/زیان واقعی شده در ماه جاری نمایش می‌دهد.
    """
    try:
        # فیلتر کردن کاربر با user_id = 0 و محدود کردن نتایج به 20 نفر برتر
        cursor.execute(
            "SELECT user_id, username, first_name, monthly_realized_pnl FROM users WHERE user_id != 0 ORDER BY monthly_realized_pnl DESC LIMIT 20")
        top_users = cursor.fetchall()

        if not top_users:
            await update.message.reply_text("در حال حاضر هیچ کاربری در این ماه سود یا زیانی نداشته است.")
            return

        # --- تعیین عرض دقیق برای هر ستون برای فرمت‌بندی یکنواخت ---
        # افزایش عرض برای جلوگیری از Truncation و بهبود خوانایی
        rank_col_width = 2  # برای رتبه (تا 20 نفر، 2 رقم، پس 4 کافی است)
        name_col_width = 13 # افزایش عرض نام کاربر (برای نام‌های طولانی‌تر)
        pnl_col_width = 5  # افزایش عرض سود/زیان (برای اعداد بزرگتر و عنوان)

        leaderboard_message = "🏆 * جدول رتبه‌بندی ماهانه * 🏆\n\n"

        # --- ردیف عنوان جدول (Header Row) ---
        # استفاده از عناوین فارسی و راست‌چین برای نام کاربر و سود/زیان
        leaderboard_message += f"`{'R':<{rank_col_width}} | {'User':>{name_col_width}} | {'Pnl':>{pnl_col_width}}`\n"

        # --- ردیف جداکننده (Separator Row) ---
        leaderboard_message += f"`{'-' * rank_col_width}-+-{'-' * name_col_width}-+-{'-' * pnl_col_width}`\n"

        # --- ردیف‌های داده (Data Rows) ---
        for i, user_data in enumerate(top_users):
            user_id, username, first_name, monthly_pnl = user_data

            display_name = ""
            # ترتیب ترجیح کاربر: first_name سپس username
            if first_name:
                display_name = first_name
            elif username:
                display_name = f"@{username}"
            else:
                display_name = f"کاربر {user_id}" # fallback برای نبود نام

            # کوتاه کردن نام نمایش در صورت طولانی بودن و اضافه کردن '…'
            if len(display_name) > name_col_width:
                display_name = display_name[:name_col_width - 1] + '…'

            # --- فرمت‌بندی نهایی ردیف داده‌ها ---
            # رتبه چپ‌چین، نام کاربر راست‌چین، سود/زیان راست‌چین
            leaderboard_message += f"`{i + 1:<{rank_col_width}} | {display_name:>{name_col_width}} | {monthly_pnl:>{pnl_col_width}.2f}`\n"

        leaderboard_message += "\n_نتایج ابتدای هر ماه ریست می‌شود._"

        await update.message.reply_text(leaderboard_message, parse_mode='Markdown')

    except Exception as e:
        logging.error(f"خطا در اجرای دستور /top: {e}")
        await update.message.reply_text("متاسفانه خطایی در دریافت رتبه‌بندی رخ داد. لطفا دوباره تلاش کنید.")


# ----------------- تنظیمات Logging -----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ----------------- اتصال به دیتابیس (نمونه، باید در فایل اصلی شما وجود داشته باشد) -----------------
# این بخش باید در بالاترین قسمت فایل شما یا در یک فایل config.py باشد.
try:
    conn = sqlite3.connect('trade.db', check_same_thread=False)
    cursor = conn.cursor()
except Exception as e:
    logging.error(f"Failed to connect to database: {e}")
    # اگر اتصال به دیتابیس ناموفق بود، شاید باید برنامه را متوقف کنید یا مدیریت خطا کنید.
    exit(1)


# ----------------- تابع main اصلاح شده -----------------

def main():
    """نقطه ورود اصلی برای راه‌اندازی ربات تلگرام و دیتابیس."""
    # حذف 'global conn, cursor' - اتصال به دیتابیس باید به صورت محلی مدیریت شود.
    conn = None
    cursor = None

    logging.info("Starting database initialization...")

    try:
        conn = sqlite3.connect('trade.db')
        conn.row_factory = sqlite3.Row  # این خط برای دسترسی به ستون‌ها با نام آن‌ها ضروری است
        cursor = conn.cursor()

        # 1. ایجاد یا بروزرسانی جدول users با تمام ستون‌های لازم (کد دیتابیس شما)
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS users
                       (
                           user_id
                           INTEGER
                           PRIMARY
                           KEY,
                           balance
                           REAL
                           DEFAULT
                           1000.0,
                           bot_commission_balance
                           REAL
                           DEFAULT
                           0.0,
                           user_commission_balance
                           REAL
                           DEFAULT
                           0.0,
                           total_realized_pnl
                           REAL
                           DEFAULT
                           0.0,
                           monthly_realized_pnl
                           REAL
                           DEFAULT
                           0.0,
                           last_monthly_reset_date
                           TEXT
                           DEFAULT (
                           strftime
                       (
                           '%Y-%m-%d %H:%M:%S',
                           'now'
                       )),
                           vip_level INTEGER DEFAULT 0,
                           chat_id INTEGER,
                           username TEXT,
                           first_name TEXT,
                           referrer_id INTEGER DEFAULT NULL
                           );
                       """)
        conn.commit()
        logging.info("Created 'users' table if it did not exist or confirmed structure.")

        # 2. اضافه کردن ستون‌های جدید به صورت مستقل به جدول users (برای سازگاری با دیتابیس‌های قدیمی)
        columns_to_add_users = {
            'username': 'TEXT',
            'first_name': 'TEXT',
            'bot_commission_balance': 'REAL DEFAULT 0.0',
            'user_commission_balance': 'REAL DEFAULT 0.0',
            'total_realized_pnl': 'REAL DEFAULT 0.0',
            'monthly_realized_pnl': 'REAL DEFAULT 0.0',
            'last_monthly_reset_date': "TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))",
            'vip_level': 'INTEGER DEFAULT 0',
            'chat_id': 'INTEGER',
            'referrer_id': 'INTEGER DEFAULT NULL'
        }

        for col_name, col_type in columns_to_add_users.items():
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                conn.commit()
                logging.info(f"Added '{col_name}' column to 'users' table.")
            except sqlite3.OperationalError as e:
                if f"duplicate column name: {col_name}" in str(e):
                    logging.info(f"'{col_name}' column already exists in 'users' table.")
                else:
                    logging.error(f"Error altering users table for {col_name}: {e}")

        # 3. ایجاد جدول user_positions (اگر وجود ندارد) با تمام ستون‌های لازم
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS user_positions
                       (
                           position_id
                           INTEGER
                           PRIMARY
                           KEY
                           AUTOINCREMENT,
                           user_id
                           INTEGER,
                           symbol
                           TEXT,
                           amount
                           REAL,
                           buy_price
                           REAL,
                           open_timestamp
                           DATETIME
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           status
                           TEXT
                           DEFAULT
                           'open',
                           closed_price
                           REAL,
                           close_timestamp
                           DATETIME,
                           profit_loss
                           REAL,
                           commission_paid
                           REAL
                           DEFAULT
                           0.0,
                           tp_price
                           REAL
                           DEFAULT
                           0.0,
                           sl_price
                           REAL
                           DEFAULT
                           0.0,
                           take_profit_price
                           REAL
                           DEFAULT
                           0.0,
                           stop_loss_price
                           REAL
                           DEFAULT
                           0.0,
                           coin_slug
                           TEXT
                       );
                       ''')
        conn.commit()
        logging.info("Created 'user_positions' table if it did not exist.")

        # --- 4. اضافه کردن ستون 'username' به جدول user_positions ---
        try:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN username TEXT")
            conn.commit()
            logging.info("Added 'username' column to 'user_positions' table.")
        except sqlite3.OperationalError as e:
            if "duplicate column name: username" in str(e):
                logging.info("'username' column already exists in 'user_positions' table.")
            else:
                logging.error(f"Error altering user_positions table for 'username': {e}")

        # 5. اضافه کردن ستون‌های جدید به صورت مستقل به جدول user_positions (برای سازگاری با دیتابیس‌های قدیمی)
        columns_to_add_positions = {
            'tp_price': 'REAL DEFAULT 0.0',
            'sl_price': 'REAL DEFAULT 0.0',
            'take_profit_price': 'REAL DEFAULT 0.0',
            'stop_loss_price': 'REAL DEFAULT 0.0',
            'coin_slug': 'TEXT',
            'closed_price': 'REAL DEFAULT 0.0'
        }

        for col_name, col_type in columns_to_add_positions.items():
            try:
                cursor.execute(f"ALTER TABLE user_positions ADD COLUMN {col_name} {col_type}")
                conn.commit()
                logging.info(f"Added '{col_name}' column to 'user_positions' table.")
            except sqlite3.OperationalError as e:
                if f"duplicate column name: {col_name}" in str(e):
                    logging.info(f"'{col_name}' column already exists in 'user_positions' table.")
                else:
                    logging.error(f"Error altering user_positions table for {col_name}: {e}")

        # 6. ایجاد جدول cached_prices (با coin_slug به عنوان PRIMARY KEY)
        # ** تغییرات اصلی برای اضافه کردن دو ستون جدید به cached_prices در اینجا **
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS cached_prices
                       (
                           coin_slug
                           TEXT
                           PRIMARY
                           KEY,
                           price
                           REAL,
                           last_updated
                           DATETIME,
                           total_buy_amount
                           REAL
                           DEFAULT
                           0.0, -- ** ستون جدید: حجم کل خرید شده **
                           average_buy_price
                           REAL
                           DEFAULT
                           0.0  -- ** ستون جدید: میانگین قیمت خرید **
                       );
                       ''')
        conn.commit()
        logging.info("Created 'cached_prices' table if it did not exist.")

        # --- 7. اضافه کردن ستون‌های جدید به cached_prices (برای سازگاری با دیتابیس‌های قدیمی) ---
        columns_to_add_cached_prices = {
            'total_buy_amount': 'REAL DEFAULT 0.0',
            'average_buy_price': 'REAL DEFAULT 0.0'
        }
        for col_name, col_type in columns_to_add_cached_prices.items():
            try:
                cursor.execute(f"ALTER TABLE cached_prices ADD COLUMN {col_name} {col_type}")
                conn.commit()
                logging.info(f"Added '{col_name}' column to 'cached_prices' table.")
            except sqlite3.OperationalError as e:
                if f"duplicate column name: {col_name}" in str(e):
                    logging.info(f"'{col_name}' column already exists in 'cached_prices' table.")
                else:
                    logging.error(f"Error altering cached_prices table for {col_name}: {e}")

        # 8. اضافه کردن جدول referral_rewards برای ردیابی پاداش‌ها (NEW TABLE)
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS referral_rewards
                       (
                           referrer_id
                           INTEGER,
                           new_user_id
                           INTEGER
                           PRIMARY
                           KEY,
                           reward_given_timestamp
                           DATETIME
                           DEFAULT
                           CURRENT_TIMESTAMP,
                           FOREIGN
                           KEY
                       (
                           referrer_id
                       ) REFERENCES users
                       (
                           user_id
                       ),
                           FOREIGN KEY
                       (
                           new_user_id
                       ) REFERENCES users
                       (
                           user_id
                       )
                           );
                       ''')
        conn.commit()
        logging.info("Created 'referral_rewards' table if it did not exist.")

        # 9. ایجاد رکورد برای user_id = 0 (ربات) اگر وجود نداشته باشد
        cursor.execute("""
                       INSERT
                       OR IGNORE INTO users(user_id, balance, bot_commission_balance, user_commission_balance,
                                       total_realized_pnl, monthly_realized_pnl, last_monthly_reset_date,
                                       vip_level, chat_id, username, first_name, referrer_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       """,
                       (0, 0.0, 0.0, 0.0, 0.0, 0.0, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 0, None,
                        "Bot",
                        "Admin", None))
        conn.commit()
        logging.info("Ensured user_id 0 (bot) exists in users table.")
        logging.info("Database initialization complete.")



        # ** post_init باید اینجا به ApplicationBuilder پاس داده شود **
        app = Application.builder().token(TOKEN).post_init(post_init).read_timeout(30).write_timeout(30).build()



        # --- Admin Conversation Handler ---
        # تعریف استیت ها (اینها باید در ابتدای فایل به صورت گلوبال تعریف شده باشند)
        # ADMIN_PANEL, ADMIN_SELECT_USER, ADMIN_SELECTED_USER_ACTIONS, ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT, ADMIN_MANAGE_BALANCE_USER_ID, ADMIN_MANAGE_BALANCE_AMOUNT, ADMIN_BROADCAST_MESSAGE = range(7)

        admin_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler("admin_panel", admin_panel_command),
                CallbackQueryHandler(admin_panel_command, pattern='^admin_panel$')
            ],
            states={
                ADMIN_PANEL: [
                    CallbackQueryHandler(admin_stats_command, pattern='^admin_stats$'),
                    CallbackQueryHandler(show_user_list_for_admin, pattern='^admin_manage_balance$'),
                    CallbackQueryHandler(admin_broadcast_entry, pattern='^admin_broadcast$'),
                    CallbackQueryHandler(admin_all_open_positions, pattern="^admin_all_open_positions$"),
                    CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$')
                ],
                ADMIN_SELECT_USER: [
                    CallbackQueryHandler(admin_selected_user_action, pattern=r"^admin_select_user:\d+$"),
                    CallbackQueryHandler(admin_panel_command, pattern="^admin_panel$")
                ],
                ADMIN_SELECTED_USER_ACTIONS: [
                    CallbackQueryHandler(admin_initiate_balance_change,
                                         pattern="^admin_change_balance:(add|deduct):(\d+)$"),
                    CallbackQueryHandler(admin_view_user_positions, pattern="^admin_view_user_positions:(\d+)$"),
                    CallbackQueryHandler(show_user_list_for_admin, pattern="^admin_back_to_user_list$"),
                    CallbackQueryHandler(admin_panel_command, pattern="^admin_panel$"),
                ],
                ADMIN_AWAITING_BALANCE_CHANGE_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, admin_process_balance_change),
                    CallbackQueryHandler(admin_panel_command, pattern="^admin_panel$"),
                    CallbackQueryHandler(admin_selected_user_action, pattern=r"^admin_select_user:\d+$"),
                ],
                ADMIN_BROADCAST_MESSAGE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_broadcast),
                    CommandHandler("cancel", admin_cancel_command),
                    CallbackQueryHandler(admin_panel_command, pattern='^admin_panel$'),
                    CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$')
                ],
                ADMIN_MANAGE_BALANCE_USER_ID: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_user_id_for_balance),
                    CommandHandler("cancel", admin_cancel_command),
                    CallbackQueryHandler(admin_panel_command, pattern='^admin_panel$'),
                    CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$')
                ],
                ADMIN_MANAGE_BALANCE_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_user_balance),
                    CommandHandler("cancel", admin_cancel_command),
                    CallbackQueryHandler(admin_panel_command, pattern='^admin_panel$'),
                    CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$')
                ],
            },
            fallbacks=[
                CommandHandler("cancel", admin_cancel_command),
                CallbackQueryHandler(admin_panel_command, pattern='^admin_panel$'),
                CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$')
            ],
            map_to_parent={
                ConversationHandler.END: ConversationHandler.END
            },
            allow_reentry=True
        )

        # --- Main Trading Conversation Handler ---
        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(trade_entry_point, pattern='^start_trade$'),
                CallbackQueryHandler(trade_entry_point, pattern='^start_trade_new$'),
                CallbackQueryHandler(sell_portfolio_entry_point, pattern='^sell_portfolio_entry$'),
                CommandHandler("trade", trade_command)
            ],
            states={
                CHOOSING_COIN: [
                    CallbackQueryHandler(button_callback, pattern='^select_coin_'),
                    CallbackQueryHandler(button_callback, pattern='^coins_page_')
                ],
                ENTERING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input)],
                CONFIRM_BUY: [CallbackQueryHandler(process_buy_order, pattern='^confirm_buy$')],
                RECONFIRM_BUY: [CallbackQueryHandler(process_buy_order, pattern='^confirm_buy$')],
                ASKING_TP_SL: [
                    CallbackQueryHandler(handle_tpsl_choice, pattern='^set_tpsl$'),
                ],
                ENTERING_TP_PRICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tpsl_input),
                ],
                ENTERING_SL_PRICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_tpsl_input),
                ],
                CHOOSING_COIN_TO_SELL: [
                    CallbackQueryHandler(choose_coin_to_sell, pattern='^sell_coin_'),
                    CallbackQueryHandler(button_callback, pattern='^(?!sell_coin_).*')
                ],
                ENTERING_SELL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sell_amount_input)],
                CONFIRM_SELL: [CallbackQueryHandler(process_sell_order, pattern='^confirm_sell_final$')],
                RECONFIRM_SELL: [CallbackQueryHandler(process_sell_order, pattern='^confirm_sell_final$')]
            },

            fallbacks=[
                CommandHandler("cancel", cancel_conversation),
                MessageHandler(filters.Regex("^(بازگشت به منو اصلی|لغو)$"), back_to_main_menu),
                CallbackQueryHandler(cancel_trade, pattern='^cancel_trade$'),
                CallbackQueryHandler(button_callback, pattern='^back_to_previous_step$'),
                CallbackQueryHandler(back_to_main_menu, pattern='^back_to_main_menu$')
            ],
            allow_reentry=True
        )

        # --- Add Handlers to Application ---
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("mybalance", show_balance_and_portfolio))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("top", top_command))
        app.add_handler(CommandHandler("rules", rules_command))
        app.add_handler(CommandHandler("invitefriends", invite_friends_command))

        app.add_handler(admin_conv_handler)
        app.add_handler(conv_handler)

        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_handler(CallbackQueryHandler(invite_friends_command, pattern='^invite_friends$'))

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       lambda u, c: u.message.reply_text("لطفاً از دکمه‌های موجود استفاده کنید.",
                                                                         reply_markup=get_main_menu_keyboard())))

        logging.info("Bot is polling for updates...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        logging.error(f"An error occurred during bot startup: {e}")
    finally:
        if conn:
            conn.close()
            logging.info("Database connection closed.")


if __name__ == "__main__":
    main()