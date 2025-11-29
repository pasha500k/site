"""Web search and page fetching helper for the slow, accurate pipeline.

The module is intentionally simple: it supports a hybrid search plan
(BM25-style keyword queries via SerpAPI/DuckDuckGo + optional vector rerank
when embeddings are provided externally). The goal is to keep logic explicit
and debuggable rather than fast.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional

import requests
from bs4 import BeautifulSoup


@dataclass
class SearchResult:
    query: str
    title: str
    url: str
    snippet: str
    content: str
    source_rank: int


def _serpapi_search(query: str, api_key: str, engine: str = "google", num: int = 5) -> List[dict]:
    """Call SerpAPI; returns raw results. Slow but reliable if key is present."""
    endpoint = "https://serpapi.com/search.json"
    params = {"q": query, "api_key": api_key, "engine": engine, "num": num}
    resp = requests.get(endpoint, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("organic_results", [])


def _duckduckgo_fallback(query: str, num: int = 5) -> List[dict]:
    """Very lightweight DuckDuckGo HTML scrape as a fallback when no API key."""
    url = "https://duckduckgo.com/html/"
    resp = requests.post(url, data={"q": query}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for i, link in enumerate(soup.select("a.result__a")):
        if i >= num:
            break
        href = link.get("href", "")
        snippet_el = link.find_parent("div", class_="result__body").find("a", class_="result__snippet")
        snippet = snippet_el.text.strip() if snippet_el else ""
        results.append({"title": link.text.strip(), "link": href, "snippet": snippet})
    return results


def fetch_page(url: str) -> str:
    """Download and lightly clean the page text."""
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Drop scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = " ".join(soup.get_text(" ", strip=True).split())
    return text[:20000]  # keep it bounded for chunking


def search(query: str, top_k: int = 5, api_key: Optional[str] = None, delay: float = 1.0) -> List[SearchResult]:
    """Perform a hybrid-friendly keyword search and fetch page contents.

    If `api_key` is missing, falls back to DuckDuckGo HTML form. Delay is added
    between requests to be polite.
    """
    api_key = api_key or os.getenv("SERPAPI_KEY")
    raw_results: Iterable[dict]
    if api_key:
        raw_results = _serpapi_search(query, api_key, num=top_k)
    else:
        raw_results = _duckduckgo_fallback(query, num=top_k)

    results: List[SearchResult] = []
    for rank, item in enumerate(raw_results):
        url = item.get("link") or item.get("url") or ""
        if not url:
            continue
        snippet = item.get("snippet") or item.get("title") or ""
        try:
            content = fetch_page(url)
        except Exception as exc:  # noqa: BLE001
            content = f"[fetch_failed:{exc}]"
        results.append(
            SearchResult(
                query=query,
                title=item.get("title", ""),
                url=url,
                snippet=snippet,
                content=content,
                source_rank=rank + 1,
            )
        )
        time.sleep(delay)
    return results


def save_results(results: List[SearchResult], path: str) -> None:
    import json

    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run a simple web search and fetch pages.")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--top_k", type=int, default=5)
    parser.add_argument("--out", type=str, default="search_results.jsonl")
    parser.add_argument("--serpapi_key", type=str, help="Optional SerpAPI key")
    args = parser.parse_args()

    results = search(args.query, top_k=args.top_k, api_key=args.serpapi_key)
    save_results(results, args.out)
    print(f"Saved {len(results)} results to {args.out}")


if __name__ == "__main__":
    main()
