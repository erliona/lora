import os
import logging
import base64
import asyncio
import time
import json
import websockets
from io import BytesIO
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, RetryAfter
import aiohttp
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
API_URL = 'https://cuda.serge.cc/api/connect/workflows/api-video'
WS_URL = 'wss://cuda.serge.cc/ws'

# Статистика времени обработки с детализацией по фазам
class ProcessingStats:
    def __init__(self):
        self.completion_times = deque(maxlen=50)
        self.phase_timings = {
            'server_request': deque(maxlen=50),
            'queue_wait': deque(maxlen=50),
            'video_creation': deque(maxlen=50),
            'download': deque(maxlen=50)
        }
        self.load_stats()
    
    def add_completion_time(self, duration):
        self.completion_times.append(duration)
        self.save_stats()
        logger.info(f"Добавлено время: {duration}с, всего записей: {len(self.completion_times)}")
    
    def add_phase_timing(self, phase, duration):
        if phase in self.phase_timings:
            self.phase_timings[phase].append(duration)
            logger.info(f"Фаза '{phase}': {duration}с")
            self.save_stats()
    
    def get_phase_estimate(self, phase):
        if phase not in self.phase_timings:
            return 5
        
        timings = list(self.phase_timings[phase])
        if not timings:
            defaults = {
                'server_request': 3,
                'queue_wait': 10, 
                'video_creation': 100,
                'download': 2
            }
            return defaults.get(phase, 5)
        
        return sum(timings) / len(timings)
    
    def get_estimate(self, elapsed_time, current_phase=None):
        if not self.completion_times:
            if elapsed_time < 30:
                return "~30-90с (нет данных)"
            elif elapsed_time < 60:
                return "~60-120с (нет данных)"
            else:
                return "почти готово (нет данных)"
        
        server_avg = self.get_phase_estimate('server_request')
        queue_avg = self.get_phase_estimate('queue_wait')
        creation_avg = self.get_phase_estimate('video_creation')
        download_avg = self.get_phase_estimate('download')
        
        total_avg = server_avg + queue_avg + creation_avg + download_avg
        
        if current_phase == "Отправляю на сервер":
            remaining = total_avg - elapsed_time
        elif current_phase == "В очереди на обработку":
            remaining = queue_avg + creation_avg + download_avg - max(0, elapsed_time - server_avg)
        elif current_phase == "Создаю видео":
            remaining = creation_avg + download_avg - max(0, elapsed_time - server_avg - queue_avg)
        elif current_phase == "Скачиваю готовое видео":
            remaining = download_avg - max(0, elapsed_time - total_avg + download_avg)
        else:
            remaining = total_avg - elapsed_time
        
        remaining = max(0, remaining)
        
        if remaining < 10:
            return "почти готово"
        elif remaining < 30:
            return f"~{int(remaining)}с"
        elif remaining < 120:
            return f"~{int(remaining//60)}м {int(remaining%60)}с"
        else:
            return f"~{int(remaining//60)}м"
    
    def get_progress_ratio(self, elapsed_time, current_phase, queue_position=0):
        server_avg = self.get_phase_estimate('server_request')
        queue_avg = self.get_phase_estimate('queue_wait')
        creation_avg = self.get_phase_estimate('video_creation')
        download_avg = self.get_phase_estimate('download')
        
        total_avg = server_avg + queue_avg + creation_avg + download_avg
        
        if current_phase == "Отправляю на сервер":
            phase_progress = min(elapsed_time / server_avg, 1.0)
            return phase_progress * 0.05
            
        elif current_phase == "В очереди на обработку":
            if queue_position > 0:
                return 0.05 + min(elapsed_time / (queue_avg * (queue_position + 1)), 0.10)
            else:
                phase_elapsed = max(0, elapsed_time - server_avg)
                phase_progress = min(phase_elapsed / queue_avg, 1.0)
                return 0.05 + (phase_progress * 0.10)
            
        elif current_phase == "Создаю видео":
            base_time = server_avg + queue_avg
            if elapsed_time <= base_time:
                return 0.15
            phase_elapsed = elapsed_time - base_time
            phase_progress = min(phase_elapsed / creation_avg, 1.0)
            return 0.15 + (phase_progress * 0.75)
            
        elif current_phase == "Скачиваю готовое видео":
            base_time = server_avg + queue_avg + creation_avg
            if elapsed_time <= base_time:
                return 0.90
            phase_elapsed = elapsed_time - base_time
            phase_progress = min(phase_elapsed / download_avg, 1.0)
            return 0.90 + (phase_progress * 0.08)
        
        return min(elapsed_time / total_avg, 0.98)
    
    def get_stats_summary(self):
        if not self.completion_times:
            return "Нет данных о времени обработки"
        
        times = list(self.completion_times)
        avg_time = sum(times) / len(times)
        min_time = min(times)
        max_time = max(times)
        
        phase_stats = []
        for phase_name, phase_key in [
            ("Отправка", "server_request"),
            ("Очередь", "queue_wait"), 
            ("Создание", "video_creation"),
            ("Скачивание", "download")
        ]:
            if self.phase_timings[phase_key]:
                avg_phase = sum(self.phase_timings[phase_key]) / len(self.phase_timings[phase_key])
                if avg_phase >= 0.1:
                    phase_stats.append(f"{phase_name}: {format_time(avg_phase)}")
                else:
                    phase_stats.append(f"{phase_name}: < 1с")
        
        base_stats = (
            f"📊 Статистика ({len(times)} заданий):\n"
            f"⚡ Быстрее всего: {format_time(min_time)}\n"
            f"📈 Среднее время: {format_time(avg_time)}\n"
            f"🐌 Дольше всего: {format_time(max_time)}"
        )
        
        if phase_stats:
            return base_stats + f"\n\n🔄 По фазам:\n" + "\n".join(phase_stats)
        
        return base_stats
    
    def save_stats(self):
        try:
            data = {
                'completion_times': list(self.completion_times),
                'phase_timings': {k: list(v) for k, v in self.phase_timings.items()}
            }
            with open('processing_stats.json', 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"Ошибка сохранения статистики: {e}")
    
    def load_stats(self):
        try:
            if os.path.exists('processing_stats.json'):
                with open('processing_stats.json', 'r') as f:
                    data = json.load(f)
                    
                if isinstance(data, list):
                    self.completion_times.extend(data)
                    logger.info(f"Загружено {len(data)} записей статистики (старый формат)")
                else:
                    self.completion_times.extend(data.get('completion_times', []))
                    for phase, timings in data.get('phase_timings', {}).items():
                        if phase in self.phase_timings:
                            self.phase_timings[phase].extend(timings)
                    
                    total_records = len(self.completion_times)
                    phase_records = sum(len(v) for v in self.phase_timings.values())
                    logger.info(f"Загружено {total_records} общих записей, {phase_records} записей по фазам")
        except Exception as e:
            logger.error(f"Ошибка загрузки статистики: {e}")

stats = ProcessingStats()

# WebSocket трекер фаз
class WebSocketPhaseTracker:
    def __init__(self, client_id, start_time):
        self.client_id = client_id
        self.start_time = start_time
        self.current_phase = None
        self.phase_start_time = start_time
        self.phase_timings = {}
        self.queue_position = 0
        self.is_executing = False
        self.is_completed = False
    
    def switch_phase(self, new_phase):
        current_time = time.time()
        
        if self.current_phase:
            phase_duration = current_time - self.phase_start_time
            self.phase_timings[self.current_phase] = phase_duration
            
            phase_key_map = {
                "Отправляю на сервер": "server_request",
                "В очереди на обработку": "queue_wait",
                "Создаю видео": "video_creation", 
                "Скачиваю готовое видео": "download"
            }
            
            if self.current_phase in phase_key_map:
                stats.add_phase_timing(phase_key_map[self.current_phase], phase_duration)
                logger.info(f"{self.client_id}: завершил фазу '{self.current_phase}' за {format_time(phase_duration)}")
        
        self.current_phase = new_phase
        self.phase_start_time = current_time
        logger.info(f"{self.client_id}: переход к фазе '{new_phase}'")
    
    def update_queue_position(self, queue_remaining):
        self.queue_position = queue_remaining
        if queue_remaining == 0:
            if self.current_phase != "Создаю видео":
                self.switch_phase("Создаю видео")
    
    def set_executing(self):
        self.is_executing = True
        if self.current_phase != "Создаю видео":
            self.switch_phase("Создаю видео")
    
    def set_completed(self):
        self.is_completed = True
        if self.current_phase != "Скачиваю готовое видео":
            self.switch_phase("Скачиваю готовое видео")
    
    def set_downloading(self):
        """Переключение на фазу скачивания"""
        if self.current_phase != "Скачиваю готовое видео":
            self.switch_phase("Скачиваю готовое видео")
    
    def get_elapsed_time(self):
        """Получить время с начала обработки"""
        return time.time() - self.start_time
    
    def finish(self):
        if self.current_phase:
            self.switch_phase(None)
        
        total_time = time.time() - self.start_time
        stats.add_completion_time(total_time)
        
        logger.info(f"{self.client_id}: завершено за {format_time(total_time)}")
        for phase, duration in self.phase_timings.items():
            logger.info(f"  {phase}: {format_time(duration)}")

# Глобальное хранилище трекеров
phase_trackers = {}

def format_time(seconds):
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

async def safe_edit_message(message, text, max_retries=3):
    for attempt in range(max_retries):
        try:
            await message.edit_text(text)
            return True
        except RetryAfter as e:
            logger.warning(f"Rate limit hit, waiting {e.retry_after} seconds")
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return True
            elif "Message can't be edited" in str(e):
                logger.warning("Сообщение слишком старое для редактирования")
                return False
            else:
                logger.error(f"BadRequest при редактировании: {e}")
                return False
        except Exception as e:
            logger.error(f"Ошибка редактирования сообщения (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            else:
                return False
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats_summary = stats.get_stats_summary()
    await update.message.reply_text(
        'Привет! Отправь мне фото, и я создам из него видео.\n'
        'Просто отправь любое изображение!\n\n'
        '⚡ Поддерживаю одновременную обработку\n'
        '⏱ Реальный прогресс через WebSocket\n'
        f'🤖 Используй /stats для подробной статистики\n\n'
        f'{stats_summary}'
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats_summary = stats.get_stats_summary()
    
    if stats.completion_times:
        times = list(stats.completion_times)
        recent_avg = sum(times[-10:]) / min(10, len(times))
        
        additional_info = (
            f"\n\n💡 Последние 10 заданий: {format_time(recent_avg)} в среднем\n"
            f"📈 Тренд: {'⬆️ растет' if recent_avg > sum(times)/len(times) else '⬇️ снижается'}"
        )
    else:
        additional_info = "\n\n🔄 Пока нет данных. Отправьте фото для начала сбора статистики!"
    
    await update.message.reply_text(stats_summary + additional_info)

# WebSocket и остальные функции
async def update_progress_message(status_message, client_id, start_time):
    try:
        tracker = phase_trackers.get(client_id)
        if not tracker:
            return
        
        elapsed = time.time() - start_time
        elapsed_str = format_time(elapsed)
        current_phase = tracker.current_phase or "Обработка"
        
        loading_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        frame = loading_frames[int(elapsed * 2) % len(loading_frames)]
        
        estimate = stats.get_estimate(elapsed, current_phase)
        progress_ratio = stats.get_progress_ratio(elapsed, current_phase, tracker.queue_position)
        progress_ratio = min(progress_ratio, 0.98)
        
        filled_bars = int(progress_ratio * 18)
        progress = "▓" * filled_bars + "░" * (18 - filled_bars)
        
        queue_info = ""
        if current_phase == "Создаю видео":
            queue_info = " (выполняется)"
        elif current_phase == "Скачиваю готовое видео":
            queue_info = " (скачиваю)"
        elif current_phase == "В очереди на обработку" and tracker.queue_position > 0:
            queue_info = f" (в очереди: {tracker.queue_position})"
        elif tracker.is_executing:
            queue_info = " (выполняется)"
        
        new_message = (
            f'{frame} {current_phase}{queue_info}...\n'
            f'⏱ Прошло: {elapsed_str}\n'
            f'📊 [{progress}]\n'
            f'🎯 Оценка: {estimate}\n'
            f'🆔 {client_id[-8:]}'
        )
        
        success = await safe_edit_message(status_message, new_message)
        if not success:
            logger.warning(f"Не удалось обновить прогресс для {client_id}")
        
    except Exception as e:
        logger.error(f"Ошибка обновления прогресса: {e}")

async def progress_updater_task(status_message, client_id, start_time, stop_event=None):
    try:
        while not (stop_event and stop_event.is_set()):
            await update_progress_message(status_message, client_id, start_time)
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        logger.info(f"Progress updater для {client_id} отменен")
        if client_id in phase_trackers:
            phase_trackers[client_id].finish()
            del phase_trackers[client_id]
    except Exception as e:
        logger.error(f"Ошибка в progress_updater_task: {e}")

async def websocket_monitor_task(client_id, tracker, stop_event=None):
    """
    WebSocket мониторинг только для отслеживания очереди.
    НЕ используется для определения завершения задачи.
    """
    try:
        ws_url = f"{WS_URL}?clientId={client_id}"
        async with websockets.connect(ws_url) as websocket:
            logger.info(f"WebSocket подключен для {client_id}")
            await websocket.recv()  # Читаем начальное сообщение
            
            while not (stop_event and stop_event.is_set()):
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    data = json.loads(message)
                    msg_type = data.get('type')
                    
                    # Обрабатываем ТОЛЬКО события очереди
                    if msg_type == 'status':
                        status_data = data.get('data', {}).get('status', {})
                        exec_info = status_data.get('exec_info', {})
                        queue_remaining = exec_info.get('queue_remaining', 0)
                        
                        old_pos = tracker.queue_position
                        tracker.update_queue_position(queue_remaining)
                        
                        # Логируем только значительные изменения
                        if old_pos != queue_remaining:
                            logger.info(f"{client_id}: позиция в очереди {queue_remaining}")
                        
                        # Переключаем фазу когда очередь обнуляется
                        if queue_remaining == 0 and not tracker.is_executing:
                            tracker.set_executing()
                            logger.info(f"{client_id}: начало выполнения")
                        
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Ошибка обработки WebSocket для {client_id}: {e}")
                    break
    
    except Exception as e:
        logger.error(f"Ошибка подключения WebSocket для {client_id}: {e}")


async def poll_for_completion(session, client_id, tracker, stop_event=None, poll_interval=3):
    """
    Периодически проверяет history API на наличие готового результата.
    Возвращает (filename, subfolder, folder_type) при успехе или None.
    """
    logger.info(f"Начинаем опрос готовности для {client_id}")
    
    while not (stop_event and stop_event.is_set()):
        try:
            await asyncio.sleep(poll_interval)
            
            # Проверяем history
            history_url = "https://cuda.serge.cc/history"
            async with session.get(history_url) as response:
                if response.status != 200:
                    logger.warning(f"{client_id}: history API вернул {response.status}")
                    continue
                
                history = await response.json()
                
                # Ищем задачу по client_id
                for prompt_id, prompt_data in history.items():
                    if not isinstance(prompt_data, dict) or 'prompt' not in prompt_data:
                        continue
                    
                    prompt_info = prompt_data['prompt']
                    
                    # Проверяем что prompt_info это словарь
                    if not isinstance(prompt_info, dict):
                        continue
                    
                    # Проверяем client_id в prompt
                    found_client_id = None
                    for node_id, node_data in prompt_info.items():
                        if isinstance(node_data, dict):
                            inputs = node_data.get('inputs', {})
                            if inputs.get('client_id') == client_id:
                                found_client_id = client_id
                                break
                    
                    if not found_client_id:
                        continue
                    
                    # Нашли нашу задачу! Проверяем outputs
                    outputs = prompt_data.get('outputs', {})
                    for node_id, node_output in outputs.items():
                        if not isinstance(node_output, dict):
                            continue
                        
                        videos = node_output.get('gifs', []) or node_output.get('videos', [])
                        
                        for video_info in videos:
                            if isinstance(video_info, dict):
                                filename = video_info.get('filename', '')
                                
                                # Проверяем что это видео-файл
                                if any(filename.endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.gif']):
                                    subfolder = video_info.get('subfolder', '')
                                    folder_type = video_info.get('type', 'output')
                                    
                                    logger.info(f"{client_id}: найден результат {filename}")
                                    tracker.set_completed()
                                    
                                    return (filename, subfolder, folder_type)
        
        except asyncio.CancelledError:
            logger.info(f"{client_id}: опрос отменен")
            raise
        except Exception as e:
            logger.error(f"{client_id}: ошибка опроса: {e}")
            await asyncio.sleep(poll_interval)
    
    return None

async def download_video_file(session, filename, subfolder="", folder_type="output"):
    try:
        download_url = f"https://cuda.serge.cc/view"
        params = {
            "filename": filename,
            "type": folder_type,
            "subfolder": subfolder
        }
        
        logger.info(f"Скачиваю файл {filename}")
        
        async with session.get(download_url, params=params) as response:
            if response.status == 200:
                content = await response.read()
                logger.info(f"Файл скачан, размер: {len(content)} байт")
                return content
            else:
                logger.error(f"Ошибка скачивания: HTTP {response.status}")
                return None
    except Exception as e:
        logger.error(f"Ошибка скачивания файла: {e}")
        return None

async def process_video_result(update, status_message, video_data, client_id, total_time):
    try:
        await safe_edit_message(
            status_message,
            f'✅ Видео готово за {format_time(total_time)}!\n'
            f'📤 Отправляю...\n'
            f'🆔 {client_id[-8:]}'
        )
        
        if isinstance(video_data, bytes):
            video_buffer = BytesIO(video_data)
            video_buffer.name = 'video.mp4'
            
            await update.message.reply_video(
                video=video_buffer,
                caption=(
                    f'🎬 Ваше видео готово!\n'
                    f'⏱ Время: {format_time(total_time)}\n'
                    f'🆔 {client_id[-8:]}'
                )
            )
            await status_message.delete()
            
        elif isinstance(video_data, str):
            try:
                if video_data.startswith('http'):
                    await update.message.reply_text(
                        f'🎬 Ваше видео: {video_data}\n'
                        f'⏱ Время: {format_time(total_time)}\n'
                        f'🆔 {client_id[-8:]}'
                    )
                else:
                    video_bytes = base64.b64decode(video_data)
                    video_buffer = BytesIO(video_bytes)
                    video_buffer.name = 'video.mp4'
                    
                    await update.message.reply_video(
                        video=video_buffer,
                        caption=(
                            f'🎬 Ваше видео готово!\n'
                            f'⏱ Время: {format_time(total_time)}\n'
                            f'🆔 {client_id[-8:]}'
                        )
                    )
                
                await status_message.delete()
                
            except Exception as e:
                logger.error(f"Ошибка декодирования для {client_id}: {e}")
                await safe_edit_message(
                    status_message,
                    f'❌ Ошибка обработки видео за {format_time(total_time)}\n'
                    f'🆔 {client_id[-8:]}'
                )
        
        else:
            await safe_edit_message(
                status_message,
                f'❌ Неожиданный тип данных за {format_time(total_time)}\n'
                f'🆔 {client_id[-8:]}'
            )
            
    except Exception as e:
        logger.error(f"Ошибка обработки результата для {client_id}: {e}")
        await safe_edit_message(
            status_message,
            f'❌ Ошибка обработки за {format_time(total_time)}\n'
            f'🆔 {client_id[-8:]}'
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Гибридный подход: WebSocket для очереди + polling для завершения
    """
    start_time = time.time()
    client_id = f"telegram_{update.effective_user.id}_{int(start_time * 1000)}"
    
    logger.info(f"Новый запрос от пользователя {update.effective_user.id}, client_id: {client_id}")
    
    status_message = await update.message.reply_text("🔄 Обрабатываю...")
    
    # Создаем tracker
    tracker = WebSocketPhaseTracker(client_id, start_time)
    phase_trackers[client_id] = tracker
    
    # Tasks
    progress_task = None
    ws_task = None
    poll_task = None
    stop_event = asyncio.Event()
    
    try:
        # Скачиваем фото
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        photo_data = BytesIO()
        await file.download_to_memory(photo_data)
        photo_data.seek(0)
        
        photo_base64 = base64.b64encode(photo_data.read()).decode('utf-8')
        
        # Отправляем запрос
        tracker.switch_phase("Отправляю на сервер")
        
        async with aiohttp.ClientSession() as session:
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
            
            logger.info(f"{client_id}: отправляю запрос на сервер")
            
            async with session.post(API_URL, json=payload) as response:
                if response.status != 200:
                    elapsed = time.time() - start_time
                    await safe_edit_message(
                        status_message,
                        f"❌ Ошибка сервера: HTTP {response.status}\n"
                        f"⏱ Время: {format_time(elapsed)}\n"
                        f"🆔 {client_id[-8:]}"
                    )
                    return
                
                result = await response.json()
                logger.info(f"{client_id}: получен ответ {result.get('status')}")
            
            # Запускаем фоновые задачи
            tracker.switch_phase("В очереди на обработку")
            
            progress_task = asyncio.create_task(
                progress_updater_task(status_message, client_id, start_time, stop_event)
            )
            
            ws_task = asyncio.create_task(
                websocket_monitor_task(client_id, tracker, stop_event)
            )
            
            poll_task = asyncio.create_task(
                poll_for_completion(session, client_id, tracker, stop_event)
            )
            
            # Ждем готовности (до 5 минут)
            try:
                result = await asyncio.wait_for(poll_task, timeout=300)
                
                if result:
                    filename, subfolder, folder_type = result
                    
                    # Скачиваем видео
                    tracker.set_downloading()
                    logger.info(f"{client_id}: скачиваю видео {filename}")
                    
                    video_bytes = await download_video_file(
                        session, filename, subfolder, folder_type
                    )
                    
                    if video_bytes:
                        total_time = time.time() - start_time
                        
                        # Отправляем пользователю
                        await process_video_result(
                            update, status_message, video_bytes, 
                            client_id, total_time
                        )
                        logger.info(f"{client_id}: видео успешно отправлено за {format_time(total_time)}")
                    else:
                        elapsed = time.time() - start_time
                        await safe_edit_message(
                            status_message,
                            f"❌ Не удалось скачать видео\n"
                            f"⏱ Время: {format_time(elapsed)}\n"
                            f"🆔 {client_id[-8:]}"
                        )
                else:
                    elapsed = time.time() - start_time
                    await safe_edit_message(
                        status_message,
                        f"⏱ Таймаут обработки\n"
                        f"⏱ Время: {format_time(elapsed)}\n"
                        f"🆔 {client_id[-8:]}"
                    )
            
            except asyncio.TimeoutError:
                elapsed = time.time() - start_time
                await safe_edit_message(
                    status_message,
                    f"⏱ Таймаут после {format_time(elapsed)}\n"
                    f"🆔 {client_id[-8:]}"
                )
    
    except Exception as e:
        logger.error(f"Ошибка обработки {client_id}: {e}")
        elapsed = time.time() - start_time
        await safe_edit_message(
            status_message,
            f"❌ Ошибка: {str(e)[:100]}\n"
            f"⏱ Время: {format_time(elapsed)}\n"
            f"🆔 {client_id[-8:]}"
        )
    
    finally:
        # Cleanup
        if client_id in phase_trackers:
            phase_trackers[client_id].finish()
            del phase_trackers[client_id]
        
        # Останавливаем задачи
        stop_event.set()
        
        for task in [progress_task, ws_task, poll_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Пожалуйста, отправьте фото, а не текст. Я умею работать только с изображениями!'
    )

def main():
    if not BOT_TOKEN:
        print("Ошибка: BOT_TOKEN не найден в переменных окружения!")
        print("Создайте файл .env и добавьте туда:")
        print("BOT_TOKEN=your_telegram_bot_token_here")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("🚀 WebSocket бот запущен с реальным отслеживанием прогресса!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
