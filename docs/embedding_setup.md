# Cloudflare BGE-M3 та перехід бази знань на 1024 виміри

1. У [Cloudflare Workers AI](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)
   відкрий **Use REST API**, створи Workers AI API Token і скопіюй Account ID.
   Для ручного токена Cloudflare вказує дозволи Workers AI Read і Edit.
2. Додай на бекенді в локальний .env і Render Environment:

```dotenv
CLOUDFLARE_ACCOUNT_ID=your_account_id
CLOUDFLARE_API_TOKEN=your_workers_ai_token
EMBEDDING_API_TIMEOUT_SECONDS=10
CHAT_PRELOAD_AI=false
```

   Токен зберігай у секретах бекенду. GROQ_API_KEY потрібен для відповідей.
   Нові embeddings використовують модель @cf/baai/bge-m3 через
   [Workers AI REST API](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/).
3. З папки бекенду встанови залежності та перевір реальний API:

```powershell
python -m pip install -r requirements.txt
python -m app.check_embedding_api
```

   Очікуються два рядки "BGE-M3 API OK" із dimensions=1024 та norm=1.000000.
   Команда потребує DB_URL у налаштуваннях, але не відкриває БД, Redis чи Groq.

## Перевірка застосунку

```powershell
python -m pip install -r tests/requirements-chat.txt
python -m pytest tests/test_embedding_api.py tests/test_intent_classifier.py tests/test_ai_knowledge_import.py tests/test_ai_knowledge_markdown.py tests/test_ai_knowledge_model.py tests/test_ai_assistant.py tests/test_chat_service.py tests/test_chat_startup.py tests/test_chat_websocket.py -q
python -m app.try_intent_classifier
```

Для інтерактивної перевірки використовуй доступну тестову БД і Groq API.
Прогони [приклади для кожного підтримуваного інтенту](intent_classification.md),
змішане питання та сторонній запит, наприклад «Дай рецепт сирників».
Перевір мітку, оцінки схожості, зміст відповіді та питання з H3-контекстом.
Поріг чату лишився 0.2 і потребує перевірки на реальних українських запитах.
У браузері перевір ті ж приклади через /ws/chat із доступним Redis.
З CHAT_PRELOAD_AI=false перша відповідь включає час створення еталонів.

## Міграція існуючої ai_knowledge

1. Створи [тестову гілку Neon з даними](https://neon.com/docs/introduction/branching)
   від робочої БД. Встанови DB_URL для **прямого підключення** до цієї гілки
   з правами зміни таблиці. У хоста для міграції не має бути суфікса -pooler.
   Змінна середовища DB_URL має пріоритет над .env. Перевір ціль у Neon перед запуском.
2. Запусти на тестовій гілці:

```powershell
python -m app.migrate_ai_knowledge --apply
python -m app.migrate_ai_knowledge --verify
```

   Скрипт читає тексти кожного наявного рядка, генерує всі нові вектори
   перед блокуванням і перевіряє, що джерело за цей час не змінилося.
   Потім в одній транзакції оновлює вектори, метадані, тип колонки та індекс
   HNSW із vector_cosine_ops. Ідентифікатори, тексти, категорії, дати
   та інші метадані залишаються в тих самих рядках. Якщо API, блокування,
   зміна схеми або перевірка не пройдуть, транзакція не застосовується.
   Скрипт підтримує старі vector(384) і vector(1536), а для вже мігрованої
   таблиці повторний запуск лише перевіряє результат.
3. Перед production зроби резервну копію або збережи гілку Neon зі станом
   до міграції. Перевір кількість рядків до та після на тестовій гілці
   й перевір чат. Заплануй коротке вікно обслуговування, бо зміна колонки
   та індексу блокує доступ до ai_knowledge. Повтори --apply і --verify
   з прямим production DB_URL, потім розгорни новий бекенд у Render.
   Для застосунку можеш залишити штатне pooled-підключення.
4. Для відкату використовуй резервний стан **таблиці** разом зі старою
   версією бекенду. Вектори BGE-M3 не можна перетворити назад на вектори
   MiniLM обрізанням. Відновлення всієї робочої БД може відкотити сторонні записи.

Для нової таблиці створення схеми бекендом одразу створює vector(1024).
За потреби наповни її з Markdown через python -m app.import_ai_knowledge --apply.
Для старої таблиці create_all не змінює розмірність. Міграція переобчислює
наявні документи, а імпортер окремо оновлює чи додає записи з Markdown.
