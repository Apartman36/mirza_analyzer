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

## Карта этапов

Пайплайн состоит из нескольких слоёв. Каждый следующий читает выходы предыдущего и
ничего не переписывает в источнике истины.

| Этап | Назначение | Главная команда | Главный выход |
|------|------------|------------------|----------------|
| Stage 1 | Аудит + загрузка Telegram-экспортов в одну SQLite | `audit`, `ingest`, `stats`, `sample` | `outputs/mirza.sqlite` |
| Stage 1.5 | Детерминированный поиск кандидатов по категориям | `candidates` | `outputs/candidates/` |
| Stage 2 / 2.1 | Детерминированное извлечение структурированных фактов + clean / needs_review | `extract-facts` | `outputs/extracted/extracted_facts.sqlite` |
| Stage 2.5 | Опциональное LLM-ревью фактов (LM Studio или mock) | `llm-review` | `outputs/llm_review*/llm_review.sqlite` |
| Stage 3 / 3.1 | Общий Markdown-отчёт для отца | `father-report` | `outputs/father_report/father_report_short.md` |
| Stage 4 / 4.1 | Отчёт по кухонным палитрам + фото | `kitchen-palette-report` | `outputs/kitchen_palette_report/kitchen_palette_short_clean.md` |
| Stage 5 (план) | Обмер квартиры + 2D/3D санити-вид | ещё не реализован | см. [docs/plans/stage5_3d_plan.md](docs/plans/stage5_3d_plan.md) |

Карта всех выходных папок и того, что важно/временно/безопасно перегенерировать —
в [docs/project_outputs.md](docs/project_outputs.md).

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

## Stage 1.5: category candidate mining

Слой Stage 1.5 добавляет детерминированный поиск кандидатов по категориям из исходных требований: полы, цвета стен, кухни, стулья, столы, диваны, прихожая и мебель для гостиной.

Команда читает `canonical_messages` и реальные фотографии из `media`, где `media_kind = 'photo'`, сопоставляет текст с UTF-8 конфигурацией категорий и сохраняет доказательства для ручной проверки:

```powershell
python -m mirza_analyzer candidates --db outputs/mirza.sqlite --out-dir outputs/candidates --limit-per-category 100
```

Выходные файлы:

```text
outputs/candidates/
  summary.md
  candidates.csv
  candidates.jsonl
  flooring.md
  wall_colors.md
  kitchens.md
  chairs.md
  tables.md
  sofas.md
  hallway.md
  living_room_furniture.md
  project_article_posts.md
```

Этот слой не использует LLM, OCR, VLM или OpenRouter. Он не делает финальные дизайн-выводы, не утверждает «самое частое» как итоговый факт и не формирует рекомендации по ремонту. Результаты нужны только для ручного просмотра и для решения, где текста достаточно, а где позже понадобятся OCR/VLM/комментарии.

## Stage 2: structured fact extraction

Stage 2 adds deterministic text extraction from project/article posts and high-confidence category candidates. It reads `canonical_messages` and `media` directly from SQLite, so it does not require generated `outputs/candidates/*.md` files to exist.

Default project/article extraction:

```powershell
python -m mirza_analyzer extract-facts --db outputs/mirza.sqlite --out-dir outputs/extracted --source project_articles
```

Useful options:

```powershell
python -m mirza_analyzer extract-facts --db outputs/mirza.sqlite --out-dir outputs/extracted --source all_text --limit 100 --format all
```

`--include-needs-review` is kept only as a deprecated compatibility flag. Stage 2.1 always writes all retained rows and always writes clean/review split files.

## Stage 2.1: extraction QA review buckets

Stage 2.1 repairs deterministic extraction quality issues found during QA. The goal is honesty, not maximum row count: clean rows should be safe deterministic evidence, suspicious rows should stay visible as `needs_review`, and grouped purchase lines should not copy a shared bundle price onto several single-item facts.

This stage is still deterministic. It does not use LLM validation, LM Studio, OpenRouter, OCR, VLM, comments sync, dashboards, PDFs, or father-facing final reports.

`bundle_purchase` rows are review rows for lines such as `Диван, журнальный столик, тумба Divan.ru 80 996₽`. The shared price is kept on the `bundle_purchase` evidence row and is not emitted as clean sofa/table/cabinet prices.

Generated outputs:

```text
outputs/extracted/
  extracted_facts_all.csv
  extracted_facts_clean.csv
  extracted_facts_needs_review.csv
  extracted_facts.jsonl
  extracted_facts.sqlite
  summary.md
  extraction_quality_summary.md
  by_category/
  by_category_clean/
  by_category_needs_review/
    flooring.md
    wall_colors.md
    kitchens.md
    chairs.md
    tables.md
    sofas.md
    hallway.md
    living_room_furniture.md
```

`summary.md` reports total, clean, and `needs_review` facts, plus category splits and top values from clean rows. `extraction_quality_summary.md` reports review signals such as bundles, suspicious descriptors, non-target room contexts, and context/false-positive rows.

The extracted facts are draft evidence rows with source message ids and evidence quotes; they are not father-facing renovation recommendations.

Ожидаемые результаты:

```text
outputs/
  audit.md
  mirza.sqlite
  sample_posts.md
```

## Stage 2.5: LLM-assisted validation and normalization

Stage 2.5 adds an optional reviewer/normalizer layer on top of the deterministic
extracted facts. The LLM **does not** replace the deterministic parser. The
deterministic facts in `outputs/extracted/extracted_facts.sqlite` remain the
source of record. Stage 2.5 writes a separate validation dataset into
`outputs/llm_review/` and never edits `extracted_facts_clean.csv` or
`extracted_facts_needs_review.csv`.

What Stage 2.5 does:

- samples deterministic facts (clean / needs_review / mixed / by category);
- sends each fact plus its evidence quote and the original post excerpt to a
  local LM Studio model (or to an in-process mock for tests);
- requires a strict JSON answer (decision, corrections, normalized terms);
- retries once with a repair prompt if the response is not valid JSON;
- falls back to `decision=needs_human` if the response is still invalid;
- writes JSONL, CSV, SQLite, summary, and per-decision / per-category markdowns.

What Stage 2.5 does **not** do: OCR, VLM, comments sync, dashboards, PDFs,
father-facing final reports, new father-facing categories, OpenRouter integration,
direct edits to deterministic extraction outputs, automatic overwriting of
`extracted_facts_clean.csv` or `extracted_facts_needs_review.csv`.

### Starting LM Studio

Open LM Studio, load one of the local models (for example `qwen3.6-27b`,
`gemma-4-31b`, or `nemotron-3-nano-omni`) and start the OpenAI-compatible
server. The default base URL is `http://127.0.0.1:1234/v1`.

Verify the server is up:

```powershell
curl http://127.0.0.1:1234/v1/models
```

### Example run

```powershell
python -m mirza_analyzer llm-review `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --out-dir outputs/llm_review `
  --provider lmstudio `
  --base-url http://127.0.0.1:1234/v1 `
  --model qwen3.6-27b `
  --sample-size 100 `
  --source mixed
```

Use `--provider mock` to exercise the pipeline without LM Studio. The mock
provider is also what the test suite uses, so `python -m pytest` never
requires a running LM Studio server.

Useful options:

- `--source` — one of `clean`, `needs_review`, `mixed`, `category`.
- `--category` — when `--source category`, filter to a single category.
- `--sample-size`, `--seed` — deterministic sampling (default 100, seed 42).
- `--max-evidence-chars` — bounded source-post excerpt length (default 2500).
- `--canonical-db outputs/mirza.sqlite` — pull original post text into prompts.
- `--temperature` — model temperature, defaults to 0.
- `--timeout-seconds` — per-call HTTP timeout (default 120s).
- `--dry-run` — print the planned sample, write `planned_rows.csv`, make no model calls.
- `--resume` — skip rows whose `(input_hash, provider, model)` are already in the SQLite results.
- `--strict-json` / `--no-strict-json` — require a top-level JSON object; without
  strict mode, the first `{...}` block in the response is salvaged.

### Optional manual smoke test

When LM Studio is running, you can run a small smoke test that is **not** part
of `pytest`:

```powershell
python -m mirza_analyzer llm-review `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --out-dir outputs/llm_review_smoke `
  --provider lmstudio `
  --base-url http://127.0.0.1:1234/v1 `
  --model qwen3.6-27b `
  --sample-size 5 `
  --source needs_review
```

If the server is unavailable, the CLI exits with a clear error instead of a
stack trace.

### Generated outputs

```text
outputs/llm_review/
  llm_review_results.jsonl
  llm_review_results.csv
  llm_review.sqlite
  summary.md
  by_decision/
    keep.md
    fix.md
    discard.md
    needs_human.md
  by_category/
    flooring.md
    wall_colors.md
    kitchens.md
    chairs.md
    tables.md
    sofas.md
    hallway.md
    living_room_furniture.md
  prompts/
    system_prompt.txt
    user_prompt_template.txt
```

All `outputs/llm_review/*` and `outputs/llm_review_smoke/*` files are ignored by
git. Deterministic facts in `outputs/extracted/` are never overwritten by this
stage.

## Stage 3: father-facing Markdown report

Stage 3 writes the first human-readable renovation cheat sheet for the user's
father. It reads the deterministic Stage 2 facts, optionally applies Stage 2.5
LLM review decisions as a QA/enrichment layer, and writes Markdown only.

The report is evidence-based: it summarizes recurring vendors, stores,
materials, colors, facade combinations, models, prices where attached to a
specific row, and compact evidence quotes. It does not create an
apartment-specific design plan and does not infer anything from photos.

Inputs:

```text
outputs/extracted/extracted_facts.sqlite
outputs/llm_review_gptoss_mixed_100/llm_review.sqlite  # optional
outputs/mirza.sqlite                                   # optional photo paths
```

Recommended command:

```powershell
python -m mirza_analyzer father-report `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --llm-review-db outputs/llm_review_gptoss_mixed_100/llm_review.sqlite `
  --out-dir outputs/father_report `
  --format markdown
```

Generated outputs:

```text
outputs/father_report/
  father_report.md
  father_report_short.md
  father_report_summary.md
  data_quality_notes.md
  source_facts_used.csv
  source_facts_excluded.csv
  category_sections/
    flooring.md
    wall_colors.md
    kitchens.md
    chairs.md
    tables.md
    sofas.md
    hallway.md
    living_room_furniture.md
```

Stage 3.1 adds a short curated `father_report_short.md` (2–4 pages), filters
noisy display values out of report aggregations (room contexts like
`в прихожей`, pure article numbers, essay fragments, `Арт`, etc.) without
deleting source rows, renders internal item types (`flooring_laminate`,
`kitchen_facades`, `coffee_table`, …) as Russian phrases, and removes the
duplicated evidence heading in the kitchens section.

Stage 3 does **not** implement HTML, PDF, OCR, VLM/image analysis, Telegram
comments sync, dashboards, OpenRouter, new scraping, or new LLM calls. All
`outputs/father_report/*` files are generated artifacts and are ignored by git.

## Stage 4: kitchen palette report

Stage 4 writes a deeper kitchen-focused Markdown report for manual review. It
groups deterministic kitchen facts by project post, resolves candidate project
links from Telegram text links such as `Артикулы проекта`, extracts cautious
project metadata from the linked post, and classifies kitchens into three
text-based palette hypotheses:

- светлое дерево + тёплый нейтральный фасад;
- дерево + цветной/природный акцент;
- светлый фасад + камень/фартук/столешница как акцент.

Recommended command:

```powershell
python -m mirza_analyzer kitchen-palette-report `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --canonical-db outputs/mirza.sqlite `
  --out-dir outputs/kitchen_palette_report `
  --channel-username olya_homestaging `
  --examples-per-category 6 `
  --photos-per-example 2 `
  --format markdown
```

Inputs:

```text
outputs/extracted/extracted_facts.sqlite
outputs/mirza.sqlite
```

Generated outputs:

```text
outputs/kitchen_palette_report/
  kitchen_palette_report.md
  kitchen_palette_short.md
  kitchen_palette_short_clean.md
  kitchen_palette_quality_notes.md
  kitchen_examples.csv
  kitchen_examples.jsonl
  kitchen_examples_selected_clean.csv
  link_validation_todo.csv
  contact_sheets/
  images_by_example/
```

Stage 4.1 adds a stricter report-layer quality score, suppresses noisy palette
fragments in father-facing summaries, and writes `kitchen_palette_short_clean.md`
plus `kitchen_examples_selected_clean.csv` for the examples actually used in the
clean report. Categories are not padded to six examples; medium examples are
used only when a category has fewer than three high-quality examples.

The report is deterministic and local-first. It does not require LM Studio and
does not call any LLM. It does not implement 3D rendering, apartment-specific
design generation, HTML/PDF, OCR, VLM/image understanding, OpenRouter, new
scraping, Telegram comments sync, RAG/chatbot, or a web dashboard. Telegram
links in the report are candidates and require manual verification. Photos and
contact sheets are attached mechanically from the same post/message series;
image content is not interpreted.

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

## Текущий рекомендованный workflow для отца

Минимальный путь от чистого клона до отчётов, которые можно показать отцу.

```powershell
# 1. Канонический SQLite из Telegram-экспортов (один раз)
python -m mirza_analyzer ingest --data-root "C:\sarychev\mirzabaeva" --db outputs/mirza.sqlite

# 2. Детерминированные факты (Stage 2 / 2.1)
python -m mirza_analyzer extract-facts `
  --db outputs/mirza.sqlite `
  --out-dir outputs/extracted `
  --source project_articles

# 3. Общий отчёт для отца (Stage 3 / 3.1)
python -m mirza_analyzer father-report `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --llm-review-db outputs/llm_review_gptoss_mixed_100/llm_review.sqlite `
  --out-dir outputs/father_report `
  --format markdown

# 4. Кухонные палитры (Stage 4 / 4.1)
python -m mirza_analyzer kitchen-palette-report `
  --facts-db outputs/extracted/extracted_facts.sqlite `
  --canonical-db outputs/mirza.sqlite `
  --out-dir outputs/kitchen_palette_report `
  --channel-username olya_homestaging `
  --examples-per-category 6 `
  --photos-per-example 2 `
  --format markdown
```

`--llm-review-db` опционален. Если соответствующей папки нет, Stage 3 можно
запускать без него — отчёт станет строго детерминированным.

## Что отправить отцу сейчас

Готовые «чистые» материалы, которые сейчас имеет смысл показывать:

- [`outputs/father_report/father_report_short.md`](outputs/father_report/father_report_short.md) — короткий 2–4-страничный отчёт по всем категориям (главный документ);
- [`outputs/father_report/father_report.md`](outputs/father_report/father_report.md) — полный детерминированный отчёт со всеми категориями (как референс);
- [`outputs/kitchen_palette_report/kitchen_palette_short_clean.md`](outputs/kitchen_palette_report/kitchen_palette_short_clean.md) — очищенный отчёт по трём кухонным палитрам;
- [`outputs/kitchen_palette_report/kitchen_palette_report.md`](outputs/kitchen_palette_report/kitchen_palette_report.md) — полный отчёт по кухням с примерами и метаданными;
- [`outputs/kitchen_palette_report/kitchen_examples_selected_clean.csv`](outputs/kitchen_palette_report/kitchen_examples_selected_clean.csv) — таблица выбранных кухонных примеров для ручной сверки;
- [`outputs/kitchen_palette_report/contact_sheets/`](outputs/kitchen_palette_report/contact_sheets/) и [`outputs/kitchen_palette_report/images_by_example/`](outputs/kitchen_palette_report/images_by_example/) — контактные листы и фото по проектам.

Эти файлы не лежат в git (они в `.gitignore`), но генерируются командами выше.

## Чему пока не доверять

Что в репозитории есть, но **ещё не готово показывать как итог**:

- любые «топ-N» подсчёты в полных отчётах без ручной сверки против исходных постов — Stage 2 детерминирован, но часть рядов уходит в `needs_review`;
- ряды с `bundle_purchase` (групповые покупки): общая цена не делится по позициям и помечена как `needs_review`;
- ссылки на проекты в `kitchen_palette_report.md` — это **кандидаты**, требуют ручной проверки (см. `link_validation_todo.csv`);
- любая ссылка на содержимое фотографии — фото подцепляются **механически** по message-серии, никакого VLM/OCR нет;
- сравнения моделей в `outputs/llm_review_*` — это эксперименты, не источник истины;
- никаких 3D-визуализаций, AI-картинок интерьеров и автоматических планировок в проекте сейчас нет (см. план Stage 5).

Все эти ограничения сознательные: проект сначала фиксирует надёжную базу, а уже
потом продвигается к выводам.

## Сгенерированные выходы и git

Все папки и файлы в `outputs/` игнорируются git — см. `.gitignore`. В репозитории
лежит только код, тесты, документация и `.gitkeep`-маркеры. Любая папка вида
`outputs/<что-то>/` отсутствует на свежем клоне и пересобирается соответствующей
командой. Подробная карта выходов — в
[docs/project_outputs.md](docs/project_outputs.md).

Сырые Telegram-экспорты в `C:\sarychev\mirzabaeva` тоже не в репозитории и
read-only для всех команд проекта.

## Тестирование

Все тесты — детерминированные, без сети, без LM Studio, без OCR. LLM-провайдер
заменяется in-process mock'ом.

```powershell
python -B -m pytest
```

Тесты покрывают: нормализацию текста, слияние сообщений, детекцию медиа,
матчинг категорий, CLI-команды `candidates`, `extract-facts`, паттерны
извлечения, `llm-review` (с mock-провайдером), `father-report` и
`kitchen-palette-report`.

## Текущее состояние

- Stage 1, 1.5, 2, 2.1 — стабильные, детерминированные, покрыты тестами.
- Stage 2.5 — опциональный; используется как QA-слой поверх Stage 2.
- Stage 3 / 3.1 — стабильный, отчёт `father_report_short.md` пригоден для отца.
- Stage 4 / 4.1 — стабильный, отчёт `kitchen_palette_short_clean.md` пригоден для отца.
- Stage 5 (обмер квартиры + 2D/3D санити-вид) — **только план**, см. [docs/plans/stage5_3d_plan.md](docs/plans/stage5_3d_plan.md). Имплементации в репозитории нет.

## Дополнительная документация

- [docs/project_outputs.md](docs/project_outputs.md) — карта всех выходных файлов: что важно, что временное, что можно регенерировать.
- [docs/plans/stage5_3d_plan.md](docs/plans/stage5_3d_plan.md) — план следующего этапа (обмер квартиры, JSON-схема, 2D санити-вид, Three.js-вьюер, опциональный Blender).
