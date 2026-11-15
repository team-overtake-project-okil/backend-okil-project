
# Звірка з point_types

Джерело: Neon, проєкт `okil`, база `okil_new_db`, гілка `production`,
перевірено 2026-11-06. Таблицю та дані БД не змінювали.

У `config.py` підтримуються всі 25 кодів із таблиці. `bank_atm` названо
«Банки та банкомати», оскільки в ньому є обидва різновиди об’єктів.
Припущені коди `atm`, `entertainment`, `swimming_pool` прибрано.

Для пояснення потрібен тільки `radius_of_influence_1`: це радіус
максимального дистанційного бала і підрахунку кількості. Він читається
безпосередньо з БД; запасних радіусів 200/300 м у SQL більше немає.
Другий і третій пороги залишаються частиною наявного розрахунку балів
і не використовуються генератором.

| Категорія | Код типу | Радіус максимального бала, м |
| --- | --- | ---: |
| daily | bank_atm | 300 |
| daily | grocery | 150 |
| daily | pharmacy | 200 |
| daily | post_office | 250 |
| daily | supermarket | 300 |
| enjoyment | cinema | 600 |
| enjoyment | mall | 600 |
| enjoyment | museum | 600 |
| enjoyment | playground | 150 |
| food | bakery | 200 |
| food | cafe | 450 |
| food | coffee_shop | 200 |
| food | fast_food | 500 |
| food | pub_bar | 450 |
| food | restaurant | 850 |
| health | dentistry | 400 |
| health | hospital | 800 |
| health | polyclinic | 400 |
| mobility | gtfs_stop | 200 |
| security | shelter | 100 |
| sport | gym | 400 |
| sport | sport_ground | 200 |
| sport | stadium | 800 |
| study | kindergarten | 300 |
| study | school | 400 |

Обидва `examples/category_test_cases.json` і `examples/category_scored_cases.json`
оновлені фактичними даними для H3 `891e7688377ffff`. Команда запуску
не змінилася. Це знімок прикладів, а не поточне читання БД при запуску CLI.
Файл `tests/fixtures/category_test_cases.json` зберігає початковий JSON
користувача для перевірки сумісності формату; він не є каталогом БД.

`tests/fixtures/point_types.json` містить повний знімок трьох порогів.
`tests/fixtures/hexagon_accessibility.json` — фактичні кількості й відстані.
Ці файли потрібні тільки для регресійних тестів, API не читає з них радіуси.

Перевірка відповідності каталогу:

```powershell
python -m pytest tests/test_point_type_catalog.py -q
```
