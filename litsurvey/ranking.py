"""Relevance ranking and thematic clustering (TF-IDF + k-means)."""
from __future__ import annotations

import math
from datetime import date

import numpy as np
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .models import Paper


def _doc(p: Paper) -> str:
    return f"{p.title}. {p.title}. {p.abstract}"  # title weighted twice


def rank(papers: list[Paper], topic: str, queries: list[str], max_papers: int,
         min_relevance: float = 0.05) -> list[Paper]:
    """Score = 0.70 relevance + 0.20 citation impact + 0.10 recency."""
    if not papers:
        return []
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True,
                          min_df=1, max_df=0.9 if len(papers) > 20 else 1.0)
    X = vec.fit_transform([_doc(p) for p in papers])
    Q = vec.transform([" ".join([topic] * 2 + queries)])
    rel = cosine_similarity(X, Q).ravel()
    if rel.max() > 0:
        rel = rel / rel.max()
    cit = np.array([math.log1p(p.citations) for p in papers])
    cit = cit / cit.max() if cit.max() > 0 else cit
    this_year = date.today().year
    rec = np.array([max(0.0, 1 - (this_year - (p.year or this_year - 10)) / 15) for p in papers])
    for p, r, c, y in zip(papers, rel, cit, rec):
        p.relevance = float(r)
        p.score = float(0.70 * r + 0.20 * c + 0.10 * y)
    kept = [p for p in papers if p.relevance >= min_relevance and p.abstract]
    kept.sort(key=lambda p: p.score, reverse=True)
    return kept[:max_papers] if max_papers else kept


def cluster(papers: list[Paper], n_themes: int | None = None, seed: int = 42):
    """Assign p.theme; return list of (theme_id, top_terms)."""
    n = len(papers)
    if n == 0:
        return []
    k = n_themes or max(2, min(10, round(math.sqrt(n / 2))))
    k = max(1, min(k, n))
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True,
                          max_df=0.6 if n > 10 else 1.0, min_df=1)
    X = vec.fit_transform([_doc(p) for p in papers])
    if k == 1:
        labels = np.zeros(n, dtype=int)
        centers = np.asarray(X.mean(axis=0))
    else:
        km = KMeans(n_clusters=k, n_init=10, random_state=seed)
        labels = km.fit_predict(X)
        centers = km.cluster_centers_
    terms = np.array(vec.get_feature_names_out())
    themes = []
    for t in range(k):
        top = terms[np.argsort(centers[t])[::-1][:8]].tolist()
        themes.append((t, top))
    for p, l in zip(papers, labels):
        p.theme = int(l)
    # drop empty clusters, renumber by size (largest first)
    sizes = {t: sum(1 for p in papers if p.theme == t) for t, _ in themes}
    order = [t for t, _ in sorted(themes, key=lambda x: -sizes[x[0]]) if sizes[t] > 0]
    remap = {old: new for new, old in enumerate(order)}
    for p in papers:
        p.theme = remap[p.theme]
    return sorted([(remap[t], top) for t, top in themes if t in remap], key=lambda x: x[0])
