# Shopping List Bot

## Переменные окружения (Render → Environment)
- `BOT_TOKEN` — токен от @BotFather
- `ANTHROPIC_API_KEY` — ключ Anthropic (для генерации ингредиентов по блюду)
- `OPENAI_API_KEY` — опционально, для распознавания голосовых (Whisper). Без него голосовые не работают, бот попросит текст.

## Деплой на Render (та же схема, что Mousse Crumb)
1. Залей эту папку в отдельный репозиторий на GitHub.
2. Render → New → Background Worker (не Web Service — это бот на polling, без HTTP).
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Добавь переменные окружения выше.
6. UptimeRobot тут не нужен — Background Worker не засыпает как Web Service.

## Локальный запуск
```
pip install -r requirements.txt
export BOT_TOKEN=...
export ANTHROPIC_API_KEY=...
python bot.py
```

## Хранилище
SQLite-файл `shopping.db` создаётся автоматически рядом с bot.py. На Render Background Worker диск эфемерный — при передеплое список обнулится. Если нужна постоянность между деплоями, можно подключить Render Disk или переехать на Postgres — скажи, докручу.
