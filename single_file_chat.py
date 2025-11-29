"""All-in-one slow-thinking GameDev chat with web UI in a single file.

Features packed here:
- Hybrid web search + polite page fetch (DuckDuckGo fallback, SerpAPI if key provided)
- Chunking + multilingual MiniLM embeddings for rerank
- Heuristic concept-link knowledge graph
- Slow-thinking orchestrator (plan → search → summarize → links → answer)
- Optional local LLM polishing (HF transformers + PEFT LoRA)
- Flask web UI that serves a ChatGPT-style page with thoughts panel

Run:
  python single_file_chat.py --host 0.0.0.0 --port 7860 --device cpu
Add --model_dir /path/to/llm --adapter /path/to/lora to polish answers.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
import torch
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request
from peft import PeftModel
from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer, TextGenerationPipeline

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_EMB_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# ----------------------------
# Data classes
# ----------------------------


@dataclass
class SearchResult:
    query: str
    title: str
    url: str
    snippet: str
    content: str
    source_rank: int


@dataclass
class Chunk:
    query: str
    url: str
    title: str
    snippet: str
    text: str
    chunk_id: str
    source_rank: int


@dataclass
class Triple:
    head: str
    relation: str
    tail: str
    source_url: str
    source_chunk: str


@dataclass
class SlowAnswer:
    sources_summary: str
    concept_links: List[str]
    final_answer: str
    used_results: List[SearchResult]
    triples: List[Triple]


@dataclass
class SessionState:
    stage: str = "prototype"
    engine: str = "ue5"
    genre: List[str] = field(default_factory=list)
    constraints: str = ""
    user_prefs: Dict[str, str] = field(default_factory=dict)
    quality_tags: List[str] = field(default_factory=lambda: ["ru", "structured", "verbose", "slow_rag"])

    def update(self, payload: Dict[str, object]):
        if payload.get("stage"):
            self.stage = str(payload["stage"])
        if payload.get("engine"):
            self.engine = str(payload["engine"])
        if payload.get("genre"):
            if isinstance(payload["genre"], list):
                self.genre = [str(g) for g in payload["genre"] if str(g).strip()]
            else:
                self.genre = [g.strip() for g in str(payload["genre"]).split(",") if g.strip()]
        if payload.get("constraints"):
            self.constraints = str(payload["constraints"])


# ----------------------------
# Search utilities
# ----------------------------


def _serpapi_search(query: str, api_key: str, engine: str = "google", num: int = 5) -> List[dict]:
    endpoint = "https://serpapi.com/search.json"
    params = {"q": query, "api_key": api_key, "engine": engine, "num": num}
    resp = requests.get(endpoint, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("organic_results", [])


def _duckduckgo_fallback(query: str, num: int = 5) -> List[dict]:
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
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = " ".join(soup.get_text(" ", strip=True).split())
    return text[:20000]


def search(query: str, top_k: int = 5, api_key: Optional[str] = None, delay: float = 1.0) -> List[SearchResult]:
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


# ----------------------------
# Chunking + embeddings
# ----------------------------


def chunk_text(text: str, max_tokens: int = 180, stride: int = 120) -> List[str]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + max_tokens, len(words))
        chunks.append(" ".join(words[start:end]))
        start += stride
    return chunks


def build_chunks(results: List[SearchResult]) -> List[Chunk]:
    chunks: List[Chunk] = []
    for row in results:
        base_id = f"{row.source_rank}_{row.url[:30]}".replace("/", "_")
        for i, piece in enumerate(chunk_text(row.content)):
            chunk_id = f"{base_id}_c{i}"
            chunks.append(
                Chunk(
                    query=row.query,
                    url=row.url,
                    title=row.title,
                    snippet=row.snippet,
                    text=piece,
                    chunk_id=chunk_id,
                    source_rank=row.source_rank,
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


# ----------------------------
# Concept links (graph)
# ----------------------------


def extract_candidates(text: str, top_k: int = 8) -> List[str]:
    import re
    from collections import Counter

    tokens = re.findall(r"[A-Za-zА-Яа-я0-9#\+]{3,}", text)
    freq = Counter(tokens)
    return [w for w, _ in freq.most_common(top_k)]


def build_triples(chunks: List[Chunk]) -> List[Triple]:
    triples: List[Triple] = []
    for row in chunks:
        candidates = extract_candidates(row.text)
        if len(candidates) < 2:
            continue
        for i in range(len(candidates) - 1):
            head, tail = candidates[i], candidates[i + 1]
            relation = "использует" if "Engine" in tail or "UE" in tail else "похоже на"
            triples.append(
                Triple(
                    head=head,
                    relation=relation,
                    tail=tail,
                    source_url=row.url,
                    source_chunk=row.chunk_id,
                )
            )
    uniq = {}
    for t in triples:
        key = (t.head, t.relation, t.tail)
        if key not in uniq:
            uniq[key] = t
    return list(uniq.values())


def build_adjacency(triples: List[Triple]) -> dict:
    from collections import defaultdict

    adj: dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for t in triples:
        adj[t.head].append((t.relation, t.tail))
    return adj


def build_concept_links(triples: List[Triple]) -> List[str]:
    adj = build_adjacency(triples)
    links = []
    for head, rels in adj.items():
        rel_str = ", ".join([f"{rel} -> {tail}" for rel, tail in rels[:4]])
        links.append(f"{head}: {rel_str}")
    return links[:10]


# ----------------------------
# Slow-thinking pipeline
# ----------------------------


def _cosine(a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    a_norm = torch.nn.functional.normalize(a, p=2, dim=1)
    b_norm = torch.nn.functional.normalize(b, p=2, dim=1)
    return torch.matmul(a_norm, b_norm.T)


def plan_search(query: str) -> List[str]:
    return [query, f"{query} Unreal Engine design", f"{query} core loop mechanics"]


def summarize_sources(chunks: List[Chunk], top_idxs: List[int]) -> str:
    lines = []
    for i in top_idxs:
        c = chunks[i]
        lines.append(f"[{c.source_rank}] {c.title or c.url}: {c.text[:180]}…")
    return "\n".join(lines)


def slow_answer(query: str, work_dir: Path, emb_model: str, device: str = "cpu") -> SlowAnswer:
    work_dir.mkdir(parents=True, exist_ok=True)
    planned_queries = plan_search(query)

    search_results: List[SearchResult] = []
    for q in planned_queries:
        hits = search(q, top_k=4)
        search_results.extend(hits)

    if not search_results:
        fallback_text = "Не нашла надёжные источники по запросу; уточните формулировку."
        return SlowAnswer(fallback_text, [], fallback_text, [], [])

    chunks = build_chunks(search_results)
    embeddings, _ = embed_chunks(chunks, model_name=emb_model, device=device)

    tok = AutoTokenizer.from_pretrained(emb_model)
    mod = AutoModel.from_pretrained(emb_model).to(device)
    mod.eval()
    enc = tok([query], return_tensors="pt", truncation=True, padding=True).to(device)
    with torch.no_grad():
        q_emb = torch.nn.functional.normalize(mod(**enc).last_hidden_state.mean(dim=1), p=2, dim=1)
    sims = _cosine(q_emb, embeddings)[0]
    top_vals, top_idxs = torch.topk(sims, k=min(5, sims.size(0)))
    top_idxs_list = top_idxs.tolist()

    triples = build_triples(chunks)
    concept_links = build_concept_links(triples)

    sources_summary = summarize_sources(chunks, top_idxs_list)
    final_answer = (
        "Черновой ответ на основе найденных фрагментов и связей. "
        "Дополните своим LLM для красивой формулировки."
    )

    return SlowAnswer(
        sources_summary=sources_summary,
        concept_links=concept_links,
        final_answer=final_answer,
        used_results=search_results,
        triples=triples,
    )


# ----------------------------
# Optional LLM polishing
# ----------------------------


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


# ----------------------------
# Flask app
# ----------------------------


def inline_page() -> str:
    return """
<!DOCTYPE html>
<html lang=\"ru\">
<head>
  <meta charset=\"UTF-8\" />
  <title>GameDev Slow Chat</title>
  <style>
    body { font-family: 'Inter', system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; }
    #app { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; height: 100vh; padding: 12px; box-sizing: border-box; }
    .panel { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 12px; overflow: auto; }
    #chat { display: flex; flex-direction: column; height: 100%; }
    #history { flex: 1; overflow-y: auto; margin-bottom: 12px; }
    .bubble { padding: 10px 12px; border-radius: 8px; margin: 6px 0; max-width: 92%; }
    .user { background: #0ea5e9; color: #0b1726; margin-left: auto; }
    .bot { background: #111827; border: 1px solid #334155; }
    #controls { display: flex; gap: 8px; }
    input, select { background: #0b1220; color: #e2e8f0; border: 1px solid #334155; border-radius: 8px; padding: 8px; }
    button { background: #22c55e; border: none; color: #0b1220; padding: 10px 14px; border-radius: 8px; cursor: pointer; font-weight: 600; }
    button:disabled { background: #4b5563; cursor: not-allowed; }
    pre { white-space: pre-wrap; word-wrap: break-word; }
  </style>
</head>
<body>
  <div id=\"app\">
    <div id=\"chat\" class=\"panel\">
      <div id=\"history\"></div>
      <div id=\"controls\">
        <input id=\"message\" placeholder=\"Опишите задачу для игры...\" style=\"flex:1\" />
        <button id=\"send\">Отправить</button>
      </div>
      <div style=\"margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;\">
        <input id=\"stage\" placeholder=\"stage (prototype)\" />
        <input id=\"engine\" placeholder=\"engine (ue5)\" />
        <input id=\"genre\" placeholder=\"genre (shooter, roguelike)\" style=\"flex:1\" />
        <input id=\"constraints\" placeholder=\"constraints\" style=\"flex:1\" />
        <select id=\"mode\"><option>Designer</option><option>Coder</option></select>
      </div>
    </div>
    <div id=\"thoughts\" class=\"panel\">
      <h3>SourcesSummary</h3>
      <pre id=\"sources\"></pre>
      <h3>ConceptLinks</h3>
      <pre id=\"links\"></pre>
      <h3>DatasetLog</h3>
      <pre id=\"log\"></pre>
    </div>
  </div>
<script>
  const history = document.getElementById('history');
  const sendBtn = document.getElementById('send');
  const message = document.getElementById('message');
  let sessionId = localStorage.getItem('session_id') || '';

  function addBubble(text, cls) {
    const div = document.createElement('div');
    div.className = 'bubble ' + cls;
    div.innerText = text;
    history.appendChild(div);
    history.scrollTop = history.scrollHeight;
  }

  async function send() {
    const payload = {
      message: message.value,
      session_id: sessionId,
      stage: document.getElementById('stage').value,
      engine: document.getElementById('engine').value,
      genre: document.getElementById('genre').value,
      constraints: document.getElementById('constraints').value,
      mode: document.getElementById('mode').value
    };
    addBubble(payload.message, 'user');
    sendBtn.disabled = true;
    const resp = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await resp.json();
    sessionId = data.session_id || sessionId;
    localStorage.setItem('session_id', sessionId);
    addBubble(data.final_answer || data.error || 'Нет ответа', 'bot');
    document.getElementById('sources').innerText = data.sources_summary || '';
    document.getElementById('links').innerText = (data.concept_links || []).join('\n');
    document.getElementById('log').innerText = JSON.stringify(data.dataset_log || {}, null, 2);
    sendBtn.disabled = false;
    message.value = '';
    message.focus();
  }

  sendBtn.onclick = send;
  message.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) send(); });
</script>
</body>
</html>
"""


def build_payload(state: SessionState, user_text: str, sources_summary: str, concept_links: List[str], final_answer: str, work_dir: Path) -> Dict[str, object]:
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


def save_iteration(out_dir: Path, payload: Dict[str, object]):
    out_dir.mkdir(parents=True, exist_ok=True)
    train_path = out_dir / "train.jsonl"
    prefs_path = out_dir / "user_prefs.jsonl"
    with train_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload["dataset_log"], ensure_ascii=False) + "\n")
    with prefs_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload["user_prefs"], ensure_ascii=False) + "\n")


def create_app(config):
    app = Flask(__name__)
    pipe = load_model(config.model_dir, config.tokenizer, config.adapter)
    sessions: Dict[str, SessionState] = {}

    @app.route("/")
    def index() -> Response:
        return Response(inline_page(), mimetype="text/html")

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
        save_iteration(Path(config.out_dir) / session_id, payload)

        return jsonify({"session_id": session_id, **payload})

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


def parse_args():
    parser = argparse.ArgumentParser(description="Single-file web chat for GameDev slow RAG")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--model_dir", type=Path, help="Optional base model directory for polishing answers")
    parser.add_argument("--tokenizer", type=Path, help="Tokenizer path (defaults to model_dir)")
    parser.add_argument("--adapter", type=Path, help="Optional LoRA adapter directory")
    parser.add_argument("--out_dir", type=Path, default=Path("sessions_web"))
    parser.add_argument("--emb_model", type=str, default=DEFAULT_EMB_MODEL)
    parser.add_argument("--device", type=str, default="cpu")
    parser.add_argument("--max_new_tokens", type=int, default=512)
    return parser.parse_args()


def main():
    args = parse_args()
    app = create_app(args)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
