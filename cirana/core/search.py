"""Query matching for the global search (pure Python, unit-tested)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def score(query: str, text: str) -> int:
    """Higher is better, 0 means no match. Prefix > word start > substring > in-order letters."""
    q, t = query.lower().strip(), text.lower()
    if not q:
        return 0
    if t == q:
        return 1000
    if t.startswith(q):
        return 800 - min(len(t) - len(q), 100)
    words = t.replace("-", " ").replace("_", " ").replace(".", " ").split()
    if any(w.startswith(q) for w in words):
        return 600 - min(len(t), 100)
    if q in t:
        return 400 - min(t.index(q), 100)
    pos = 0
    for ch in q:
        pos = t.find(ch, pos)
        if pos < 0:
            return 0
        pos += 1
    return 100 if len(q) >= 3 else 0


@dataclass
class SearchResult:
    kind: str  # "app" | "category" | "workspace"
    key: str
    title: str
    subtitle: str
    score: int
    payload: Any = None


def search(
    query: str,
    apps: list[tuple[str, str, str]],  # (id, name, category display name)
    categories: list[tuple[str, str, int]],  # (id, name, app count)
    workspaces: list[tuple[str, str, int]],  # (id, name, app count)
    recent_ids: list[str],
    limit: int = 40,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    recent_rank = {app_id: i for i, app_id in enumerate(recent_ids)}
    q = query.strip()
    if not q:
        for app_id, name, category in apps:
            if app_id in recent_rank:
                results.append(SearchResult("app", app_id, name, category, 1000 - recent_rank[app_id]))
        return sorted(results, key=lambda r: -r.score)[:limit]
    for app_id, name, category in apps:
        s = max(score(q, name), score(q, category) // 4)
        if s:
            if app_id in recent_rank:
                s += 50 - min(recent_rank[app_id], 40)  # recently used results float up
            results.append(SearchResult("app", app_id, name, category, s))
    for cid, name, count in categories:
        s = score(q, name)
        if s and count:
            results.append(SearchResult("category", cid, name, f"Category · {count} apps", s // 2 + 100))
    for wid, name, count in workspaces:
        s = score(q, name)
        if s:
            results.append(SearchResult("workspace", wid, name, f"Workspace · {count} apps", s))
    results.sort(key=lambda r: (-r.score, r.title.lower()))
    return results[:limit]
