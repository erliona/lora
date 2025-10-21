# 🎬 Telegram Video Bot

Telegram-бот для создания видео из фотографий с использованием ComfyUI-Connect API.

## 📋 Описание

Бот принимает фотографию от пользователя, отправляет её на сервер обработки [ComfyUI-Connect](https://github.com/Good-Dream-Studio/ComfyUI-Connect) и возвращает готовое видео.

### Особенности:
- ⚡ Простая и надёжная логика работы
- 📊 Умный прогресс-бар с оценкой времени
- 🎯 Адаптивные оценки на основе истории
- 📈 Сбор статистики времени обработки
- 🔄 Graceful обработка ошибок

## 🚀 Установка

### 1. Клонируйте репозиторий
```bash
git clone <repo_url>
cd lora
```

### 2. Установите зависимости
```bash
pip install -r requirements.txt
```

### 3. Настройте переменные окружения
```bash
# Скопируйте пример конфигурации
cp env.example .env

# Отредактируйте .env и добавьте ваш токен бота
nano .env
```

Получите токен у [@BotFather](https://t.me/BotFather):
1. Напишите `/newbot`
2. Следуйте инструкциям
3. Скопируйте полученный токен в `.env`

### 4. Запустите бота

#### Локально (для разработки):
```bash
python bot.py
```

#### На сервере (production):
```bash
# Скопируйте systemd service
sudo cp lora-bot.service /etc/systemd/system/

# Отредактируйте пути в service файле под ваш сервер
sudo nano /etc/systemd/system/lora-bot.service

# Запустите как сервис
sudo systemctl daemon-reload
sudo systemctl enable lora-bot
sudo systemctl start lora-bot

# Проверьте статус
sudo systemctl status lora-bot

# Смотрите логи
journalctl -u lora-bot -f
```

## 📱 Использование

1. Запустите бота командой `/start`
2. Отправьте любую фотографию
3. Дождитесь обработки (обычно 1-2 минуты)
4. Получите готовое видео!

### Команды:
- `/start` - Приветствие и краткая статистика
- `/stats` - Подробная статистика обработки

## 🛠 Технологии

- **python-telegram-bot** - Telegram Bot API
- **aiohttp** - Асинхронные HTTP запросы
- **python-dotenv** - Управление переменными окружения
- **ComfyUI-Connect** - REST API обёртка для ComfyUI

## 🔧 ComfyUI-Connect API

Бот использует [ComfyUI-Connect](https://github.com/Good-Dream-Studio/ComfyUI-Connect) для взаимодействия с ComfyUI через REST API.

### Гибридный подход (Connect API + History API):

Бот использует комбинацию двух методов получения результата:

1. **Попытка 1 - Connect API Response (быстро):**
   - Отправляет POST запрос с фото
   - Если workflow возвращает result.output - использует его
   - Работает для нод типа Preview Image, Save Image

2. **Fallback - History API (для VHS_VideoCombine):**
   - Если result.output пустой (для видео-нод VHS)
   - Опрашивает /history каждые 3 сек (до 60 сек)
   - Ищет задачу по уникальному имени файла `input_{client_id}.jpg`
   - Скачивает готовое видео через /view endpoint
   - **Безопасно**: каждый пользователь получает только своё видео!

### Формат запроса:
```json
{
  "image": {
    "image": {
      "type": "file",
      "content": "base64_encoded_image...",
      "name": "input_telegram_USER_TIMESTAMP.jpg"
    }
  },
  "client_id": "telegram_USER_TIMESTAMP"
}
```

### Формат ответа:
```json
{
  "output_name": "base64_encoded_video..."
}
```

## 📊 Архитектура

### Основные компоненты:

**`handle_photo()`** - Главная функция обработки:
1. Скачивает фото из Telegram
2. Конвертирует в base64
3. Отправляет на ComfyUI-Connect
4. Показывает прогресс
5. Получает и отправляет видео

**`process_comfyui_connect()`** - Работа с API:
- Отправка POST запроса с payload
- Ожидание ответа (до 10 минут)
- Автоматический поиск видео в ответе
- Поддержка разных форматов ответов

**`update_progress()`** - Отображение прогресса:
- Анимированный спиннер
- Прогресс-бар
- Оценка оставшегося времени
- На основе истории обработки

### Статистика:
- Сохраняет последние 50 результатов
- Рассчитывает среднее время
- Показывает min/max/avg значения

## 🔧 Конфигурация

### API Endpoint
```python
API_URL = 'https://cuda.serge.cc/api/connect/workflows/api-video'
```

Где:
- `https://cuda.serge.cc` - хост ComfyUI-Connect
- `/api/connect/workflows/` - базовый путь
- `api-video` - название workflow

### Таймауты
- **Запрос**: 600 секунд (10 минут)
- **Обновление прогресса**: каждые 2 секунды

## 📝 Логирование

Бот логирует все важные события:
- 📸 Новые запросы
- 🚀 Отправка на сервер
- ✅ Успешная обработка
- ❌ Ошибки и таймауты

Уровень логирования: `INFO`

## ⚠️ Требования

- Python 3.8+
- Telegram Bot Token
- Доступ к ComfyUI-Connect серверу

## 🐛 Устранение неполадок

### Бот не отвечает
- Проверьте токен в `.env`
- Убедитесь что бот запущен

### Ошибка "HTTP 500"
- Проверьте доступность сервера
- Проверьте формат payload

### Таймаут обработки
- Увеличьте `timeout` в коде
- Проверьте нагрузку на сервер

## 📄 История изменений

### v2.0 (Текущая)
- ✅ Полная переработка под ComfyUI-Connect API
- ✅ Упрощённая архитектура (394 строки вместо 802)
- ✅ Улучшенная обработка ошибок
- ✅ Автоматический поиск видео в ответе

### v1.0
- История с WebSocket и polling
- Сложная логика с фазами
- 800+ строк кода

## 📄 Лицензия

MIT

---

Сделано с ❤️ для работы с ComfyUI-Connect
