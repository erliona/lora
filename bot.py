import os
import logging
import base64
import asyncio
import time
import aiohttp
import sqlite3
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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

# Статистика обработки
processing_times = []

# Система балансов
class TokenBalance:
    def __init__(self, db_path='balances.db'):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
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
        conn.commit()
        conn.close()
        logger.info("💾 База данных балансов инициализирована")
    
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

def get_average_time():
    """Получить среднее время обработки (последние 10 запросов)"""
    if not processing_times:
        return 120  # По умолчанию 2 минуты
    recent = processing_times[-10:]
    return sum(recent) / len(recent)

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    user = update.effective_user
    username = user.username
    first_name = user.first_name
    last_name = user.last_name
    
    # Обновляем информацию о пользователе
    balance = token_balance.get_balance(user_id)
    token_balance.add_tokens(user_id, 0, username, first_name, last_name)
    
    avg_time = get_average_time()
    stats_text = ""
    if processing_times:
        stats_text = f"\n📊 Среднее время: {format_time(avg_time)}"
    
    await update.message.reply_text(
        f'👋 Привет, {username}!\n\n'
        f'📸 Отправьте фото - я создам видео!{stats_text}\n\n'
        f'💰 Баланс: {balance} токенов\n'
        f'💵 Стоимость: {TOKENS_PER_VIDEO} токенов/видео\n\n'
        f'📋 /balance - баланс\n'
        f'📊 /stats - статистика'
    )

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
    if not processing_times:
        await update.message.reply_text(
            '📊 Статистика пока пуста.\n'
            'Отправьте фото для начала!'
        )
        return
    
    avg = sum(processing_times) / len(processing_times)
    recent_avg = get_average_time()
    min_time = min(processing_times)
    max_time = max(processing_times)
    
    stats_text = (
        f"📊 Статистика обработки ({len(processing_times)} видео):\n\n"
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

async def process_comfyui_connect(session, photo_base64, client_id, status_message, start_time):
    """
    Отправка запроса в ComfyUI-Connect и получение результата
    
    ComfyUI-Connect возвращает результат сразу в ответе в виде:
    {
        "output_name": "base64_encoded_data..."
    }
    """
    # Формируем payload согласно документации ComfyUI-Connect
    # Для загрузки изображения используем формат:
    # "node-name": { "image": { "type": "file", "content": "base64", "name": "filename" } }
    
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

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фотографии от пользователя"""
    start_time = time.time()
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Обновляем информацию о пользователе и проверяем баланс
    balance = token_balance.get_balance(user_id)
    token_balance.add_tokens(user_id, 0, user.username, user.first_name, user.last_name)
    
    if balance < TOKENS_PER_VIDEO:
        await update.message.reply_text(
            f'❌ Недостаточно токенов!\n\n'
            f'💰 Баланс: {balance}\n'
            f'💵 Требуется: {TOKENS_PER_VIDEO}\n\n'
            f'Обратитесь к @{(await update.get_bot()).username} администратору'
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
                video_data, error = await process_comfyui_connect(
                    session, photo_base64, client_id, status_message, start_time
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
                processing_times.append(total_time)
                
                # Сохраняем последние 50 результатов
                if len(processing_times) > 50:
                    processing_times.pop(0)
                
                await safe_edit_message(
                    status_message,
                    f"✅ Готово за {format_time(total_time)}!\n"
                    f"📤 Отправляю видео..."
                )
                
                # Списываем токены и увеличиваем счетчик видео
                token_balance.spend_tokens(user_id, TOKENS_PER_VIDEO)
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
                        f"💸 Списано: {TOKENS_PER_VIDEO} токенов\n"
                        f"💰 Остаток: {new_balance}"
                    )
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
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("addtokens", addtokens_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("🚀 Бот запущен!")
    logger.info(f"📡 ComfyUI-Connect API: {API_URL}")
    print("🚀 Бот запущен и готов к работе!")
    print(f"📡 API: {API_URL}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
