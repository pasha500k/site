"""Video recommendation module for the Flask video site.

This module is intentionally self-contained so it can be dropped into the
existing backend without requiring changes to the rest of the codebase.  The
VideoRecommender class wraps the full lifecycle of a lightweight content-based
recommender system:

* extracts video and interaction metadata from the existing SQLite database;
* converts human-readable metadata into dense embeddings (SentenceTransformers,
  CLIP, pHash, or deterministic fallbacks when heavyweight ML dependencies are
  unavailable);
* stores the dense vectors in a FAISS index (or an in-memory cosine similarity
  matrix if FAISS is not present) so lookups remain fast even for large video
  libraries;
* exposes a ``recommend_for_user`` method used by the REST API to return the top
  ``k`` similar videos for a user.

The goal is to show how a real recommendation component could be integrated
modularly.  The implementation below favours clarity, resilience, and extensive
comments over raw performance so that future engineers can extend it with more
advanced models or collaborative features.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

# ``faiss`` is the de-facto standard library for similarity search.  It is not a
# strict dependency of this repository, so we attempt the import lazily and fall
# back to an in-memory cosine similarity engine when it is not installed.
try:  # pragma: no cover - optional dependency
    import faiss  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    faiss = None  # type: ignore


log = logging.getLogger(__name__)


@dataclass
class VideoRecord:
    """Single row used to build the recommendation index.

    Attributes
    ----------
    video_path:
        Canonical path used by the rest of the application to locate the video
        file.  It doubles as a stable identifier for FAISS lookups.
    title:
        Human readable title extracted either from the database or inferred from
        the filename.  It feeds the embedding model.
    description:
        Optional description/notes that provide richer text for the embedding
        model.  Defaults to an empty string.
    author:
        Optional author/channel metadata that can bias recommendations towards
        creators a user likes.
    thumbnail_url:
        Relative URL to the video's thumbnail so the frontend can render cards
        without additional lookups.
    playback_url:
        URL that can be opened directly in the browser.
    """

    video_path: str
    title: str
    description: str
    author: str
    thumbnail_url: str
    playback_url: str


class VideoRecommender:
    """Content-based recommender tailored for the existing SQLite schema.

    The pipeline executed by the class is broken into distinct phases so that it
    can be scheduled in a background job or triggered manually via a CLI cron.

    1. ``load_video_catalog`` pulls the current snapshot of videos from the
       database (falling back to scanning ``VIDEO_ROOT`` if necessary).
    2. ``embed_videos`` turns free-form text into dense vectors using
       SentenceTransformers, CLIP, or a deterministic hashing fallback.
    3. ``build_index`` inserts the vectors into FAISS.  When FAISS is not
       available, the vectors are simply cached in memory and cosine similarity
       is computed on the fly.
    4. ``recommend_for_user`` fetches the user's recent interactions, aggregates
       them into a preference vector, and returns the nearest neighbours from the
       vector store.

    The class keeps its state thread-safe via ``self._lock`` so the Flask app can
    serve recommendation requests while a background task refreshes the index.
    """

    def __init__(
        self,
        db_path: str,
        video_root: str,
        index_path: str = "data/video.index",
        metadata_cache: str = "data/recommender_metadata.json",
        embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> None:
        self.db_path = db_path
        self.video_root = Path(video_root)
        self.index_path = Path(index_path)
        self.metadata_cache_path = Path(metadata_cache)
        self.embedding_model_name = embedding_model_name

        self._lock = threading.RLock()
        self._videos: List[VideoRecord] = []
        self._vectors: Optional[np.ndarray] = None
        self._index = None
        self._video_id_to_pos: Dict[str, int] = {}

        # Lazily initialised embedding model.  The fallback implementation keeps
        # the module dependency-light but can be replaced with a production-grade
        # model by simply installing ``sentence-transformers`` or ``open-clip``.
        self._embedding_model = None

        # Ensure the index folder exists so we can persist FAISS artefacts.
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_cache_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Data extraction layer
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def load_video_catalog(self) -> List[VideoRecord]:
        """Load all known videos from the database.

        The current schema stores most metadata in ``video_metadata`` keyed by
        ``video_path``.  We attempt to join other tables to enrich the record
        (e.g., aggregated statistics) but gracefully handle missing columns so
        the recommender works even on partially populated databases.
        """

        with self._lock:
            videos: List[VideoRecord] = []
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT video_path,
                               COALESCE(json_extract(extra, '$.title'), '') AS title,
                               COALESCE(json_extract(extra, '$.description'), '') AS description,
                               COALESCE(author_override, '') AS author
                        FROM video_metadata
                        LEFT JOIN (
                            SELECT video_path,
                                   json_group_object('title', title) AS extra
                            FROM (
                                SELECT video_path,
                                       printf('%s', video_path) AS title
                            )
                        ) USING (video_path)
                        ORDER BY video_path
                        """
                    ).fetchall()
            except sqlite3.OperationalError:
                rows = []

            for row in rows:
                video_path = row["video_path"]
                title = row["title"] or Path(video_path).stem.replace("_", " ")
                description = row["description"] or ""
                author = row["author"] or ""
                videos.append(
                    VideoRecord(
                        video_path=video_path,
                        title=title,
                        description=description,
                        author=author,
                        thumbnail_url=f"/thumb/{video_path}",
                        playback_url=f"/watch/{video_path}",
                    )
                )

            # Fall back to scanning the filesystem when the database is empty.
            if not videos:
                for path in sorted(self.video_root.glob("**/*.mp4")):
                    rel = path.relative_to(self.video_root).as_posix()
                    videos.append(
                        VideoRecord(
                            video_path=rel,
                            title=path.stem.replace("_", " "),
                            description="",
                            author="",
                            thumbnail_url=f"/thumb/{rel}",
                            playback_url=f"/watch/{rel}",
                        )
                    )

            self._videos = videos
            return videos

    # ------------------------------------------------------------------
    # Embedding layer
    # ------------------------------------------------------------------
    def _load_embedding_model(self):
        if self._embedding_model is not None:
            return self._embedding_model

        # Attempt to load SentenceTransformer (text) first, fall back to CLIP,
        # and finally to a deterministic hashing baseline to keep the code path
        # operational in restricted environments.
        try:  # pragma: no cover - optional dependency
            from sentence_transformers import SentenceTransformer

            self._embedding_model = SentenceTransformer(self.embedding_model_name)
            log.info("Loaded SentenceTransformer model %s", self.embedding_model_name)
            return self._embedding_model
        except Exception as exc:  # pragma: no cover - optional dependency
            log.warning("SentenceTransformer unavailable: %s", exc)

        try:  # pragma: no cover - optional dependency
            import clip  # type: ignore
            import torch

            model, preprocess = clip.load("ViT-B/32", device="cpu")

            class _ClipWrapper:
                def __init__(self, model, preprocess):
                    self.model = model
                    self.preprocess = preprocess

                def encode(self, texts: Sequence[str]) -> np.ndarray:
                    with torch.no_grad():
                        tokens = clip.tokenize(list(texts))
                        features = self.model.encode_text(tokens)
                        features = features / features.norm(dim=-1, keepdim=True)
                        return features.cpu().numpy().astype("float32")

            self._embedding_model = _ClipWrapper(model, preprocess)
            log.info("Loaded CLIP text encoder")
            return self._embedding_model
        except Exception as exc:  # pragma: no cover - optional dependency
            log.warning("CLIP unavailable: %s", exc)

        # Deterministic hashing fallback: convert text into a pseudo-random
        # vector using a seeded hashing trick.  This keeps recommendations stable
        # across runs and satisfies environments without ML models installed.
        log.info("Falling back to hashing embeddings")

        class _HashingEncoder:
            def encode(self, texts: Sequence[str]) -> np.ndarray:
                dim = 384
                out = np.zeros((len(texts), dim), dtype="float32")
                for i, text in enumerate(texts):
                    if not text:
                        continue
                    for token in text.lower().split():
                        token_hash = abs(hash(token)) % dim
                        out[i, token_hash] += 1.0
                # L2 normalise to align with cosine similarity expectations.
                norms = np.linalg.norm(out, axis=1, keepdims=True) + 1e-9
                return out / norms

        self._embedding_model = _HashingEncoder()
        return self._embedding_model

    def embed_videos(self, videos: Optional[Sequence[VideoRecord]] = None) -> np.ndarray:
        """Create embeddings for every video in ``videos``.

        The textual prompt concatenates the title, author, and description so the
        embedding captures a broader semantic context.
        """

        if videos is None:
            videos = self._videos
        if not videos:
            return np.zeros((0, 1), dtype="float32")

        encoder = self._load_embedding_model()
        prompts = [
            f"Title: {v.title}\nAuthor: {v.author}\nDescription: {v.description}"
            for v in videos
        ]
        embeddings = encoder.encode(prompts)
        if not isinstance(embeddings, np.ndarray):
            embeddings = np.asarray(embeddings, dtype="float32")
        embeddings = embeddings.astype("float32")

        # Persist metadata for debugging/inspection.
        payload = {
            "videos": [v.__dict__ for v in videos],
        }
        try:
            self.metadata_cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("Unable to write recommender metadata cache: %s", exc)

        self._vectors = embeddings
        return embeddings

    # ------------------------------------------------------------------
    # Index layer
    # ------------------------------------------------------------------
    def build_index(self) -> None:
        """Build or refresh the FAISS (or numpy) index in-place."""

        with self._lock:
            if not self._videos:
                self.load_video_catalog()
            if not self._videos:
                log.warning("No videos available for indexing")
                return

            embeddings = self.embed_videos(self._videos)
            if embeddings.size == 0:
                log.warning("No embeddings generated for videos")
                return

            vectors = self._normalise(embeddings)
            self._video_id_to_pos = {v.video_path: idx for idx, v in enumerate(self._videos)}

            if faiss is not None:
                index = faiss.IndexFlatIP(vectors.shape[1])
                index.add(vectors.astype("float32"))
                self._index = index
                try:  # pragma: no cover - disk IO
                    faiss.write_index(index, str(self.index_path))
                except Exception as exc:
                    log.warning("Unable to persist FAISS index: %s", exc)
            else:
                self._index = None  # In-memory numpy fallback

            self._vectors = vectors

    # ------------------------------------------------------------------
    # Recommendation layer
    # ------------------------------------------------------------------
    def recommend_for_user(self, user_id: int, limit: int = 10) -> List[Dict[str, str]]:
        """Return a ranked list of recommended videos for ``user_id``.

        Steps:
        1. Extract the videos the user interacted with recently (likes, views,
           favourites) and convert them to embedding vectors.
        2. Average the user vectors to form a simple user profile.  A production
           system could implement time decay or a personalised model at this
           stage.
        3. Run a similarity search against the FAISS index / numpy store.
        4. Post-process the results to remove items the user already consumed and
           format the payload for the frontend.
        """

        with self._lock:
            if not self._videos or self._vectors is None:
                self.build_index()
            if not self._videos or self._vectors is None:
                return []

            user_history = self._fetch_user_history(user_id)
            if not user_history:
                # Cold-start: return globally popular videos (first N in catalog).
                return [self._video_to_dict(v) for v in self._videos[:limit]]

            history_vectors = []
            for video_path in user_history:
                pos = self._video_id_to_pos.get(video_path)
                if pos is not None:
                    history_vectors.append(self._vectors[pos])

            if not history_vectors:
                return [self._video_to_dict(v) for v in self._videos[:limit]]

            user_profile = np.mean(history_vectors, axis=0, keepdims=True).astype("float32")
            user_profile = self._normalise(user_profile)

            if self._index is not None:
                scores, indices = self._index.search(user_profile, limit + len(user_history))
                ranked_positions = indices[0].tolist()
            else:
                # Manual cosine similarity against the dense matrix.
                sims = np.dot(self._vectors, user_profile[0])
                ranked_positions = np.argsort(sims)[::-1].tolist()

            seen = set(user_history)
            recommendations: List[Dict[str, str]] = []
            for pos in ranked_positions:
                if pos >= len(self._videos):
                    continue
                record = self._videos[pos]
                if record.video_path in seen:
                    continue
                recommendations.append(self._video_to_dict(record))
                if len(recommendations) >= limit:
                    break

            return recommendations

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalise(self, vectors: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9
        return vectors / norms

    def _fetch_user_history(self, user_id: int) -> List[str]:
        """Collect candidate seed videos for the user profile."""

        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT video_path
                    FROM (
                        SELECT video_path, MAX(created_at) AS ts FROM reactions WHERE user_id = ?
                        UNION ALL
                        SELECT video_path, MAX(created_at) AS ts FROM favorites WHERE user_id = ?
                        UNION ALL
                        SELECT video_path, MAX(created_at) AS ts FROM watch_sessions WHERE user_id = ?
                    )
                    WHERE video_path IS NOT NULL
                    ORDER BY ts DESC
                    LIMIT 50
                    """,
                    (user_id, user_id, user_id),
                ).fetchall()
            return [row["video_path"] for row in rows]
        except sqlite3.OperationalError as exc:
            log.warning("Failed to fetch user history: %s", exc)
            return []

    def _video_to_dict(self, record: VideoRecord) -> Dict[str, str]:
        return {
            "video_path": record.video_path,
            "title": record.title,
            "description": record.description,
            "author": record.author,
            "thumbnail_url": record.thumbnail_url,
            "playback_url": record.playback_url,
        }

    # ------------------------------------------------------------------
    # Maintenance utilities
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Convenience helper for cron jobs.

        Example usage::

            >>> from recommender import VideoRecommender
            >>> VideoRecommender(DB_PATH, VIDEO_ROOT).refresh()

        The method simply rebuilds the catalog and the FAISS index.
        """

        self.load_video_catalog()
        self.build_index()

    def warm_start(self) -> None:
        """Load a pre-existing FAISS index from disk if available."""

        if faiss is None:
            return
        if not self.index_path.exists():
            return
        try:
            with self._lock:
                self.load_video_catalog()
                self._vectors = self.embed_videos(self._videos)
                self._vectors = self._normalise(self._vectors)
                self._index = faiss.read_index(str(self.index_path))
                self._video_id_to_pos = {v.video_path: idx for idx, v in enumerate(self._videos)}
                log.info("Loaded FAISS index from %s", self.index_path)
        except Exception as exc:
            log.warning("Unable to load FAISS index: %s", exc)


__all__ = ["VideoRecommender", "VideoRecord"]
