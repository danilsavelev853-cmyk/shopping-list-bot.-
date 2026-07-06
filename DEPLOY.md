# Деплой Shopping List Bot — пошагово

## 1. Токен бота
1. Открой @BotFather в Telegram
2. `/newbot` → задай имя (например `Shopping List Bot`) → задай username, обязательно с окончанием `bot` (например `MyShoppingListBot`)
3. BotFather пришлёт токен вида `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` — сохрани, это `BOT_TOKEN`

## 2. Anthropic API ключ
1. Зайди на console.anthropic.com
2. Settings → API Keys → Create Key
3. Скопируй ключ (показывается один раз) — это `ANTHROPIC_API_KEY`
4. Убедись, что на аккаунте есть баланс (Billing → Add credits), иначе запросы будут падать с ошибкой 401/403

## 3. (Опционально) OpenAI ключ для голосовых
1. platform.openai.com → API Keys → Create new secret key
2. Это `OPENAI_API_KEY`. Без него всё работает, кроме распознавания войсов — бот попросит написать текстом

## 4. Репозиторий на GitHub
1. github.com → New repository → назови, например `shopping-list-bot` → Create
2. Загрузи туда 3 файла из архива: `bot.py`, `requirements.txt`, `README.md`
   - Через веб: Add file → Upload files → перетащи файлы → Commit changes
   - Или через git локально:
     ```
     git init
     git add .
     git commit -m "init"
     git remote add origin https://github.com/ТВОЙ_ЮЗЕРНЕЙМ/shopping-list-bot.git
     git push -u origin main
     ```

## 5. Render — создание Background Worker
1. render.com → залогинься (можно через GitHub)
2. New → **Background Worker** (важно: не Web Service — этот бот работает через polling, ему не нужен открытый порт)
3. Connect a repository → выбери `shopping-list-bot`
4. Настройки:
   - **Name**: любое, например `shopping-list-bot`
   - **Region**: любой ближайший
   - **Branch**: `main`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
   - **Instance Type**: Free (для старта хватит)

## 6. Переменные окружения
В том же экране создания (или потом в Environment → Add Environment Variable) добавь:
| Key | Value |
|---|---|
| `BOT_TOKEN` | токен из шага 1 |
| `ANTHROPIC_API_KEY` | ключ из шага 2 |
| `OPENAI_API_KEY` | ключ из шага 3, если делаешь голосовые |

## 7. Деплой
1. Create Background Worker → Render сам соберёт и запустит
2. Логи смотри во вкладке Logs — там будет видно, что aiogram стартовал и слушает апдейты (никаких ошибок импорта/токена)
3. Если билд упал — почти всегда это опечатка в переменных или несовпадение версий в requirements.txt

## 8. Проверка
1. Открой бота в Telegram по его username
2. `/start` — должно прийти меню с кнопками
3. Проверь по очереди: «По блюду», «Вручную», «Список», «Напоминание»

## 9. Частые проблемы
- **Бот не отвечает** — смотри Logs на Render, скорее всего неверный `BOT_TOKEN` или воркер не запустился
- **Ошибка от Anthropic** — проверь баланс и правильность `ANTHROPIC_API_KEY`
- **Голосовые не работают** — нормально, если `OPENAI_API_KEY` не задан; бот сам об этом скажет
- **После передеплоя список пуст** — так и задумано: SQLite-файл лежит на эфемерном диске Render. Если это мешает, скажи — подключим Render Disk (постоянное хранилище) или перенесём базу на Postgres (у Render есть бесплатный тариф)

## 10. Что можно докрутить дальше
- Постоянное хранилище (Render Disk / Postgres)
- Общий список на семью/пару (сейчас список привязан к user_id, у каждого свой)
- Кнопка «удалить один пункт» вместо полной очистки
- Категории продуктов (овощи/молочка/бытовая химия) для удобства в магазине
