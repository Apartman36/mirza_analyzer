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
