"""Slow-thinking RAG pipeline that respects the "долго, но верно" policy.

Steps per query:
1) Plan search intents.
2) Run 2-3 search rounds (refine if weak).
3) Chunk + embed results, rerank, summarize.
4) Build concept links from a small graph layer.
5) Produce final answer only after (3) and (4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

import torch
from transformers import AutoTokenizer, AutoModel

from build_graph import Triple, build_adjacency, build_triples, merge_with_existing
from chunk_and_embed import Chunk, build_chunks, embed_chunks, save_chunks
from search_web import SearchResult, save_results, search


@dataclass
class SlowAnswer:
    sources_summary: str
    concept_links: List[str]
    final_answer: str
    used_results: List[SearchResult]
    triples: List[Triple]


# simple cosine similarity

def _cosine(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    a_norm = torch.nn.functional.normalize(a, p=2, dim=1)
    b_norm = torch.nn.functional.normalize(b, p=2, dim=1)
    return torch.matmul(a_norm, b_norm.T)


def plan_search(query: str) -> List[str]:
    # For now: 2–3 focused queries; can be expanded.
    return [query, f"{query} Unreal Engine design", f"{query} core loop mechanics"]


def summarize_sources(chunks: List[Chunk], top_idxs: List[int]) -> str:
    lines = []
    for i in top_idxs:
        c = chunks[i]
        lines.append(f"[{c.source_rank}] {c.title or c.url}: {c.text[:180]}…")
    return "\n".join(lines)


def build_concept_links(triples: List[Triple]) -> List[str]:
    adj = build_adjacency(triples)
    links = []
    for head, rels in adj.items():
        rel_str = ", ".join([f"{rel} -> {tail}" for rel, tail in rels[:4]])
        links.append(f"{head}: {rel_str}")
    return links[:10]


def slow_answer(query: str, work_dir: Path, emb_model: str, device: str = "cpu") -> SlowAnswer:
    work_dir.mkdir(parents=True, exist_ok=True)
    # 1) Plan search
    planned_queries = plan_search(query)

    # 2) Run 2–3 searches
    search_results: List[SearchResult] = []
    for q in planned_queries:
        hits = search(q, top_k=4)
        search_results.extend(hits)
    save_results(search_results, work_dir / "search_results.jsonl")

    if not search_results:
        fallback_text = "Не нашла надёжные источники по запросу; уточните формулировку."
        return SlowAnswer(
            sources_summary=fallback_text,
            concept_links=[],
            final_answer=fallback_text,
            used_results=[],
            triples=[],
        )

    # 3) Chunk + embed + simple rerank
    chunks = build_chunks(work_dir / "search_results.jsonl")
    embeddings, _ = embed_chunks(chunks, model_name=emb_model, device=device)
    chunks_path, emb_path = save_chunks(chunks, embeddings, work_dir / "index")
    # query embedding
    tok = AutoTokenizer.from_pretrained(emb_model)
    mod = AutoModel.from_pretrained(emb_model).to(device)
    mod.eval()
    enc = tok([query], return_tensors="pt", truncation=True, padding=True).to(device)
    with torch.no_grad():
        q_emb = torch.nn.functional.normalize(mod(**enc).last_hidden_state.mean(dim=1), p=2, dim=1)
    sims = _cosine(q_emb, embeddings)[0]
    top_vals, top_idxs = torch.topk(sims, k=min(5, sims.size(0)))
    top_idxs_list = top_idxs.tolist()

    # 4) Build graph and concept links
    triples = build_triples(chunks_path)
    graph_path = work_dir / "graph.jsonl"
    merged_triples = merge_with_existing(triples, graph_path)
    from build_graph import save_graph

    save_graph(merged_triples, graph_path)
    concept_links = build_concept_links(merged_triples)

    # 5) Summaries and final answer template (LLM-free for now; hook your LM here)
    sources_summary = summarize_sources(chunks, top_idxs_list)
    final_answer = (
        "Черновой ответ на основе найденных фрагментов и связей."
        " Дополните своим LLM для красивой формулировки."
    )

    return SlowAnswer(
        sources_summary=sources_summary,
        concept_links=concept_links,
        final_answer=final_answer,
        used_results=search_results,
        triples=merged_triples,
    )


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Slow-thinking RAG answerer")
    parser.add_argument("query", help="User question")
    parser.add_argument("--work_dir", type=Path, default=Path("rag_work"))
    parser.add_argument("--emb_model", type=str, default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    parser.add_argument("--device", type=str, default="cpu")
    args = parser.parse_args()

    ans = slow_answer(args.query, args.work_dir, args.emb_model, device=args.device)
    print("SourcesSummary:\n", ans.sources_summary)
    print("ConceptLinks:\n", "\n".join(ans.concept_links))
    print("FinalAnswer:\n", ans.final_answer)


if __name__ == "__main__":
    main()
