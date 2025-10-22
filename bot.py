import os
import logging
import base64
import asyncio
import time
import aiohttp
import sqlite3
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.error import BadRequest, RetryAfter
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'
log_level = logging.DEBUG if DEBUG_MODE else logging.INFO

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=log_level
)
logger = logging.getLogger(__name__)

if DEBUG_MODE:
    logger.info("🐛 DEBUG режим включен")

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', '0'))
# ComfyUI-Connect endpoint для workflow 'api-video'
API_URL = 'https://cuda.serge.cc/api/connect/workflows/api-video'

# Настройки токенов
TOKENS_PER_VIDEO = int(os.getenv('TOKENS_PER_VIDEO', '10'))
DEFAULT_TOKENS = int(os.getenv('DEFAULT_TOKENS', '100'))

# Конфигурация параметров создания видео
DURATIONS = {
    '5': {
        'seconds': 5,
        'cost': 5,
        'name': '⚡ 5 секунд',
        'description': 'Короткое видео для Stories',
        'emoji': '⚡',
    },
    '10': {
        'seconds': 10,
        'cost': 10,
        'name': '⭐ 10 секунд',
        'description': 'Оптимальная длительность',
        'emoji': '⭐',
        'recommended': True,
    },
    '15': {
        'seconds': 15,
        'cost': 15,
        'name': '🎬 15 секунд',
        'description': 'Полное видео',
        'emoji': '🎬',
    }
}

QUALITIES = {
    'low': {
        'name': '📱 Низкое',
        'pixels': 300,
        'cost_modifier': 0,
        'description': 'Быстрая загрузка',
        'emoji': '📱',
    },
    'medium': {
        'name': '💎 Среднее',
        'pixels': 600,
        'cost_modifier': 0,
        'description': 'Баланс качества и размера',
        'emoji': '💎',
        'recommended': True,
    },
    'high': {
        'name': '🎯 Высокое',
        'pixels': 832,
        'cost_modifier': 5,
        'description': 'Максимальное качество',
        'emoji': '🎯',
    }
}

# Статистика обработки
class ProcessingStats:
    def __init__(self, stats_file='processing_stats.json'):
        self.stats_file = stats_file
        self.times = []  # Старый формат для совместимости
        self.times_by_settings = {}  # Новый формат: {"duration_quality": [times]}
        self.load()
    
    def load(self):
        """Загрузить статистику из файла"""
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, 'r') as f:
                    import json
                    data = json.load(f)
                    
                    # Поддержка старого формата от bot_old.py
                    if 'completion_times' in data:
                        self.times = data['completion_times']
                        logger.info(f"📊 Загружено {len(self.times)} записей (старый формат)")
                    else:
                        self.times = data.get('times', [])
                        self.times_by_settings = data.get('times_by_settings', {})
                        logger.info(f"📊 Загружено {len(self.times)} записей + {len(self.times_by_settings)} настроек")
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")
            self.times = []
            self.times_by_settings = {}
    
    def save(self):
        """Сохранить статистику в файл"""
        try:
            import json
            with open(self.stats_file, 'w') as f:
                json.dump({
                    'times': self.times,
                    'times_by_settings': self.times_by_settings
                }, f)
        except Exception as e:
            logger.error(f"Ошибка сохранения статистики: {e}")
    
    def add_time(self, duration, video_duration=None, quality=None):
        """Добавить время обработки"""
        self.times.append(duration)
        
        # Если есть настройки - сохраняем по ключу
        if video_duration is not None and quality is not None:
            key = f"{video_duration}_{quality}"
            if key not in self.times_by_settings:
                self.times_by_settings[key] = []
            self.times_by_settings[key].append(duration)
            
            # Храним только последние 20 записей для каждой настройки
            if len(self.times_by_settings[key]) > 20:
                self.times_by_settings[key] = self.times_by_settings[key][-20:]
        
        # Храним только последние 100 записей в общем списке
        if len(self.times) > 100:
            self.times = self.times[-100:]
        self.save()
        logger.info(f"📊 Время обработки: {format_time(duration)}, всего записей: {len(self.times)}")
    
    def get_times(self):
        """Получить все времена"""
        return self.times
    
    def get_average(self):
        """Среднее время последних 10 записей"""
        if not self.times:
            return 120
        recent = self.times[-10:]
        return sum(recent) / len(recent)
    
    def get_average_by_settings(self, video_duration, quality):
        """Получить среднее время для конкретных настроек"""
        key = f"{video_duration}_{quality}"
        if key in self.times_by_settings and self.times_by_settings[key]:
            times = self.times_by_settings[key]
            return sum(times) / len(times)
        return None

processing_stats = ProcessingStats()

# Система балансов
class TokenBalance:
    def __init__(self, db_path='balances.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Создаем таблицу если не существует
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                tokens INTEGER NOT NULL DEFAULT 0,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                videos_created INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Миграция: добавляем новые поля если их нет
        cursor.execute("PRAGMA table_info(balances)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'first_name' not in columns:
            logger.info("📝 Миграция: добавляем поле first_name")
            cursor.execute('ALTER TABLE balances ADD COLUMN first_name TEXT')
        
        if 'last_name' not in columns:
            logger.info("📝 Миграция: добавляем поле last_name")
            cursor.execute('ALTER TABLE balances ADD COLUMN last_name TEXT')
        
        if 'videos_created' not in columns:
            logger.info("📝 Миграция: добавляем поле videos_created")
            cursor.execute('ALTER TABLE balances ADD COLUMN videos_created INTEGER DEFAULT 0')
            cursor.execute('UPDATE balances SET videos_created = 0 WHERE videos_created IS NULL')
        
        conn.commit()
        conn.close()
        logger.info("💾 База данных балансов готова")
    
    def get_balance(self, user_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT tokens FROM balances WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return result[0]
        else:
            self.add_tokens(user_id, DEFAULT_TOKENS)
            return DEFAULT_TOKENS
    
    def add_tokens(self, user_id, amount, username=None, first_name=None, last_name=None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO balances (user_id, tokens, username, first_name, last_name) 
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) 
            DO UPDATE SET 
                tokens = tokens + ?, 
                username = COALESCE(?, username),
                first_name = COALESCE(?, first_name),
                last_name = COALESCE(?, last_name),
                updated_at = CURRENT_TIMESTAMP
        ''', (user_id, amount, username, first_name, last_name, amount, username, first_name, last_name))
        
        conn.commit()
        
        cursor.execute('SELECT tokens FROM balances WHERE user_id = ?', (user_id,))
        new_balance = cursor.fetchone()[0]
        conn.close()
        
        logger.info(f"💰 +{amount} токенов для {user_id} ({username}), баланс: {new_balance}")
        return new_balance
    
    def increment_videos(self, user_id):
        """Увеличить счетчик созданных видео"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE balances 
            SET videos_created = videos_created + 1 
            WHERE user_id = ?
        ''', (user_id,))
        conn.commit()
        conn.close()
    
    def spend_tokens(self, user_id, amount):
        balance = self.get_balance(user_id)
        
        if balance < amount:
            return False
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE balances 
            SET tokens = tokens - ?, updated_at = CURRENT_TIMESTAMP 
            WHERE user_id = ?
        ''', (amount, user_id))
        conn.commit()
        conn.close()
        
        logger.info(f"💸 -{amount} токенов для {user_id}, осталось: {balance - amount}")
        return True
    
    def get_all_users(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, tokens, username, first_name, last_name, videos_created, 
                   created_at, updated_at 
            FROM balances 
            ORDER BY tokens DESC
        ''')
        users = cursor.fetchall()
        conn.close()
        return users

token_balance = TokenBalance()

# Константы для мастера создания видео

def calculate_cost(duration, quality):
    """Рассчитать итоговую стоимость"""
    base_cost = DURATIONS[str(duration)]['cost']
    quality_mod = QUALITIES[quality]['cost_modifier']
    return base_cost + quality_mod

def format_size_kb(bytes):
    """Форматировать размер в KB"""
    return f"{bytes // 1024} KB"

def format_time(seconds):
    """Форматирование времени в читаемый вид"""
    if seconds < 60:
        return f"{int(seconds)}с"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}м {secs}с"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}ч {minutes}м"

def get_estimated_time(duration, quality):
    """Рассчитать примерное время создания видео"""
    # Сначала пробуем найти точное время для этих настроек
    exact_time = processing_stats.get_average_by_settings(duration, quality)
    if exact_time:
        logger.info(f"🎯 Используем точное время для {duration}с/{quality}: {format_time(exact_time)}")
        return format_time(exact_time)
    
    # Если нет точного времени - используем общее среднее (это основной fallback)
    general_average = processing_stats.get_average()
    if general_average > 0:
        logger.info(f"📊 Используем общее среднее время: {format_time(general_average)}")
        return format_time(general_average)
    
    # Если вообще нет истории - используем разумное время по умолчанию
    default_time = 120  # 2 минуты по умолчанию
    logger.info(f"🔮 Используем время по умолчанию: {format_time(default_time)}")
    return format_time(default_time)

def get_average_time():
    """Получить среднее время обработки (последние 10 запросов)"""
    return processing_stats.get_average()

def get_progress_bar(progress):
    """Создать прогресс-бар"""
    filled = int(progress * 20)
    return "▓" * filled + "░" * (20 - filled)

async def safe_edit_message(message, text, max_retries=3):
    """Безопасное редактирование сообщения с обработкой ошибок"""
    for attempt in range(max_retries):
        try:
            await message.edit_text(text)
            return True
        except RetryAfter as e:
            logger.warning(f"Rate limit, ждем {e.retry_after}с")
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return True
            elif "Message can't be edited" in str(e):
                return False
            else:
                logger.error(f"BadRequest: {e}")
                return False
        except Exception as e:
            logger.error(f"Ошибка редактирования: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
    return False

def create_main_menu():
    """Создать главное меню"""
    keyboard = [
        [
            InlineKeyboardButton("🎬 Создать видео", callback_data='create_video'),
            InlineKeyboardButton("⚡ Быстрый режим", callback_data='quick_mode')
        ],
        [
            InlineKeyboardButton("💰 Баланс", callback_data='balance'),
            InlineKeyboardButton("📊 Статистика", callback_data='stats')
        ],
        [
            InlineKeyboardButton("❓ Помощь", callback_data='help')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    user = update.effective_user
    first_name = user.first_name or user.username or "Пользователь"
    last_name = user.last_name
    username = user.username
    
    # Обновляем информацию о пользователе
    balance = token_balance.get_balance(user_id)
    token_balance.add_tokens(user_id, 0, username, first_name, last_name)
    
    # Проверяем новый ли пользователь
    is_new_user = balance == DEFAULT_TOKENS
    
    if is_new_user:
        # Онбординг для новых пользователей
        text = f"""🎉 Добро пожаловать, {first_name}!

Я создаю анимированные видео из ваших фотографий с помощью ИИ.

🎁 **Бонус новичка**: {DEFAULT_TOKENS} токенов в подарок!

🎬 **Как это работает:**
• Отправьте фото → получите видео
• Выберите длительность и качество
• Стоимость: от 5 токенов

Готовы попробовать?"""
    else:
        # Обычное приветствие
        avg_time = get_average_time()
        times = processing_stats.get_times()
        stats_text = ""
        if times:
            stats_text = f"\n📊 Среднее время: {format_time(avg_time)}"
        
        text = f"""👋 Привет, {first_name}!

Я создаю анимированные видео из фотографий!{stats_text}

💰 **Ваш баланс**: {balance} токенов
💵 **Стоимость**: от 5 токенов

Выберите действие:"""
    
    await update.message.reply_text(
        text,
        reply_markup=create_main_menu()
    )

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок главного меню"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == 'create_video':
        # Запускаем мастер создания видео
        await query.edit_message_text(
            "🎬 **Мастер создания видео**\n\n"
            "Отправьте фото для создания видео с выбором параметров:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        # Создаем сессию мастера и устанавливаем состояние ожидания фото
        context.user_data['create_session'] = {
            'user_id': user_id,
            'username': query.from_user.username or query.from_user.first_name
        }
        context.user_data['waiting_for_photo'] = 'wizard'
        
    elif data == 'quick_mode':
        # Быстрый режим
        await query.edit_message_text(
            "⚡ **Быстрый режим**\n\n"
            "Отправьте фото для быстрого создания видео (10 сек, среднее качество):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        # Устанавливаем состояние ожидания фото для быстрого режима
        context.user_data['waiting_for_photo'] = 'quick'
        
    elif data == 'balance':
        # Показываем баланс
        balance = token_balance.get_balance(user_id)
        username = query.from_user.username or query.from_user.first_name or "Пользователь"
        
        await query.edit_message_text(
            f"💰 **Ваш баланс**\n\n"
            f"👤 {username}\n"
            f"🪙 Токенов: {balance}\n"
            f"🎬 Доступно видео: {balance // 5}\n\n"
            f"💡 Один токен = 1 секунда видео",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        
    elif data == 'stats':
        # Показываем статистику
        times = processing_stats.get_times()
        if times:
            avg_time = get_average_time()
            fastest = min(times)
            slowest = max(times)
            recent_times = times[-10:] if len(times) >= 10 else times
            recent_str = ", ".join([format_time(t) for t in recent_times])
            
            text = f"""📊 **Статистика обработки** ({len(times)} видео)

⚡ **Быстрее всего**: {format_time(fastest)}
📈 **В среднем**: {format_time(avg_time)}
🐌 **Дольше всего**: {format_time(slowest)}

🔄 **Последние 10**: {recent_str}"""
        else:
            text = "📊 **Статистика обработки**\n\nПока нет данных"
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        
    elif data == 'help':
        # Показываем помощь
        await query.edit_message_text(
            "❓ **Помощь**\n\n"
            "🎬 **Создание видео:**\n"
            "• Мастер: выбор длительности и качества\n"
            "• Быстрый: 10 сек, среднее качество\n\n"
            "💰 **Токены:**\n"
            "• 5 токенов = 5 секунд видео\n"
            "• 10 токенов = 10 секунд видео\n"
            "• 15 токенов = 15 секунд видео\n\n"
            "📱 **Навигация:**\n"
            "• Все действия через кнопки\n"
            "• /start - главное меню\n"
            "• Кнопка 'Назад' везде\n\n"
            "💡 **Совет:** Просто отправьте фото для быстрого создания!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        
    elif data == 'create_more':
        # Создать еще через мастер
        await query.answer("🎬 Запускаю мастер создания видео")
        await query.message.reply_text(
            "🎬 **Мастер создания видео**\n\n"
            "Отправьте фото для создания видео с выбором параметров:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        # Создаем сессию мастера и устанавливаем состояние ожидания фото
        context.user_data['create_session'] = {
            'user_id': user_id,
            'username': query.from_user.username or query.from_user.first_name
        }
        context.user_data['waiting_for_photo'] = 'wizard'
        
    elif data == 'quick_more':
        # Быстрый режим
        await query.answer("⚡ Запускаю быстрый режим")
        await query.message.reply_text(
            "⚡ **Быстрый режим**\n\n"
            "Отправьте фото для быстрого создания видео (10 сек, среднее качество):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')
            ]])
        )
        # Устанавливаем состояние ожидания фото для быстрого режима
        context.user_data['waiting_for_photo'] = 'quick'
        
    elif data == 'back_to_menu':
        # Возвращаемся в главное меню
        await query.answer("🏠 Возвращаюсь в главное меню")
        user = query.from_user
        first_name = user.first_name or user.username or "Пользователь"
        balance = token_balance.get_balance(user_id)
        
        text = f"""👋 Привет, {first_name}!

Я создаю анимированные видео из фотографий!

💰 **Ваш баланс**: {balance} токенов
💵 **Стоимость**: от 5 токенов

Выберите действие:"""
        
        await query.message.reply_text(
            text,
            reply_markup=create_main_menu()
        )
        # Сбрасываем состояние ожидания фото и сессию мастера
        context.user_data.pop('waiting_for_photo', None)
        context.user_data.pop('create_session', None)

def create_generate_more_menu():
    """Создать меню 'Создать еще'"""
    keyboard = [
        [
            InlineKeyboardButton("🎬 Создать еще", callback_data='create_more'),
            InlineKeyboardButton("⚡ Быстрый режим", callback_data='quick_more')
        ],
        [
            InlineKeyboardButton("🏠 Главное меню", callback_data='back_to_menu')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /balance"""
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    balance = token_balance.get_balance(user_id)
    videos_available = balance // TOKENS_PER_VIDEO
    
    await update.message.reply_text(
        f'💰 Ваш баланс\n\n'
        f'👤 {username}\n'
        f'🪙 Токенов: {balance}\n'
        f'🎬 Видео доступно: {videos_available}\n\n'
        f'💵 Стоимость: {TOKENS_PER_VIDEO} токенов/видео'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats - показывает статистику обработки"""
    times = processing_stats.get_times()
    
    if not times:
        await update.message.reply_text(
            '📊 Статистика пока пуста.\n'
            'Отправьте фото для начала!'
        )
        return
    
    avg = sum(times) / len(times)
    recent_avg = get_average_time()
    min_time = min(times)
    max_time = max(times)
    
    stats_text = (
        f"📊 Статистика обработки ({len(times)} видео):\n\n"
        f"⚡ Быстрее всего: {format_time(min_time)}\n"
        f"📈 В среднем: {format_time(avg)}\n"
        f"🐌 Дольше всего: {format_time(max_time)}\n"
        f"🔄 Последние 10: {format_time(recent_avg)}"
    )
    
    await update.message.reply_text(stats_text)

async def addtokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /addtokens - добавление токенов (только админ)"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text('❌ Нет прав')
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            '📝 /addtokens USER_ID AMOUNT\n'
            'Пример: /addtokens 123456 100'
        )
        return
    
    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        new_balance = token_balance.add_tokens(target_id, amount)
        await update.message.reply_text(
            f'✅ Добавлено: {amount}\n'
            f'👤 ID: {target_id}\n'
            f'💰 Баланс: {new_balance}'
        )
    except ValueError:
        await update.message.reply_text('❌ Неверный формат')
    except Exception as e:
        await update.message.reply_text(f'❌ Ошибка: {e}')

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /users - список пользователей (только админ)"""
    user_id = update.effective_user.id
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text('❌ Нет прав')
        return
    
    users = token_balance.get_all_users()
    
    if not users:
        await update.message.reply_text('📋 Нет пользователей')
        return
    
    text = '📋 Пользователи:\n\n'
    for user_data in users[:15]:
        uid, tokens, uname, fname, lname, videos, created, updated = user_data
        
        # Формируем имя
        full_name = ' '.join(filter(None, [fname, lname]))
        display_name = full_name or uname or 'Без имени'
        
        # Форматируем дату создания
        from datetime import datetime
        try:
            created_dt = datetime.fromisoformat(created)
            created_str = created_dt.strftime('%d.%m.%Y')
        except:
            created_str = 'н/д'
        
        text += (
            f'👤 {display_name}\n'
            f'   ID: {uid}\n'
        )
        
        if uname:
            text += f'   @{uname}\n'
        
        text += (
            f'   💰 Токенов: {tokens}\n'
            f'   🎬 Видео: {videos}\n'
            f'   📅 С {created_str}\n\n'
        )
    
    if len(users) > 15:
        text += f'...и еще {len(users) - 15} пользователей'
    
    text += f'\n\n📊 Всего пользователей: {len(users)}'
    
    await update.message.reply_text(text)

async def update_progress(message, start_time, phase="Обработка"):
    """Обновление прогресс-сообщения"""
    elapsed = time.time() - start_time
    avg_time = get_average_time()
    
    # Рассчитываем прогресс (макс 95% до завершения)
    if elapsed < avg_time:
        progress = min(elapsed / avg_time * 0.95, 0.95)
    else:
        progress = 0.95
    
    progress_bar = get_progress_bar(progress)
    
    # Оценка оставшегося времени
    remaining = max(0, avg_time - elapsed)
    
    if remaining < 10:
        estimate = "почти готово"
    elif remaining < 60:
        estimate = f"~{int(remaining)}с"
    else:
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        estimate = f"~{minutes}м {seconds}с"
    
    # Анимированный спиннер
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    frame = spinner[int(elapsed * 2) % len(spinner)]
    
    text = (
        f"{frame} {phase}...\n\n"
        f"📊 [{progress_bar}] {int(progress*100)}%\n"
        f"⏱ Прошло: {format_time(elapsed)}\n"
        f"🎯 Осталось: {estimate}"
    )
    
    await safe_edit_message(message, text)

async def process_comfyui_connect(session, photo_base64, client_id, status_message, start_time,
                                  duration=None, quality=None):
    """
    Отправка запроса в ComfyUI-Connect и получение результата
    
    Args:
        duration: Длительность в секундах (5/10/15) или None для стандарт
        quality: Качество 'low'/'medium'/'high' или None для стандарт
    """
    # Формируем payload
    payload = {
        "image": {
            "image": {
                "type": "file",
                "content": photo_base64,
                "name": f"input_{client_id}.jpg"
            }
        },
        "client_id": client_id
    }
    
    # Добавляем параметры (обязательные по OpenAPI)
    # Если не указаны - используем стандартные значения
    if duration is None:
        duration = 10  # Стандарт
    
    if quality is None:
        quality = 'medium'  # Стандарт
    
    quality_pixels = QUALITIES[quality]['pixels']
    
    payload["duration"] = {"value": duration}
    payload["quality"] = {"value": quality_pixels}
    
    logger.info(f"📏 Длительность: {duration} секунд")
    logger.info(f"📺 Качество: {quality_pixels}px")
    
    logger.info(f"🚀 Отправляю запрос на ComfyUI-Connect: {API_URL}")
    logger.debug(f"Payload keys: {payload.keys()}")
    
    try:
        # ComfyUI-Connect может долго обрабатывать, увеличиваем timeout
        timeout = aiohttp.ClientTimeout(total=600)  # 10 минут
        
        async with session.post(API_URL, json=payload, timeout=timeout) as response:
            # Проверяем статус ответа
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"❌ Ошибка API: HTTP {response.status}")
                logger.error(f"Response: {error_text[:500]}")
                return None, f"Ошибка сервера (HTTP {response.status})"
            
            # ComfyUI-Connect возвращает JSON с результатами
            result = await response.json()
            logger.info(f"✅ Получен ответ от сервера")
            
            # Выводим полный JSON для отладки
            import json as json_lib
            result_str = json_lib.dumps(result, indent=2, ensure_ascii=False)
            # Обрезаем очень длинные base64 строки для читаемости логов
            if len(result_str) > 2000:
                logger.info(f"Full response (truncated): {result_str[:2000]}...")
            else:
                logger.info(f"Full response: {result_str}")
            
            logger.info(f"Response keys: {list(result.keys())}")
            
            # Выводим детали по каждому ключу
            for key, value in result.items():
                if isinstance(value, str):
                    logger.info(f"  {key}: string длина={len(value)} начало={value[:100]}")
                elif isinstance(value, list):
                    logger.info(f"  {key}: list элементов={len(value)}")
                    if len(value) > 0:
                        logger.info(f"    первый элемент: {type(value[0]).__name__}")
                        if isinstance(value[0], str) and len(value[0]) > 50:
                            logger.info(f"    начало: {value[0][:100]}")
                elif isinstance(value, dict):
                    logger.info(f"  {key}: dict ключей={len(value.keys())}, keys={list(value.keys())}")
                else:
                    logger.info(f"  {key}: {type(value).__name__} = {value}")
            
            # Ищем видео в ответе
            # В зависимости от аннотаций в workflow, результат может быть под разными ключами
            # Обычно это что-то вроде "output", "video", "result" и т.д.
            
            video_data = None
            found_key = None
            
            # Функция для проверки является ли данные видео/изображением
            def is_media_data(data):
                """Проверяет магические байты медиа-файлов"""
                if len(data) < 10:
                    return False
                # MP4, MOV, M4V
                if data[:4] in [b'\x00\x00\x00\x18', b'\x00\x00\x00\x1c', b'\x00\x00\x00 ', 
                               b'\x00\x00\x00\x14', b'ftyp']:
                    return True
                # GIF
                if data[:3] == b'GIF':
                    return True
                # JPEG
                if data[:2] == b'\xff\xd8':
                    return True
                # PNG
                if data[:4] == b'\x89PNG':
                    return True
                # WebM
                if data[:4] == b'\x1aE\xdf\xa3':
                    return True
                return False
            
            # Приоритетно проверяем ключ 'output' (из аннотации #output)
            priority_keys = ['output', 'result', 'video', 'image']
            all_keys = priority_keys + [k for k in result.keys() if k not in priority_keys]
            
            for key in all_keys:
                if key not in result:
                    continue
                    
                value = result[key]
                logger.info(f"🔍 Проверяю ключ '{key}' типа {type(value).__name__}")
                
                # Если это строка (base64), пытаемся декодировать
                if isinstance(value, str) and len(value) > 100:
                    try:
                        # Проверяем что это валидный base64
                        decoded = base64.b64decode(value)
                        logger.info(f"  ✓ Декодировано {len(decoded)} байт, первые байты: {decoded[:20].hex()}")
                        
                        # Если данные достаточно большие (больше 10KB), скорее всего это медиа
                        if len(decoded) > 10000:
                            # Проверяем магические байты
                            if is_media_data(decoded):
                                video_data = decoded
                                found_key = key
                                logger.info(f"✅ Найдено видео в ключе '{key}' по magic bytes, размер: {len(decoded)} байт")
                                break
                            else:
                                # Большой файл но неизвестный формат - все равно пробуем
                                logger.warning(f"⚠️ Неизвестные magic bytes, но файл большой ({len(decoded)} байт), пробую использовать")
                                video_data = decoded
                                found_key = key
                                logger.info(f"✅ Используем данные из '{key}', размер: {len(decoded)} байт")
                                break
                    except Exception as e:
                        logger.debug(f"Ключ '{key}' не base64: {e}")
                        continue
                
                # Если это список base64 строк (несколько выходов)
                elif isinstance(value, list) and len(value) > 0:
                    logger.info(f"  📋 Список из {len(value)} элементов")
                    try:
                        first_item = value[0]
                        if isinstance(first_item, str) and len(first_item) > 100:
                            decoded = base64.b64decode(first_item)
                            logger.info(f"  ✓ Декодировано {len(decoded)} байт из массива, первые байты: {decoded[:20].hex()}")
                            
                            if len(decoded) > 10000:
                                if is_media_data(decoded):
                                    video_data = decoded
                                    found_key = f"{key}[0]"
                                    logger.info(f"✅ Найдено видео в массиве '{key}' по magic bytes, размер: {len(decoded)} байт")
                                    break
                                else:
                                    logger.warning(f"⚠️ Неизвестные magic bytes в массиве, но файл большой ({len(decoded)} байт)")
                                    video_data = decoded
                                    found_key = f"{key}[0]"
                                    logger.info(f"✅ Используем данные из массива '{key}', размер: {len(decoded)} байт")
                                    break
                    except Exception as e:
                        logger.debug(f"Массив '{key}' не содержит base64: {e}")
                        continue
                
                # Если это словарь (вложенная структура)
                elif isinstance(value, dict):
                    logger.info(f"  📦 Словарь с ключами: {list(value.keys())}")
                    try:
                        # Ищем внутри словаря ключи типа 'data', 'content', 'file'
                        for subkey in ['data', 'content', 'file', 'video', 'image', 'output']:
                            if subkey in value:
                                subvalue = value[subkey]
                                logger.info(f"    🔍 Проверяю подключ '{subkey}' типа {type(subvalue).__name__}")
                                
                                # Если это список - проверяем первый элемент
                                if isinstance(subvalue, list) and len(subvalue) > 0:
                                    first_item = subvalue[0]
                                    logger.info(f"      📋 Список из {len(subvalue)} элементов")
                                    if isinstance(first_item, str) and len(first_item) > 100:
                                        decoded = base64.b64decode(first_item)
                                        logger.info(f"      ✓ Декодировано {len(decoded)} байт из списка, hex: {decoded[:20].hex()}")
                                        
                                        if len(decoded) > 10000:
                                            if is_media_data(decoded):
                                                video_data = decoded
                                                found_key = f"{key}.{subkey}[0]"
                                                logger.info(f"✅ Найдено видео в '{key}.{subkey}[0]' по magic bytes, размер: {len(decoded)} байт")
                                                break
                                            else:
                                                logger.warning(f"⚠️ Неизвестные magic bytes в '{key}.{subkey}[0]', но файл большой ({len(decoded)} байт)")
                                                video_data = decoded
                                                found_key = f"{key}.{subkey}[0]"
                                                logger.info(f"✅ Используем данные из '{key}.{subkey}[0]', размер: {len(decoded)} байт")
                                                break
                                
                                # Если это строка - проверяем напрямую
                                elif isinstance(subvalue, str) and len(subvalue) > 100:
                                    decoded = base64.b64decode(subvalue)
                                    logger.info(f"    ✓ Декодировано {len(decoded)} байт")
                                    
                                    if len(decoded) > 10000:
                                        if is_media_data(decoded):
                                            video_data = decoded
                                            found_key = f"{key}.{subkey}"
                                            logger.info(f"✅ Найдено видео в '{key}.{subkey}' по magic bytes, размер: {len(decoded)} байт")
                                            break
                                        else:
                                            logger.warning(f"⚠️ Неизвестные magic bytes в '{key}.{subkey}', но файл большой")
                                            video_data = decoded
                                            found_key = f"{key}.{subkey}"
                                            logger.info(f"✅ Используем данные из '{key}.{subkey}', размер: {len(decoded)} байт")
                                            break
                        if video_data:
                            break
                    except Exception as e:
                        logger.debug(f"Dict '{key}' не содержит медиа: {e}")
                        continue
            
            if video_data:
                return video_data, None
            else:
                logger.error(f"❌ Не найдено видео в ответе. Ключи: {list(result.keys())}")
                # Fallback: пробуем через History API (для VHS_VideoCombine)
                logger.info("🔍 Output пустой, пробую через History API...")
                
                # Имя файла который мы отправили (для поиска правильной задачи)
                search_filename = f"input_{client_id}.jpg"
                logger.info(f"🔎 Ищу задачу с файлом: {search_filename}")
                
                # Ждем чтобы задача точно появилась в history
                await asyncio.sleep(5)
                
                for attempt in range(20):  # 20 попыток по 3 секунды
                    try:
                        history_url = "https://cuda.serge.cc/history"
                        async with session.get(history_url) as hist_response:
                            if hist_response.status != 200:
                                await asyncio.sleep(3)
                                continue
                            
                            history = await hist_response.json()
                            logger.debug(f"History: {len(history)} записей")
                            
                            # Ищем нашу задачу по имени файла в workflow
                            for prompt_id, prompt_data in history.items():
                                if not isinstance(prompt_data, dict):
                                    continue
                                
                                # Проверяем workflow (prompt[2])
                                prompt = prompt_data.get('prompt', [])
                                if isinstance(prompt, list) and len(prompt) > 2:
                                    workflow = prompt[2]
                                    
                                    # Ищем search_filename в workflow
                                    import json as json_lib
                                    workflow_str = json_lib.dumps(workflow)
                                    
                                    if search_filename in workflow_str:
                                        logger.info(f"✅ Найдена наша задача: {prompt_id}")
                                        
                                        # Проверяем outputs
                                        outputs = prompt_data.get('outputs', {})
                                        if not outputs:
                                            logger.debug(f"Outputs пока пусты для {prompt_id}, жду...")
                                            continue
                                        
                                        for node_id, node_output in outputs.items():
                                            if not isinstance(node_output, dict):
                                                continue
                                            
                                            for output_key in ['gifs', 'videos']:
                                                videos = node_output.get(output_key, [])
                                                if videos and isinstance(videos, list):
                                                    for video_info in videos:
                                                        if isinstance(video_info, dict):
                                                            filename = video_info.get('filename', '')
                                                            if filename.endswith(('.mp4', '.webm', '.avi', '.mov', '.gif')):
                                                                subfolder = video_info.get('subfolder', '')
                                                                folder_type = video_info.get('type', 'output')
                                                                logger.info(f"✅ Найдено видео: {filename}")
                                                                
                                                                download_url = "https://cuda.serge.cc/view"
                                                                params = {"filename": filename, "type": folder_type, "subfolder": subfolder}
                                                                
                                                                async with session.get(download_url, params=params) as dl_response:
                                                                    if dl_response.status == 200:
                                                                        video_bytes = await dl_response.read()
                                                                        logger.info(f"✅ Скачано {len(video_bytes)} байт")
                                                                        return video_bytes, None
                        
                    except Exception as e:
                        logger.error(f"History error: {e}")
                    
                    await asyncio.sleep(3)
                
                return None, "Видео не найдено в history"
    
    except asyncio.TimeoutError:
        logger.error(f"⏱ Таймаут запроса после 10 минут")
        return None, "Превышено время ожидания (10 мин)"
    
    except Exception as e:
        logger.error(f"❌ Ошибка запроса: {e}", exc_info=True)
        return None, f"Ошибка: {str(e)[:100]}"

# ============================================
# ИНТЕРАКТИВНЫЙ МАСТЕР СОЗДАНИЯ ВИДЕО
# ============================================

async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /create - запуск мастера создания видео"""
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    balance = token_balance.get_balance(user_id)
    
    if balance < 5:
        await update.message.reply_text(
            "❌ Недостаточно токенов!\n\n"
            f"💰 Ваш баланс: {balance}\n"
            "💵 Минимум для создания: 5 токенов\n\n"
            "Обратитесь к администратору"
        )
        return
    
    context.user_data['create_session'] = {
        'started_at': time.time(),
        'step': 1,
        'user_id': user_id,
        'username': username
    }
    
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='cancel')]]
    
    await update.message.reply_text(
        "🎬 Мастер создания видео\n\n"
        "Я помогу создать видео из вашей фотографии!\n\n"
        "📸 Шаг 1 из 3: Загрузка фото\n\n"
        "Отправьте любую фотографию.\n"
        "Лучше всего работает с:\n\n"
        "✅ Портретами людей\n"
        "✅ Чёткими изображениями\n"
        "✅ Хорошим освещением\n\n"
        f"💰 Ваш баланс: {balance} токенов",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def photo_received_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получено фото в мастере"""
    if 'create_session' not in context.user_data:
        # Если пользователь пришел через меню, создаем сессию
        context.user_data['create_session'] = {
            'user_id': update.effective_user.id,
            'username': update.effective_user.username or update.effective_user.first_name
        }
    
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    
    photo_data = BytesIO()
    await file.download_to_memory(photo_data)
    photo_data.seek(0)
    photo_base64 = base64.b64encode(photo_data.read()).decode()
    
    session = context.user_data['create_session']
    session.update({
        'photo_base64': photo_base64,
        'photo_size': file.file_size,
        'photo_width': photo.width,
        'photo_height': photo.height,
        'step': 2
    })
    
    keyboard = []
    for dur_key in ['5', '10', '15']:
        dur = DURATIONS[dur_key]
        text = f"{dur['emoji']} {dur['seconds']} сек - {dur['cost']}💰"
        if dur.get('recommended'):
            text += " ⭐"
        keyboard.append([InlineKeyboardButton(text, callback_data=f'duration_{dur_key}')])
    
    keyboard.append([
        InlineKeyboardButton("⏮ Другое фото", callback_data='back_photo'),
        InlineKeyboardButton("❌ Отмена", callback_data='cancel')
    ])
    
    await update.message.reply_text(
        f"✅ Фото получено!\n\n"
        f"📏 {photo.width}×{photo.height} px\n"
        f"📦 {format_size_kb(file.file_size)}\n\n"
        f"⏱ Шаг 2 из 3: Длительность\n\n"
        f"Выберите длительность видео:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def duration_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрана длительность"""
    query = update.callback_query
    await query.answer()
    
    if 'create_session' not in context.user_data:
        await query.edit_message_text("❌ Сессия не найдена. Начните заново с /start")
        return
    
    duration = query.data.split('_')[1]
    session = context.user_data['create_session']
    session.update({
        'duration': int(duration),
        'step': 3
    })
    
    keyboard = []
    for qual_key in ['low', 'medium', 'high']:
        qual = QUALITIES[qual_key]
        cost_mod = qual['cost_modifier']
        current_cost = DURATIONS[duration]['cost'] + cost_mod
        
        text = f"{qual['emoji']} {qual['name']} ({qual['pixels']}px)"
        if cost_mod > 0:
            text += f" - +{cost_mod}💰"
        else:
            text += " - бесплатно"
        
        if qual.get('recommended'):
            text += " ⭐"
        
        text += f"\nИтого: {current_cost}💰"
        
        keyboard.append([InlineKeyboardButton(text, callback_data=f'quality_{qual_key}')])
    
    keyboard.append([
        InlineKeyboardButton("⏮ Назад", callback_data='back_duration'),
        InlineKeyboardButton("❌ Отмена", callback_data='cancel')
    ])
    
    await query.edit_message_text(
        f"✅ Длительность: {duration} секунд\n\n"
        f"📺 Шаг 3 из 3: Качество\n\n"
        f"Выберите качество видео:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def quality_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбрано качество - показываем подтверждение"""
    query = update.callback_query
    await query.answer()
    
    if 'create_session' not in context.user_data:
        await query.edit_message_text("❌ Сессия не найдена. Начните заново с /start")
        return
    
    quality = query.data.split('_')[1]
    session = context.user_data['create_session']
    session.update({
        'quality': quality,
        'step': 4
    })
    
    duration = session['duration']
    cost = calculate_cost(duration, quality)
    balance = token_balance.get_balance(session['user_id'])
    
    if balance < cost:
        await query.answer("❌ Недостаточно токенов!", show_alert=True)
        await query.edit_message_text(
            f"❌ Недостаточно токенов!\n\n"
            f"💰 Ваш баланс: {balance}\n"
            f"💵 Требуется: {cost}\n\n"
            f"Выберите другие параметры или обратитесь к администратору"
        )
        return
    
    duration_info = DURATIONS[str(duration)]
    quality_info = QUALITIES[quality]
    
    text = f"""📋 Подтверждение создания

📸 **Фото:** {session['photo_width']}×{session['photo_height']} px
⏱ **Длительность:** {duration} секунд
📺 **Качество:** {quality_info['pixels']}px

💰 **Стоимость:**
• Базовая: {duration_info['cost']} токенов
• Качество: +{quality_info['cost_modifier']} токенов
• **Итого:** {cost} токенов

💳 **Баланс:** {balance}
💵 **Останется:** {balance - cost}

⏱ **Примерное время:** ~{get_estimated_time(duration, quality)}

Всё правильно?
"""
    
    keyboard = [
        [InlineKeyboardButton("✅ СОЗДАТЬ ВИДЕО", callback_data='confirm_create')],
        [],
        [
            InlineKeyboardButton("⏱ Время", callback_data='edit_duration'),
            InlineKeyboardButton("📺 Качество", callback_data='edit_quality')
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data='cancel')]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def confirm_create_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждено - начинаем обработку"""
    query = update.callback_query
    await query.answer("🚀 Начинаю создание!")
    
    session = context.user_data['create_session']
    user_id = session['user_id']
    
    await query.edit_message_text(
        f"🚀 Создаю видео!\n\n"
        f"⏱ Длительность: {session['duration']} секунд\n"
        f"📺 Качество: {QUALITIES[session['quality']]['pixels']}px\n\n"
        f"Ожидайте..."
    )
    
    # Запускаем обработку с параметрами
    start_time = time.time()
    client_id = f"telegram_{user_id}_{int(start_time * 1000)}"
    
    status_message = query.message
    
    try:
        async with aiohttp.ClientSession() as http_session:
            progress_task = None
            
            async def progress_updater():
                await asyncio.sleep(2)
                while True:
                    await update_progress(status_message, start_time, "Создаю видео")
                    await asyncio.sleep(2)
            
            try:
                progress_task = asyncio.create_task(progress_updater())
                
                # Передаём параметры в process_comfyui_connect
                video_data, error = await process_comfyui_connect(
                    http_session, session['photo_base64'], client_id,
                    status_message, start_time,
                    duration=session['duration'],
                    quality=session['quality']
                )
                
                if progress_task:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass
                
                if error or not video_data:
                    elapsed = time.time() - start_time
                    await safe_edit_message(
                        status_message,
                        f"❌ {error or 'Не удалось получить видео'}\n"
                        f"⏱ Время: {format_time(elapsed)}"
                    )
                else:
                    total_time = time.time() - start_time
                    processing_stats.add_time(total_time, session['duration'], session['quality'])
                    
                    token_balance.spend_tokens(user_id, calculate_cost(session['duration'], session['quality']))
                    token_balance.increment_videos(user_id)
                    new_balance = token_balance.get_balance(user_id)
                    
                    await safe_edit_message(
                        status_message,
                        f"✅ Готово за {format_time(total_time)}!\n"
                        f"📤 Отправляю видео..."
                    )
                    
                    video_buffer = BytesIO(video_data)
                    video_buffer.name = 'video.mp4'
                    
                    await update.effective_chat.send_video(
                        video=video_buffer,
                        caption=(
                            f"🎬 Видео готово!\n"
                            f"⏱ {format_time(total_time)}\n\n"
                            f"💸 Списано: {calculate_cost(session['duration'], session['quality'])} токенов\n"
                            f"💰 Остаток: {new_balance}\n\n"
                            f"🤖 Создано ботом: @{update.get_bot().username}"
                        ),
                        reply_markup=create_generate_more_menu()
                    )
                    
                    await status_message.delete()
                    
            except asyncio.CancelledError:
                if progress_task:
                    progress_task.cancel()
                raise
                
    except Exception as e:
        logger.error(f"Ошибка в мастере: {e}", exc_info=True)
        await safe_edit_message(
            status_message,
            f"❌ Произошла ошибка\n{str(e)[:100]}"
        )
    finally:
        context.user_data.pop('create_session', None)
    
    return

async def back_to_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к загрузке фото"""
    query = update.callback_query
    await query.answer()
    
    session = context.user_data.get('create_session', {})
    session['step'] = 1
    
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data='cancel')]]
    
    await query.edit_message_text(
        "📸 Шаг 1 из 3: Фото\n\n"
        "Отправьте новую фотографию",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def back_to_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору длительности"""
    query = update.callback_query
    await query.answer()
    
    session = context.user_data['create_session']
    session['step'] = 2
    current_duration = str(session.get('duration', '10'))
    
    keyboard = []
    for dur_key in ['5', '10', '15']:
        dur = DURATIONS[dur_key]
        text = f"{dur['emoji']} {dur['seconds']} сек - {dur['cost']}💰"
        if dur_key == current_duration:
            text += " ✅"
        elif dur.get('recommended'):
            text += " ⭐"
        keyboard.append([InlineKeyboardButton(text, callback_data=f'duration_{dur_key}')])
    
    keyboard.append([
        InlineKeyboardButton("⏮ К фото", callback_data='back_photo'),
        InlineKeyboardButton("❌ Отмена", callback_data='cancel')
    ])
    
    await query.edit_message_text(
        "⏱ Шаг 2 из 3: Длительность\n\n"
        "Выберите длительность видео:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def edit_duration_from_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование длительности с экрана подтверждения"""
    return await back_to_duration(update, context)

async def edit_quality_from_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование качества с экрана подтверждения"""
    query = update.callback_query
    await query.answer()
    
    session = context.user_data['create_session']
    duration = str(session['duration'])
    current_quality = session.get('quality', 'medium')
    
    keyboard = []
    for qual_key in ['low', 'medium', 'high']:
        qual = QUALITIES[qual_key]
        cost_mod = qual['cost_modifier']
        current_cost = DURATIONS[duration]['cost'] + cost_mod
        
        text = f"{qual['emoji']} {qual['name']} ({qual['pixels']}px)"
        if cost_mod > 0:
            text += f" +{cost_mod}💰"
        
        if qual_key == current_quality:
            text += " ✅"
        elif qual.get('recommended'):
            text += " ⭐"
        
        text += f"\nИтого: {current_cost}💰"
        
        keyboard.append([InlineKeyboardButton(text, callback_data=f'quality_{qual_key}')])
    
    keyboard.append([
        InlineKeyboardButton("⏮ Назад", callback_data='back_quality'),
        InlineKeyboardButton("❌ Отмена", callback_data='cancel')
    ])
    
    await query.edit_message_text(
        "📺 Шаг 3 из 3: Качество\n\n"
        "Выберите качество видео:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Состояние сохраняется в user_data

async def back_to_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к подтверждению (после редактирования)"""
    return await quality_selected(update, context)

async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена мастера"""
    query = update.callback_query
    await query.answer("❌ Отменено")
    
    context.user_data.pop('create_session', None)
    
    await query.edit_message_text(
        "❌ Создание видео отменено.\n\n"
        "Для нового запроса используйте /start или отправьте фото",
        reply_markup=create_main_menu()
    )
    
    return

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /cancel"""
    context.user_data.pop('create_session', None)
    
    await update.message.reply_text(
        "❌ Текущая операция отменена.\n\n"
        "Для создания видео используйте /start или отправьте фото",
        reply_markup=create_main_menu()
    )
    
    return

async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Таймаут сессии"""
    await update.message.reply_text(
        "⏱ Время сессии истекло (5 минут)\n\n"
        "Начните заново с /start или отправьте фото",
        reply_markup=create_main_menu()
    )
    
    context.user_data.pop('create_session', None)
    return

# ============================================
# ОБРАБОТКА ФОТО (ПРОСТОЙ РЕЖИМ)
# ============================================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фотографии от пользователя"""
    start_time = time.time()
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Обновляем информацию о пользователе и проверяем баланс
    balance = token_balance.get_balance(user_id)
    token_balance.add_tokens(user_id, 0, user.username, user.first_name, user.last_name)
    
    # Проверяем режим работы
    waiting_mode = context.user_data.get('waiting_for_photo')
    
    if waiting_mode == 'wizard':
        # Запускаем мастер создания видео
        context.user_data.pop('waiting_for_photo', None)
        # Передаем управление мастеру
        await photo_received_wizard(update, context)
        return
    elif waiting_mode == 'quick':
        # Быстрый режим - продолжаем обычную обработку
        context.user_data.pop('waiting_for_photo', None)
    
    # Проверяем баланс для быстрого режима
    default_cost = calculate_cost(10, 'medium')
    if balance < default_cost:
        await update.message.reply_text(
            f'❌ Недостаточно токенов!\n\n'
            f'💰 Баланс: {balance}\n'
            f'💵 Требуется: {default_cost}\n\n'
            f'Обратитесь к администратору'
        )
        return
    
    client_id = f"telegram_{user_id}_{int(start_time * 1000)}"
    display_name = user.first_name or user.username or str(user_id)
    logger.info(f"📸 Запрос от {user_id} ({display_name}), баланс: {balance}")
    
    # Начальное сообщение
    status_message = await update.message.reply_text("🔄 Получаю изображение...")
    
    try:
        # Скачиваем фото из Telegram
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        photo_data = BytesIO()
        await file.download_to_memory(photo_data)
        photo_data.seek(0)
        
        # Конвертируем в base64
        photo_base64 = base64.b64encode(photo_data.read()).decode('utf-8')
        logger.info(f"📦 Изображение готово ({len(photo_base64)} символов)")
        
        await safe_edit_message(status_message, "📤 Отправляю на сервер...")
        
        # Создаем асинхронную сессию
        async with aiohttp.ClientSession() as session:
            # Запускаем обновление прогресса
            progress_task = None
            
            async def progress_updater():
                await asyncio.sleep(2)  # Небольшая задержка перед первым обновлением
                while True:
                    await update_progress(status_message, start_time, "Создаю видео")
                    await asyncio.sleep(2)
            
            try:
                # Запускаем прогресс в фоне
                progress_task = asyncio.create_task(progress_updater())
                
                # Отправляем запрос в ComfyUI-Connect (это может занять несколько минут)
                # Используем значения по умолчанию для обычной отправки фото
                video_data, error = await process_comfyui_connect(
                    session, photo_base64, client_id, status_message, start_time,
                    duration=10,  # По умолчанию 10 секунд
                    quality='medium'  # По умолчанию среднее качество
                )
                
                # Останавливаем обновление прогресса
                if progress_task:
                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass
                
                # Проверяем результат
                if error or not video_data:
                    elapsed = time.time() - start_time
                    await safe_edit_message(
                        status_message,
                        f"❌ {error or 'Не удалось получить видео'}\n"
                        f"⏱ Время: {format_time(elapsed)}\n\n"
                        f"Попробуйте еще раз или обратитесь к администратору."
                    )
                    return
                
                # Успех! Отправляем видео
                total_time = time.time() - start_time
                processing_stats.add_time(total_time, 10, 'medium')  # Настройки по умолчанию
                
                await safe_edit_message(
                    status_message,
                    f"✅ Готово за {format_time(total_time)}!\n"
                    f"📤 Отправляю видео..."
                )
                
                # Списываем токены и увеличиваем счетчик видео (настройки по умолчанию)
                default_cost = calculate_cost(10, 'medium')
                token_balance.spend_tokens(user_id, default_cost)
                token_balance.increment_videos(user_id)
                new_balance = token_balance.get_balance(user_id)
                
                # Отправляем видео пользователю
                video_buffer = BytesIO(video_data)
                video_buffer.name = 'video.mp4'
                
                await update.message.reply_video(
                    video=video_buffer,
                    caption=(
                        f"🎬 Видео готово!\n"
                        f"⏱ {format_time(total_time)}\n\n"
                        f"💸 Списано: {default_cost} токенов\n"
                        f"💰 Остаток: {new_balance}\n\n"
                        f"🤖 Создано ботом: @{update.get_bot().username}"
                    ),
                    reply_markup=create_generate_more_menu()
                )
                
                # Удаляем статус-сообщение
                await status_message.delete()
                logger.info(f"✅ Успешно завершено за {format_time(total_time)}")
                
            except asyncio.CancelledError:
                if progress_task:
                    progress_task.cancel()
                raise
                
    except Exception as e:
        logger.error(f"❌ Ошибка обработки: {e}", exc_info=True)
        elapsed = time.time() - start_time
        await safe_edit_message(
            status_message,
            f"❌ Произошла ошибка\n"
            f"⏱ Время: {format_time(elapsed)}\n\n"
            f"Попробуйте еще раз."
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений"""
    await update.message.reply_text(
        '📸 Пожалуйста, отправьте фото!\n'
        'Я работаю только с изображениями.'
    )

def main():
    """Запуск бота"""
    if not BOT_TOKEN:
        print("❌ Ошибка: BOT_TOKEN не найден!")
        print("Создайте файл .env и добавьте:")
        print("BOT_TOKEN=your_telegram_bot_token_here")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Обработчики для мастера создания видео (без ConversationHandler)
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))  # Главная команда
    application.add_handler(CommandHandler("addtokens", addtokens_command))  # Админская
    application.add_handler(CommandHandler("users", users_command))  # Админская
    
    # Обработчик кнопок главного меню
    application.add_handler(CallbackQueryHandler(handle_menu_callback, pattern='^(create_video|quick_mode|balance|stats|help|back_to_menu|create_more|quick_more)$'))
    
    # Обработчики для мастера создания видео
    application.add_handler(CallbackQueryHandler(duration_selected, pattern='^duration_'))
    application.add_handler(CallbackQueryHandler(quality_selected, pattern='^quality_'))
    application.add_handler(CallbackQueryHandler(confirm_create_wizard, pattern='^confirm_create'))
    application.add_handler(CallbackQueryHandler(edit_duration_from_confirm, pattern='^edit_duration'))
    application.add_handler(CallbackQueryHandler(edit_quality_from_confirm, pattern='^edit_quality'))
    application.add_handler(CallbackQueryHandler(back_to_photo, pattern='^back_photo'))
    application.add_handler(CallbackQueryHandler(back_to_duration, pattern='^back_duration'))
    application.add_handler(CallbackQueryHandler(back_to_confirmation, pattern='^back_quality'))
    application.add_handler(CallbackQueryHandler(cancel_wizard, pattern='^cancel'))
    
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))  # Быстрый режим
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("🚀 Бот запущен!")
    logger.info(f"📡 ComfyUI-Connect API: {API_URL}")
    print("🚀 Бот запущен и готов к работе!")
    print(f"📡 API: {API_URL}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
