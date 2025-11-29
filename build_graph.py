"""Very small Knowledge Graph builder from web chunks.

Goal: extract rough entity-relation triples to support "concept links" in the
slow pipeline. This is intentionally heuristic (no heavy NLP deps) and
append-only: new triples are added, old не удаляются.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Tuple


@dataclass
class Triple:
    head: str
    relation: str
    tail: str
    source_url: str
    source_chunk: str


COMMON_RELATIONS = [
    "является",
    "похоже на",
    "использует",
    "зависит от",
    "противоречит",
    "улучшает",
    "ухудшает",
]


def iter_chunks(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def extract_candidates(text: str, top_k: int = 8) -> List[str]:
    # naive keyword extractor: pick frequent capitalized tokens and multi-words
    tokens = re.findall(r"[A-Za-zА-Яа-я0-9#\+]{3,}", text)
    freq = Counter(tokens)
    return [w for w, _ in freq.most_common(top_k)]


def build_triples(chunks_path: Path, min_count: int = 2) -> List[Triple]:
    triples: List[Triple] = []
    for row in iter_chunks(chunks_path):
        candidates = extract_candidates(row.get("text", ""))
        if len(candidates) < 2:
            continue
        # simple co-occurrence relations
        for i in range(len(candidates) - 1):
            head, tail = candidates[i], candidates[i + 1]
            relation = "использует" if "Engine" in tail or "UE" in tail else "похоже на"
            triples.append(
                Triple(
                    head=head,
                    relation=relation,
                    tail=tail,
                    source_url=row.get("url", ""),
                    source_chunk=row.get("chunk_id", ""),
                )
            )
    # deduplicate by (head, relation, tail)
    uniq = {}
    for t in triples:
        key = (t.head, t.relation, t.tail)
        if key not in uniq:
            uniq[key] = t
    return list(uniq.values())


def merge_with_existing(new_triples: List[Triple], graph_path: Path) -> List[Triple]:
    if graph_path.exists():
        existing = {(r["head"], r["relation"], r["tail"]): r for r in iter_chunks(graph_path)}
    else:
        existing = {}
    for t in new_triples:
        key = (t.head, t.relation, t.tail)
        if key not in existing:
            existing[key] = asdict(t)
    return [Triple(**v) for v in existing.values()]


def save_graph(triples: List[Triple], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for t in triples:
            f.write(json.dumps(asdict(t), ensure_ascii=False) + "\n")


def build_adjacency(triples: List[Triple]) -> dict:
    adj: dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for t in triples:
        adj[t.head].append((t.relation, t.tail))
    return adj


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build or extend a tiny knowledge graph from chunks.")
    parser.add_argument("chunks", type=Path, help="chunks.jsonl from chunk_and_embed.py")
    parser.add_argument("--graph", type=Path, default=Path("graph.jsonl"))
    args = parser.parse_args()

    triples = build_triples(args.chunks)
    merged = merge_with_existing(triples, args.graph)
    save_graph(merged, args.graph)
    print(f"Saved {len(merged)} triples to {args.graph}")


if __name__ == "__main__":
    main()
