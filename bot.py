import os
import logging
import base64
import asyncio
import time
import aiohttp
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, RetryAfter
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
# ComfyUI-Connect endpoint для workflow 'api-video'
API_URL = 'https://cuda.serge.cc/api/connect/workflows/api-video'

# Статистика обработки
processing_times = []

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
    avg_time = get_average_time()
    stats_text = ""
    if processing_times:
        stats_text = f"\n\n📊 Среднее время: {format_time(avg_time)}"
    
    await update.message.reply_text(
        '👋 Привет! Я создаю видео из фотографий.\n\n'
        '📸 Просто отправьте любое изображение,\n'
        'и я преобразую его в видео!\n'
        f'{stats_text}\n\n'
        '💡 Команда /stats покажет статистику'
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

async def process_comfyui_connect(session, photo_base64, status_message, start_time):
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
                "name": f"input_{int(time.time())}.jpg"
            }
        }
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
            logger.debug(f"Response keys: {result.keys()}")
            
            # Ищем видео в ответе
            # В зависимости от аннотаций в workflow, результат может быть под разными ключами
            # Обычно это что-то вроде "output", "video", "result" и т.д.
            
            video_data = None
            found_key = None
            
            # Проверяем различные возможные ключи
            for key in result.keys():
                value = result[key]
                
                # Если это строка (base64), пытаемся декодировать
                if isinstance(value, str) and len(value) > 100:
                    try:
                        # Проверяем что это валидный base64
                        decoded = base64.b64decode(value)
                        
                        # Проверяем что это видео/изображение (начинается с магических байтов)
                        if decoded[:4] in [b'\x00\x00\x00\x18', b'\x00\x00\x00\x1c', b'\x00\x00\x00 '] or \
                           decoded[:3] == b'GIF' or decoded[:2] == b'\xff\xd8':
                            video_data = decoded
                            found_key = key
                            logger.info(f"✅ Найдено видео в ключе '{key}', размер: {len(decoded)} байт")
                            break
                    except Exception as e:
                        logger.debug(f"Ключ '{key}' не содержит валидный base64: {e}")
                        continue
                
                # Если это список base64 строк (несколько выходов)
                elif isinstance(value, list) and len(value) > 0:
                    try:
                        first_item = value[0]
                        if isinstance(first_item, str):
                            decoded = base64.b64decode(first_item)
                            if decoded[:4] in [b'\x00\x00\x00\x18', b'\x00\x00\x00\x1c', b'\x00\x00\x00 '] or \
                               decoded[:3] == b'GIF' or decoded[:2] == b'\xff\xd8':
                                video_data = decoded
                                found_key = f"{key}[0]"
                                logger.info(f"✅ Найдено видео в массиве '{key}', размер: {len(decoded)} байт")
                                break
                    except Exception as e:
                        logger.debug(f"Массив '{key}' не содержит валидный base64: {e}")
                        continue
            
            if video_data:
                return video_data, None
            else:
                logger.error(f"❌ Не найдено видео в ответе. Ключи: {list(result.keys())}")
                return None, "Сервер не вернул видео"
    
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
    
    logger.info(f"📸 Новый запрос от пользователя {user_id}")
    
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
                    session, photo_base64, status_message, start_time
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
                
                # Отправляем видео пользователю
                video_buffer = BytesIO(video_data)
                video_buffer.name = 'video.mp4'
                
                await update.message.reply_video(
                    video=video_buffer,
                    caption=f"🎬 Ваше видео готово!\n⏱ Обработано за {format_time(total_time)}"
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
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    logger.info("🚀 Бот запущен!")
    logger.info(f"📡 ComfyUI-Connect API: {API_URL}")
    print("🚀 Бот запущен и готов к работе!")
    print(f"📡 API: {API_URL}")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
