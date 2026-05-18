# Карта выходных файлов проекта

Проект `mirza-analyzer` накопил много папок в `outputs/`. Этот документ объясняет,
что из них является источником истины, что — отчётом, а что — экспериментом,
который можно регенерировать или удалить (вручную, никогда автоматически).

> Все папки и файлы в `outputs/` игнорируются git (см. `.gitignore`).
> Репозиторий хранит только код, тесты, документацию и `.gitkeep`-маркеры.
> Любая папка ниже может отсутствовать на свежем клоне — её надо пересобрать
> соответствующей командой.

## Категории выходов

| Категория | Что это | Можно ли удалять | Регенерируется |
|-----------|---------|-------------------|----------------|
| Источник истины | Канонические данные, на которых строится всё остальное | Только если планируется немедленная регенерация | `ingest`, `extract-facts` |
| Финальный отчёт | То, что показываем отцу | Только если есть свежая регенерация | `father-report`, `kitchen-palette-report` |
| Промежуточный артефакт | Сводки, CSV, JSONL, использованные для отладки | Да, безопасно | Любая команда соответствующего этапа |
| Экспериментальный прогон | Старые LLM-ревью с разными моделями и сэмплами | Да, безопасно | `llm-review` с нужными флагами |
| Ручные проверки | Скрипты в `db_checks/` | Не удалять (это код, не выход) | — |

## Источники истины

Эти файлы не следует удалять без плана пересборки. Их пересборка занимает время
и требует доступа к сырым экспортам Telegram (`C:\sarychev\mirzabaeva`).

| Файл | Создаётся | Зачем |
|------|-----------|-------|
| `outputs/mirza.sqlite` | `python -m mirza_analyzer ingest --data-root C:\sarychev\mirzabaeva --db outputs/mirza.sqlite` | Канонический объединённый экспорт всех частичных дампов Telegram Desktop. Все остальные этапы читают эту базу. |
| `outputs/extracted/extracted_facts.sqlite` | `python -m mirza_analyzer extract-facts --db outputs/mirza.sqlite --out-dir outputs/extracted --source project_articles` | Детерминированные структурированные факты Stage 2/2.1. Из неё строятся Stage 3 и Stage 4. |

Без этих двух SQLite-файлов ни один отчёт пересобрать нельзя.

## Финальные отчёты (для отца)

Эти файлы — то, что реально отправляется отцу или хранится как «чистая» версия.

| Файл | Этап | Что это |
|------|------|---------|
| `outputs/father_report/father_report_short.md` | Stage 3.1 | Короткий (2–4 страницы) curated-отчёт по общим категориям. Главный документ для отца. |
| `outputs/father_report/father_report.md` | Stage 3 | Полный детерминированный отчёт со всеми категориями. Используется как референс. |
| `outputs/kitchen_palette_report/kitchen_palette_short_clean.md` | Stage 4.1 | Очищенный короткий отчёт по кухонным палитрам. Главный документ по кухне для отца. |
| `outputs/kitchen_palette_report/kitchen_palette_report.md` | Stage 4 | Полный отчёт со всеми кандидатами и качественными примечаниями. |
| `outputs/kitchen_palette_report/kitchen_examples_selected_clean.csv` | Stage 4.1 | Чистая таблица отобранных кухонных примеров — для ручной сверки и Excel. |
| `outputs/kitchen_palette_report/contact_sheets/` | Stage 4 | Контактные листы (несколько фото на одном изображении) по выбранным проектам. |
| `outputs/kitchen_palette_report/images_by_example/` | Stage 4 | Папки с фото по каждому отобранному кухонному примеру. |

> Бэкап рекомендации: если очередной прогон собрал «хороший» вариант, скопируйте
> папку `outputs/father_report` и `outputs/kitchen_palette_report` в отдельную
> архивную папку с датой, прежде чем запускать новые эксперименты.

## Промежуточные артефакты

Они нужны для отладки/прозрачности и спокойно регенерируются.

### `outputs/audit.md`

Аудит исходных Telegram-экспортов. Перезапись: `python -m mirza_analyzer audit --data-root C:\sarychev\mirzabaeva --out outputs/audit.md`.

### `outputs/sample_posts.md`

Выборка постов для ручной проверки канонической базы. Перезапись:
`python -m mirza_analyzer sample --db outputs/mirza.sqlite --out outputs/sample_posts.md --limit 50`.

### `outputs/candidates/`

Stage 1.5 — детерминированные кандидаты по категориям (полы, цвета, кухни и т.д.).
Только текстовый сигнал, для ручного просмотра. Перезапись:
`python -m mirza_analyzer candidates --db outputs/mirza.sqlite --out-dir outputs/candidates --limit-per-category 100`.

### `outputs/extracted/`

Помимо `extracted_facts.sqlite` (источник истины) тут лежат:

- `extracted_facts_all.csv`, `extracted_facts_clean.csv`, `extracted_facts_needs_review.csv` — три CSV-варианта одного и того же датасета;
- `extracted_facts.jsonl` — JSONL-зеркало;
- `summary.md`, `extraction_quality_summary.md` — сводки;
- `by_category/`, `by_category_clean/`, `by_category_needs_review/` — те же факты, разложенные по категориям в Markdown.

Все эти файлы пересобираются той же командой `extract-facts`.

### `outputs/father_report/category_sections/`, `data_quality_notes.md`, `source_facts_used.csv`, `source_facts_excluded.csv`

Технические выходы Stage 3 рядом с финальным отчётом. Регенерируются командой
`father-report`.

### `outputs/kitchen_palette_report/kitchen_examples.csv`, `kitchen_examples.jsonl`, `kitchen_palette_short.md`, `kitchen_palette_quality_notes.md`, `link_validation_todo.csv`

Промежуточные данные Stage 4 (без `_clean`-фильтра). Регенерируются командой
`kitchen-palette-report`.

## Экспериментальные прогоны LLM-ревью

Stage 2.5 запускали много раз с разными моделями и сэмплами. Каждый прогон писал
в свою папку, чтобы прошлые результаты не затирались.

| Папка | Что это |
|--------|---------|
| `outputs/llm_review/` | Базовый прогон Stage 2.5 в дефолтной конфигурации. |
| `outputs/llm_review_smoke/`, `outputs/llm_review_smoke_1/`, `outputs/llm_review_smoke_5_needs_review/`, `outputs/llm_review_smoke_5_wall_colors/` | Маленькие smoke-прогоны (5–20 фактов) для проверки, что LM Studio отвечает. |
| `outputs/llm_review_gemma_5_needs_review/` | Прогон gemma на 5 спорных фактах. |
| `outputs/llm_review_gptoss_mixed_20/`, `outputs/llm_review_gptoss_mixed_100/` | Прогоны gptoss на смешанной выборке. **`gptoss_mixed_100` используется в команде father-report** как `--llm-review-db`. |
| `outputs/llm_review_gptoss_review_20/`, `outputs/llm_review_gptoss_review_100/` | Прогоны gptoss только на `needs_review`-выборке. |
| `outputs/llm_review_benchmark_*` | Бенчмарки gptoss vs granite по фасеткам review/wall. |

**Важное:** `outputs/llm_review_gptoss_mixed_100/llm_review.sqlite` сейчас задействован
в команде Stage 3 (`--llm-review-db`). Если эту папку удалить, надо либо пересобрать
LLM-ревью, либо запускать `father-report` без `--llm-review-db`.

Остальные `llm_review_*` папки — исторические эксперименты, безопасны к удалению, если они
больше не нужны. Удалять только вручную, никогда автоматизированно.

## Старые отладочные файлы в корне `outputs/`

| Файл | Источник | Что с ним |
|------|----------|-----------|
| `outputs/category_candidates_preview.md` | Старый превью-формат Stage 1.5 | Регенерируется не нужен; оставить как историю или удалить вручную. |
| `outputs/category_scan_v2.md` | Старый сканер категорий до Stage 2 | То же. |
| `outputs/kitchen_stage4_audit.md` | Ручной аудит Stage 4 во время разработки | Полезно как заметка; не нужен для воспроизведения отчёта. |
| `outputs/sqlite_probe.sqlite`, `outputs/extracted/sqlite_probe.sqlite`, `probe*.sqlite` (в корне репо) | Однократные диагностические пробы | Не нужны для пайплайна, можно удалить вручную. |

## Скрипты ручной проверки: `db_checks/`

В `db_checks/` лежат разовые Python-скрипты, которыми мы вручную проверяли SQLite
(например, что у кухонь действительно есть фото, что в `tables` есть ссылки и т.п.).
Это **код, а не выход**, удалять без причины не нужно. Они не вызываются
автоматически и не входят в pytest.

## Что бэкапить

Минимальный набор для бэкапа перед экспериментами:

- `outputs/mirza.sqlite`
- `outputs/extracted/extracted_facts.sqlite`
- `outputs/father_report/` (целиком)
- `outputs/kitchen_palette_report/` (целиком)
- `outputs/llm_review_gptoss_mixed_100/` (используется Stage 3)

Всё остальное либо тривиально регенерируется, либо является историей экспериментов.

## Воспроизведение основных отчётов

См. также README, раздел «Текущий рекомендованный workflow для отца».

```powershell
# 0. (один раз) ингест экспортов Telegram
python -m mirza_analyzer ingest --data-root "C:\sarychev\mirzabaeva" --db outputs/mirza.sqlite

# 1. детерминированные факты (Stage 2 / 2.1)
python -m mirza_analyzer extract-facts `
  --db outputs/mirza.sqlite `
  --out-dir outputs/extracted `
  --source project_articles

# 2. общий отчёт для отца (Stage 3 / 3.1)
python -m mirza_analyzer father-report `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --llm-review-db outputs/llm_review_gptoss_mixed_100/llm_review.sqlite `
  --out-dir outputs/father_report `
  --format markdown

# 3. кухонные палитры (Stage 4 / 4.1)
python -m mirza_analyzer kitchen-palette-report `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --canonical-db outputs/mirza.sqlite `
  --out-dir outputs/kitchen_palette_report `
  --channel-username olya_homestaging `
  --examples-per-category 6 `
  --photos-per-example 2 `
  --format markdown
```

`--llm-review-db` опционален: без него Stage 3 строится только на детерминированных
фактах. Это безопасный fallback, если LLM-ревью устарел или папку убрали.
