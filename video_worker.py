#!/usr/bin/env python
"""Utility helpers to pre-process video metadata outside the web app."""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from app import (
    VIDEO_ROOT,
    refresh_video_index,
    iter_video_files,
    ensure_preview,
    mark_popular_dirty,
)


def rebuild_index() -> None:
    print("[video-worker] refreshing metadata index...")
    refresh_video_index(force=True)
    mark_popular_dirty()
    print("[video-worker] index refreshed.")


def generate_previews(workers: int) -> None:
    refresh_video_index(force=True)
    video_paths = list(iter_video_files(VIDEO_ROOT))
    total = len(video_paths)
    if not total:
        print("[video-worker] nothing to do — no videos found.")
        return
    workers = max(1, workers)
    print(f"[video-worker] generating previews for {total} videos using {workers} worker(s)...")

    def task(path: str) -> str:
        try:
            ensure_preview(path)
        except Exception as exc:
            print(f"[video-worker] failed to generate preview for {path}: {exc}", file=sys.stderr)
        return path

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for _ in executor.map(task, video_paths):
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f"[video-worker] {completed}/{total} previews ready")
    print("[video-worker] preview generation finished.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Video maintenance helper")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["index", "previews", "all"],
        help=(
            "Task to run: rebuild the metadata index, generate previews, or do both. "
            "Defaults to 'all' when omitted."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of parallel workers for preview generation (default: CPU count).",
    )
    args = parser.parse_args(argv)

    if args.command in ("index", "all"):
        rebuild_index()
    if args.command in ("previews", "all"):
        generate_previews(args.workers)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
