import React, { useEffect, useState } from "react";

/**
 * RecommendedVideos
 * -----------------
 * Minimal React component that displays personalised video recommendations.
 *
 * Props
 * -----
 * - userId: numeric identifier of the signed-in user.
 * - limit: optional number of items to fetch (defaults to 6).
 *
 * The component talks directly to the Flask endpoint introduced in
 * ``app.py`` (``/api/recommendations/<user_id>``).  It is intentionally
 * framework-agnostic: drop it into an existing React tree, or convert it to a
 * Vue/Svelte/Web Components equivalent by reusing the same fetch + render logic.
 */
export default function RecommendedVideos({ userId, limit = 6 }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!userId) {
      setItems([]);
      return;
    }

    let isMounted = true;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`/api/recommendations/${userId}?limit=${limit}`);
        if (!response.ok) {
          throw new Error(`Failed to load recommendations: ${response.status}`);
        }
        const payload = await response.json();
        if (isMounted) {
          setItems(payload.items || []);
        }
      } catch (err) {
        if (isMounted) {
          setError(err.message);
          setItems([]);
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    }

    load();
    return () => {
      isMounted = false;
    };
  }, [userId, limit]);

  if (!userId) {
    return <div className="recommended-videos">Sign in to see personalised picks.</div>;
  }

  if (loading) {
    return <div className="recommended-videos">Loading recommendations…</div>;
  }

  if (error) {
    return (
      <div className="recommended-videos recommended-videos--error">
        Could not load recommendations: {error}
      </div>
    );
  }

  if (!items.length) {
    return <div className="recommended-videos">No recommendations yet.</div>;
  }

  return (
    <div className="recommended-videos">
      <h2 className="recommended-videos__title">Recommended for you</h2>
      <div className="recommended-videos__grid">
        {items.map((item) => (
          <article key={item.video_path} className="recommended-videos__card">
            <a href={item.playback_url} className="recommended-videos__thumb">
              <img src={item.thumbnail_url} alt={item.title} loading="lazy" />
            </a>
            <div className="recommended-videos__meta">
              <h3 className="recommended-videos__name">
                <a href={item.playback_url}>{item.title}</a>
              </h3>
              {item.author && (
                <p className="recommended-videos__author">{item.author}</p>
              )}
              {item.description && (
                <p className="recommended-videos__description">{item.description}</p>
              )}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
