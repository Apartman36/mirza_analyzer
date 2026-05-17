# mirza-analyzer

Локальный фундамент для надежной загрузки нескольких частичных экспортов Telegram Desktop в одну каноническую SQLite-базу.

Проект сейчас делает только первый слой:

- аудитирует Telegram `result.json` и файлы рядом с ними;
- объединяет варианты одного сообщения по Telegram message id;
- нормализует текст Telegram Desktop;
- сохраняет исходные варианты сообщений и реальные медиафайлы;
- считает SHA-256 для найденных файлов;
- пишет воспроизводимую локальную SQLite-базу.

Проект пока не делает LLM-извлечение, OCR, RAG, чатбот, дашборд, продуктовые рекомендации или отчеты по ремонту. Эти этапы должны идти только после проверки качества базы.

## Директории

Сырые данные:

```powershell
C:\sarychev\mirzabaeva
```

Код и результаты:

```powershell
C:\sarychev\mirza-analyzer
```

Сырые данные считаются read-only. Команды этого проекта читают файлы из `C:\sarychev\mirzabaeva`, но не удаляют, не переименовывают, не перемещают и не изменяют их.

## Установка

С `uv`:

```powershell
uv sync --extra dev
```

Без `uv`:

```powershell
python -m pip install -e ".[dev]"
```

## Команды

Аудит экспортов:

```powershell
python -m mirza_analyzer audit --data-root "C:\sarychev\mirzabaeva" --out outputs/audit.md
```

Создание SQLite-базы:

```powershell
python -m mirza_analyzer ingest --data-root "C:\sarychev\mirzabaeva" --db outputs/mirza.sqlite
```

Статистика базы:

```powershell
python -m mirza_analyzer stats --db outputs/mirza.sqlite
```

Выборка для ручной проверки:

```powershell
python -m mirza_analyzer sample --db outputs/mirza.sqlite --out outputs/sample_posts.md --limit 50
```

Ожидаемые результаты:

```text
outputs/
  audit.md
  mirza.sqlite
  sample_posts.md
```

## Как проверить SQLite из Python

```python
import sqlite3

conn = sqlite3.connect("outputs/mirza.sqlite")
conn.row_factory = sqlite3.Row

row = conn.execute("select count(*) as n from canonical_messages").fetchone()
print(row["n"])

for post in conn.execute("""
    select telegram_message_id, date, substr(text_plain, 1, 120) as preview
    from canonical_messages
    where text_plain <> ''
    order by telegram_message_id
    limit 5
"""):
    print(dict(post))
```

## Следующий этап

После ручной проверки `audit.md`, `stats` и `sample_posts.md` можно добавлять отдельный слой тематической разметки: полы, цвета стен, кухни, фасады, стулья, столы, диваны, прихожие и мебель для гостиной. Этот репозиторий намеренно отделяет надежную загрузку данных от будущей аналитики.

