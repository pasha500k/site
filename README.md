# Flask Video Site

This repository contains a self-hosted Flask application for browsing and
streaming a personal video library.  The app ships with authentication, viewing
history, likes/favourites, search, and now a modular recommendation subsystem.

## Recommendation system integration

The recommendation pipeline lives in [`recommender.py`](./recommender.py).  It is
completely decoupled from the rest of the application so it can be iterated on
independently.  The high-level architecture is:

1. **Data extraction** – pull video metadata and user interactions from the
   existing SQLite database (`video_metadata`, `reactions`, `favorites`,
   `watch_sessions`).
2. **Embedding** – encode each video into a dense vector using
   SentenceTransformers/CLIP when available, or a deterministic hashing fallback
   when the ML models are not installed.
3. **Vector store** – persist the vectors in a FAISS index (if the dependency is
   present) or an in-memory cosine similarity matrix.
4. **Serving** – expose a REST endpoint that aggregates a user's history and
   returns the nearest neighbours from the vector store.

### Python usage

```python
from recommender import VideoRecommender

recommender = VideoRecommender(
    db_path="/path/to/app.db",
    video_root="/path/to/video/files",
    index_path="/path/to/data/video.faiss",
    metadata_cache="/path/to/data/videos.json",
)
recommender.refresh()  # build embeddings + FAISS

recommendations = recommender.recommend_for_user(user_id=42, limit=12)
for item in recommendations:
    print(item["title"], item["playback_url"])
```

### REST endpoint

The Flask app exposes `GET /api/recommendations/<user_id>?limit=<n>` which
returns:

```json
{
  "user_id": 42,
  "count": 3,
  "items": [
    {
      "video_path": "trailers/trailer.mp4",
      "title": "Trailer",
      "thumbnail_url": "/thumb/trailers/trailer.mp4",
      "playback_url": "/watch/trailers/trailer.mp4",
      "author": "Studio",
      "description": "Sci-fi adventure"
    }
  ]
}
```

Test with curl:

```bash
curl "http://localhost:5000/api/recommendations/42?limit=6"
```

### Frontend example

A drop-in React component is provided in
[`frontend/RecommendedVideos.jsx`](./frontend/RecommendedVideos.jsx).  Mount it
anywhere in the existing UI:

```jsx
import RecommendedVideos from "./RecommendedVideos";

export default function Sidebar({ user }) {
  return (
    <aside>
      <RecommendedVideos userId={user?.id} limit={6} />
    </aside>
  );
}
```

Include lightweight styles (optional):

```css
.recommended-videos__grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
}

.recommended-videos__card {
  border-radius: 8px;
  overflow: hidden;
  background: #111;
  color: #fff;
}
```

### Pipeline maintenance

Run the refresh flow periodically (cron, Celery, etc.) to keep embeddings up to
date after new uploads:

```bash
python - <<'PY'
from recommender import VideoRecommender
from app import DATABASE_PATH, VIDEO_ROOT

recommender = VideoRecommender(
    db_path=DATABASE_PATH,
    video_root=VIDEO_ROOT,
    index_path="/data/video.faiss",
    metadata_cache="/data/videos.json",
)
recommender.refresh()
PY
```

## Local development

1. Install dependencies: `pip install -r requirements.txt` (or run in a virtual
   environment).
2. Export `VIDEO_ROOT` pointing to your media library (defaults to the example
   path in `app.py`).
3. Start the app: `FLASK_APP=app.py flask run`.
4. Visit `http://localhost:5000` and sign in with the admin credentials from the
   environment variables.
