"""Chunking and embedding utilities for slow, accurate RAG.

Reads search results JSONL (output of search_web.py), splits content into
manageable chunks, and computes embeddings with a multilingual MiniLM model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import torch
from transformers import AutoModel, AutoTokenizer


@dataclass
class Chunk:
    query: str
    url: str
    title: str
    snippet: str
    text: str
    chunk_id: str
    source_rank: int


DEFAULT_EMB_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def iter_results(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def chunk_text(text: str, max_tokens: int = 180, stride: int = 120) -> List[str]:
    """Roughly split by whitespace count; not tokenizer-accurate but simple."""
    words = text.split()
    chunks = []
    start = 0
    idx = 0
    while start < len(words):
        end = min(start + max_tokens, len(words))
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))
        start += stride
        idx += 1
    return chunks


def build_chunks(input_path: Path) -> List[Chunk]:
    chunks: List[Chunk] = []
    for row in iter_results(input_path):
        base_id = f"{row.get('source_rank','x')}_{row.get('url','')[:30]}".replace("/", "_")
        for i, piece in enumerate(chunk_text(row.get("content", ""))):
            chunk_id = f"{base_id}_c{i}"
            chunks.append(
                Chunk(
                    query=row.get("query", ""),
                    url=row.get("url", ""),
                    title=row.get("title", ""),
                    snippet=row.get("snippet", ""),
                    text=piece,
                    chunk_id=chunk_id,
                    source_rank=row.get("source_rank", 0),
                )
            )
    return chunks


def mean_pool(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    return torch.sum(last_hidden_state * mask, dim=1) / torch.clamp(mask.sum(dim=1), min=1e-9)


def embed_chunks(chunks: List[Chunk], model_name: str = DEFAULT_EMB_MODEL, device: str = "cpu"):
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name).to(device)
    model.eval()

    texts = [c.text for c in chunks]
    enc = tokenizer(texts, padding=True, truncation=True, max_length=256, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**enc)
        embeddings = mean_pool(outputs.last_hidden_state, enc.attention_mask)
        embeddings = torch.nn.functional.normalize(embeddings, p=2, dim=1)
    return embeddings.cpu(), enc


def save_chunks(chunks: List[Chunk], embeddings: torch.Tensor, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "chunks.jsonl"
    emb_path = out_dir / "embeddings.pt"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c.__dict__, ensure_ascii=False) + "\n")
    torch.save(embeddings, emb_path)
    return jsonl_path, emb_path


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Chunk search results and embed them.")
    parser.add_argument("input", type=Path, help="search_results.jsonl from search_web.py")
    parser.add_argument("--out_dir", type=Path, default=Path("index"))
    parser.add_argument("--model", type=str, default=DEFAULT_EMB_MODEL)
    parser.add_argument("--device", type=str, default="cpu")
    args = parser.parse_args()

    chunks = build_chunks(args.input)
    embeddings, _ = embed_chunks(chunks, model_name=args.model, device=args.device)
    jsonl_path, emb_path = save_chunks(chunks, embeddings, args.out_dir)
    print(f"Saved {len(chunks)} chunks to {jsonl_path} and embeddings to {emb_path}")


if __name__ == "__main__":
    main()
