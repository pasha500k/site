"""Utility to normalize raw prompts/replies into train.jsonl and user_prefs.jsonl.
Run: python prepare_dataset.py --raw raw_samples.json --out_dir data
"""

import argparse
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def load_raw(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("raw file must be a list of objects")
    return data


def to_iso(ts: str | None) -> str:
    if ts:
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return ts.replace("+00:00", "Z")
        except ValueError:
            pass
    return datetime.utcnow().strftime(ISO_FORMAT)


def normalize_entry(obj: Dict[str, Any]) -> Dict[str, Any]:
    input_text = str(obj.get("input", "")).strip()
    output_text = str(obj.get("output", "")).strip()
    stage = str(obj.get("stage", "prototype"))
    engine = str(obj.get("engine", "ue5"))
    genre = obj.get("genre", []) or []
    if isinstance(genre, str):
        genre = [genre]
    quality_tags = obj.get("quality_tags", []) or []
    if isinstance(quality_tags, str):
        quality_tags = [quality_tags]
    timestamp = to_iso(obj.get("timestamp"))
    if not input_text or not output_text:
        raise ValueError("input and output must be non-empty")
    return {
        "input": input_text,
        "output": output_text,
        "stage": stage,
        "engine": engine,
        "genre": genre,
        "quality_tags": quality_tags,
        "timestamp": timestamp,
    }


def normalize_user_pref(obj: Dict[str, Any]) -> Dict[str, Any]:
    prefs = dict(obj)
    prefs["timestamp"] = to_iso(prefs.get("timestamp"))
    return prefs


def write_jsonl(path: Path, rows: List[Dict[str, Any]]):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", type=Path, required=True, help="Path to raw JSON list")
    parser.add_argument("--out_dir", type=Path, default=Path("data"))
    parser.add_argument("--prefs", type=Path, help="Optional path to user prefs JSON list")
    args = parser.parse_args()

    raw_entries = load_raw(args.raw)
    normalized = [normalize_entry(e) for e in raw_entries]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out_dir / "train.jsonl", normalized)

    if args.prefs:
        prefs_entries = load_raw(args.prefs)
        prefs_norm = [normalize_user_pref(p) for p in prefs_entries]
        write_jsonl(args.out_dir / "user_prefs.jsonl", prefs_norm)

    print(f"Wrote {len(normalized)} train rows to {args.out_dir / 'train.jsonl'}")
    if args.prefs:
        print(f"Wrote {len(prefs_norm)} prefs rows to {args.out_dir / 'user_prefs.jsonl'}")


if __name__ == "__main__":
    main()
