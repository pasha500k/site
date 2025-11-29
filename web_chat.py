"""Simple web chat UI + API for slow-thinking GameDev LLM pipeline.

This runs a small Flask server with two parts:
- GET / serves a lightweight ChatGPT-like page (no build tools required).
- POST /api/chat takes {message, mode, stage, engine, genre, constraints, session_id}
  and returns sources summary, concept links ("thoughts"), final answer, and
  dataset log. Optionally uses a local LLM checkpoint to polish the final answer.

Dependencies: Flask, torch, transformers, peft, sentence-transformers (for RAG).
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, TextGenerationPipeline

from rag_answer import slow_answer

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class SessionState:
    stage: str = "prototype"
    engine: str = "ue5"
    genre: List[str] = field(default_factory=list)
    constraints: str = ""
    user_prefs: Dict[str, str] = field(default_factory=dict)
    quality_tags: List[str] = field(default_factory=lambda: ["ru", "structured", "verbose", "slow_rag"])

    def update(self, payload: Dict[str, object]):
        if "stage" in payload and payload["stage"]:
            self.stage = str(payload["stage"])
        if "engine" in payload and payload["engine"]:
            self.engine = str(payload["engine"])
        if "genre" in payload and payload["genre"]:
            if isinstance(payload["genre"], list):
                self.genre = [str(g) for g in payload["genre"] if str(g).strip()]
            else:
                self.genre = [g.strip() for g in str(payload["genre"]).split(",") if g.strip()]
        if "constraints" in payload and payload["constraints"]:
            self.constraints = str(payload["constraints"])


def load_model(model_dir: Optional[Path], tokenizer_dir: Optional[Path], adapter: Optional[Path]):
    if not model_dir:
        return None
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_dir or model_dir, use_fast=True)
    model = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.float16, device_map="auto")
    if adapter:
        model = PeftModel.from_pretrained(model, adapter)
    device = 0 if torch.cuda.is_available() else -1
    return TextGenerationPipeline(model=model, tokenizer=tokenizer, device=device)


def format_prompt(state: SessionState, user_text: str, mode: str, sources: str, links: List[str]) -> str:
    header = f"[MODE:{mode}] [STAGE:{state.stage}] [ENGINE:{state.engine}] [GENRE:{','.join(state.genre)}] [CONSTRAINTS:{state.constraints}]"
    verbosity = (
        "Отвечай развёрнуто, без экономии текста: распиши шаги, числа, формулы, игровые примеры. "
        "Внимание: не делай вывод, пока не увидишь SourcesSummary и ConceptLinks."
    )
    return (
        f"SourcesSummary:\n{sources}\n\n"
        f"ConceptLinks:\n{chr(10).join(links)}\n\n"
        f"Теперь собери FinalAnswer и GameSpec.\n{header}\n{verbosity}\nUSER: {user_text}\nASSISTANT:"
    )


def generate(pipe: Optional[TextGenerationPipeline], prompt: str, max_new_tokens: int) -> str:
    if pipe is None:
        return ""
    outputs = pipe(prompt, max_new_tokens=max_new_tokens, do_sample=True, temperature=0.5, top_p=0.9)
    return outputs[0]["generated_text"][len(prompt) :].strip()


def build_payload(
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
        "spec": final_answer,
    }
    dataset_log = {
        "input": user_text,
        "output": final_answer,
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
        "dataset_log": dataset_log,
        "user_prefs": user_prefs,
        "work_dir": str(work_dir),
    }


def create_app(config):
    app = Flask(__name__, static_folder="static", template_folder="static")
    pipe = load_model(config.model_dir, config.tokenizer, config.adapter)
    sessions: Dict[str, SessionState] = {}

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.route("/static/<path:path>")
    def send_static(path):
        return send_from_directory(app.static_folder, path)

    @app.route("/api/chat", methods=["POST"])
    def chat():
        data = request.get_json(force=True)
        session_id = data.get("session_id") or str(uuid.uuid4())
        state = sessions.setdefault(session_id, SessionState())
        state.update(data)

        message = data.get("message", "").strip()
        if not message:
            return jsonify({"error": "message is required"}), 400

        rag_dir = Path(config.out_dir) / session_id / "rag"
        rag_output = slow_answer(message, rag_dir, emb_model=config.emb_model, device=config.device)

        prompt = format_prompt(state, message, data.get("mode", "Designer"), rag_output.sources_summary, rag_output.concept_links)
        generated = generate(pipe, prompt, max_new_tokens=config.max_new_tokens)
        final_answer = generated if generated else rag_output.final_answer

        payload = build_payload(state, message, rag_output.sources_summary, rag_output.concept_links, final_answer, rag_dir)

        # Save iteration
        save_iteration(Path(config.out_dir) / session_id, payload)

        return jsonify({"session_id": session_id, **payload})

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


def save_iteration(out_dir: Path, payload: Dict[str, object]):
    out_dir.mkdir(parents=True, exist_ok=True)
    train_path = out_dir / "train.jsonl"
    prefs_path = out_dir / "user_prefs.jsonl"
    with train_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload["dataset_log"], ensure_ascii=False) + "\n")
    with prefs_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload["user_prefs"], ensure_ascii=False) + "\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Run web chat for GameDev LLM")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--model_dir", type=Path, help="Optional base model directory for polishing answers")
    parser.add_argument("--tokenizer", type=Path, help="Tokenizer path (defaults to model_dir)")
    parser.add_argument("--adapter", type=Path, help="Optional LoRA adapter directory")
    parser.add_argument("--out_dir", type=Path, default=Path("sessions_web"))
    parser.add_argument("--emb_model", type=str, default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    parser.add_argument("--device", type=str, default="cpu")
    parser.add_argument("--max_new_tokens", type=int, default=512)
    return parser.parse_args()


def main():
    args = parse_args()
    app = create_app(args)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
