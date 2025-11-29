# GameDev Text LLM Assets

Быстрый набор файлов для обучаемой текстовой LLM под Unreal Engine (приоритет русский контент). Теперь описан полный путь: токенизатор → pretraining с нуля → SFT/LoRA → инференс CLI.

- `KnowledgeBase.md` — индекс и правила первой БД (список разделов, протокол обновлений).
- `knowledge_base.jsonl` — стартовый пакет из 61 строки (design/mechanic/level/content/code/qa) с тегами жанр/движок/сложность.
- `GameDevLLM.md` — полный план, формат ответов и датасета.
- `prepare_dataset.py` — нормализация сырых данных → train.jsonl / user_prefs.jsonl.
- `train_tokenizer.py` — обучает SentencePiece Unigram токенизатор (32k/64k) на смешанном ru/en/code корпусе.
- `pretrain_from_scratch.py` — pretraining GPT-подобной модели двух вариантов (A≈350M, B≈7B) на общем корпусе.
- `sft_train.py` — SFT с LoRA на базе pretraining-чекпоинта с `knowledge_base.jsonl` и сессиями.
- `train_lora.py` — (исторический) LoRA поверх готовой open-weights модели; можно переиспользовать при необходимости.
- `infer_cli.py` — интерактивный REPL с режимами Designer/Coder и автологированием; поддерживает slow-thinking RAG (SourcesSummary → ConceptLinks → FinalAnswer) и отвечает развёрнуто (числа, формулы, шаги, без сжатия).
- `search_web.py` — поиск (SerpAPI или DuckDuckGo fallback) + скачивание страниц.
- `chunk_and_embed.py` — разбиение найденного текста и мультиязычные эмбеддинги MiniLM.
- `build_graph.py` — простое построение триплетов (сущность–отношение–сущность) и накопление графа.
- `rag_answer.py` — slow pipeline: план поиска → 2–3 захода → summary → links → финальный ответ.
