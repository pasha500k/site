"""Interactive CLI with slow, accurate RAG (internet + concept links)."""

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, TextGenerationPipeline

from rag_answer import slow_answer

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class SessionState:
    def __init__(self, stage="prototype", engine="ue5", genre=None, constraints=None):
        self.stage = stage
        self.engine = engine
        self.genre = genre or []
        self.constraints = constraints or ""
        self.user_prefs: Dict[str, str] = {}
        self.quality_tags = ["ru", "structured", "verbose", "slow_rag"]

    def update_from_command(self, cmd: str, value: str):
        if cmd == "/stage":
            self.stage = value
        elif cmd == "/engine":
            self.engine = value
        elif cmd == "/genre":
            self.genre = [g.strip() for g in value.split(",") if g.strip()]
        elif cmd == "/constraints":
            self.constraints = value
        elif cmd == "/patch":
            self.user_prefs["last_patch"] = value



def load_model(model_dir: Path, tokenizer_dir: Path, adapter: Path | None):
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir, use_fast=True)
    model = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.float16, device_map="auto")
    if adapter:
        model = PeftModel.from_pretrained(model, adapter)
    pipe = TextGenerationPipeline(model=model, tokenizer=tokenizer, device=0 if torch.cuda.is_available() else -1)
    return pipe


def format_prompt(state: SessionState, user_text: str, mode: str) -> str:
    header = f"[MODE:{mode}] [STAGE:{state.stage}] [ENGINE:{state.engine}] [GENRE:{','.join(state.genre)}] [CONSTRAINTS:{state.constraints}]"
    verbosity = (
        "Отвечай развёрнуто, без экономии текста: распиши шаги, числа, формулы, игровые примеры. "
        "Стиль живой, по-человечески, но подробный. Внимание: не делай вывод, пока не увидишь SourcesSummary и ConceptLinks."  # noqa: E501
    )
    return f"{header}\n{verbosity}\nUSER: {user_text}\nASSISTANT:"


def generate(pipe: TextGenerationPipeline, prompt: str, max_new_tokens: int) -> str:
    outputs = pipe(prompt, max_new_tokens=max_new_tokens, do_sample=True, temperature=0.5, top_p=0.9)
    return outputs[0]["generated_text"][len(prompt) :].strip()


def build_response(
    raw_text: str,
    state: SessionState,
    user_text: str,
    sources_summary: str,
    concept_links: List[str],
    final_answer: str,
    work_dir: Path,
) -> Dict[str, object]:
    now = datetime.utcnow().strftime(ISO_FORMAT)
    game_spec = {
        "stage": state.stage,
        "engine": state.engine,
        "genre": state.genre,
        "constraints": state.constraints,
        "spec": raw_text,
    }
    explanation = (
        "Ответ сформирован без сжатия: сначала поиск и связки, затем итог."
    )
    dataset_log = {
        "input": user_text,
        "output": raw_text,
        "stage": state.stage,
        "engine": state.engine,
        "genre": state.genre,
        "quality_tags": state.quality_tags,
        "timestamp": now,
        "sources": sources_summary,
        "concept_links": concept_links,
    }
    user_prefs = state.user_prefs | {"last_interaction": now}
    return {
        "sources_summary": sources_summary,
        "concept_links": concept_links,
        "final_answer": final_answer,
        "game_spec": game_spec,
        "explanation": explanation,
        "dataset_log": dataset_log,
        "user_prefs": user_prefs,
        "work_dir": str(work_dir),
    }


def save_iteration(out_dir: Path, payload: Dict[str, object]):
    out_dir.mkdir(parents=True, exist_ok=True)
    train_path = out_dir / "train.jsonl"
    prefs_path = out_dir / "user_prefs.jsonl"
    with train_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload["dataset_log"], ensure_ascii=False) + "\n")
    with prefs_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload["user_prefs"], ensure_ascii=False) + "\n")


def repl(
    pipe: Optional[TextGenerationPipeline],
    state: SessionState,
    out_dir: Path,
    max_new_tokens: int,
    slow_rag: bool,
    emb_model: str,
    device: str,
):
    mode = "Designer"
    print(
        "Введите текст. Команды: /stage, /engine, /genre, /constraints, /patch. /mode coder переключает режим."
    )
    while True:
        user_text = input("you> ").strip()
        if not user_text:
            continue
        if user_text.lower() in {"/exit", "/quit"}:
            break
        if user_text.lower() == "/mode coder":
            mode = "Coder"
            print("Режим: Coder")
            continue
        if user_text.lower() == "/mode designer":
            mode = "Designer"
            print("Режим: Designer")
            continue
        if user_text.startswith("/"):
            parts = user_text.split(maxsplit=1)
            if len(parts) == 2:
                cmd, val = parts
                state.update_from_command(cmd, val)
                print(f"Обновил {cmd} -> {val}")
                continue
        rag_dir = out_dir / "rag"
        rag_output = slow_answer(user_text, rag_dir, emb_model=emb_model, device=device)
        prompt = format_prompt(state, user_text, mode)
        prompt = (
            f"SourcesSummary:\n{rag_output.sources_summary}\n\n"
            f"ConceptLinks:\n{chr(10).join(rag_output.concept_links)}\n\n"
            f"Now craft FinalAnswer and a detailed GameSpec before finishing.\n{prompt}"
        )
        raw = generate(pipe, prompt, max_new_tokens=max_new_tokens) if pipe else rag_output.final_answer
        final_answer = raw if raw else rag_output.final_answer
        payload = build_response(
            raw_text=final_answer,
            state=state,
            user_text=user_text,
            sources_summary=rag_output.sources_summary,
            concept_links=rag_output.concept_links,
            final_answer=final_answer,
            work_dir=rag_dir,
        )
        print("SourcesSummary:\n", payload["sources_summary"])
        print("ConceptLinks:\n", "\n".join(payload["concept_links"]))
        print("FinalAnswer:\n", payload["final_answer"])
        print("A) GameSpec JSON:\n", json.dumps(payload["game_spec"], ensure_ascii=False, indent=2))
        print("B) Explanation:\n", payload["explanation"])
        print("C) DatasetLog:\n", json.dumps(payload["dataset_log"], ensure_ascii=False, indent=2))
        print("D) UserPrefs:\n", json.dumps(payload["user_prefs"], ensure_ascii=False, indent=2))
        save_iteration(out_dir, payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir", type=Path, default=Path("artifacts/pretrain/variant_A"), help="Base model directory")
    parser.add_argument("--tokenizer", type=Path, default=Path("artifacts/pretrain/variant_A"))
    parser.add_argument("--adapter", type=Path, help="Optional LoRA adapter directory")
    parser.add_argument("--out_dir", type=Path, default=Path("sessions"))
    parser.add_argument("--max_new_tokens", type=int, default=512)
    parser.add_argument("--slow_rag", action="store_true", help="Use slow-thinking RAG pipeline")
    parser.add_argument("--emb_model", type=str, default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    parser.add_argument("--device", type=str, default="cpu")
    args = parser.parse_args()

    pipe = load_model(args.model_dir, args.tokenizer, args.adapter) if args.slow_rag else None
    state = SessionState()
    repl(pipe, state, args.out_dir, args.max_new_tokens, args.slow_rag, args.emb_model, args.device)


if __name__ == "__main__":
    main()
