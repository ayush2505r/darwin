"""DuckDuckGo-backed educational resource search."""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 200
EDUCATIONAL_DOMAIN_HINTS = (
    ".edu", ".gov", ".org", "khanacademy.org", "britannica.com", "nasa.gov",
    "smithsonian", "pbslearningmedia.org", "oercommons.org", "mit.edu",
    "stanford.edu", "harvard.edu", "coursera.org", "edutopia.org",
)


def _load_ddgs():
    """Prefer the current package name and retain compatibility with older installs."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS
        return DDGS


def _clean_result(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    url = str(item.get("href") or item.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    title = str(item.get("title") or "Untitled resource").strip()
    body = str(item.get("body") or item.get("description") or "").strip()
    host = parsed.netloc.lower().removeprefix("www.")
    return {"title": title[:240], "url": url, "body": body[:600], "source": host}


def _resource_score(result: Dict[str, str], topic: str) -> int:
    text = f"{result['title']} {result['body']} {result['source']}".lower()
    topic_words = {word for word in topic.lower().split() if len(word) > 2}
    score = sum(2 for word in topic_words if word in text)
    if any(hint in result["source"] for hint in EDUCATIONAL_DOMAIN_HINTS):
        score += 6
    if any(word in text for word in ("lesson", "classroom", "teaching", "explained", "education", "activity")):
        score += 3
    return score


def search_educational_resources(
    topic: str,
    student_level: str = "",
    student_context: str = "",
    max_results: int = 8,
) -> List[Dict[str, str]]:
    """Search DuckDuckGo and return ranked, deduplicated classroom resources."""
    cleaned_topic = " ".join((topic or "").split())[:MAX_QUERY_LENGTH]
    if not cleaned_topic:
        return []
    level = " ".join((student_level or "").split())[:40]
    context = " ".join((student_context or "").split())[:600]
    query = f"{cleaned_topic} {level} teaching classroom educational resources".strip()
    if context:
        query += f" student problems {context}"
    DDGS = _load_ddgs()
    try:
        raw_results = DDGS(timeout=10).text(
            query, region="wt-wt", safesearch="moderate",
            backend="duckduckgo", max_results=max_results * 3,
        )
    except TypeError:
        # Older duckduckgo_search releases do not expose the backend keyword.
        raw_results = DDGS(timeout=10).text(
            query, region="wt-wt", safesearch="moderate", max_results=max_results * 3,
        )

    results: List[Dict[str, str]] = []
    seen_urls = set()
    for item in raw_results or []:
        result = _clean_result(item)
        if not result or result["url"] in seen_urls:
            continue
        seen_urls.add(result["url"])
        result["score"] = str(_resource_score(result, cleaned_topic))
        results.append(result)

    results.sort(key=lambda item: int(item["score"]), reverse=True)
    return results[:max(1, min(max_results, 12))]


def search_youtube_content(
    topic: str,
    student_level: str = "",
    student_context: str = "",
    max_results: int = 6,
) -> List[Dict[str, str]]:
    """Find YouTube teaching videos through DuckDuckGo's video search."""
    cleaned_topic = " ".join((topic or "").split())[:MAX_QUERY_LENGTH]
    if not cleaned_topic:
        return []
    level = " ".join((student_level or "").split())[:40]
    context = " ".join((student_context or "").split())[:400]
    query = f"{cleaned_topic} {level} educational lesson tutorial".strip()
    if context:
        query += f" student difficulties {context}"

    DDGS = _load_ddgs()
    try:
        raw_results = DDGS(timeout=10).videos(
            query, region="wt-wt", safesearch="moderate",
            backend="duckduckgo", max_results=max_results * 3,
        )
    except TypeError:
        raw_results = DDGS(timeout=10).videos(
            query, region="wt-wt", safesearch="moderate", max_results=max_results * 3,
        )

    videos: List[Dict[str, str]] = []
    seen_urls = set()
    topic_words = {word for word in cleaned_topic.lower().split() if len(word) > 2}
    for item in raw_results or []:
        url = str(item.get("content") or item.get("url") or item.get("href") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or "youtube.com" not in parsed.netloc.lower():
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = str(item.get("title") or "YouTube teaching video").strip()
        description = str(item.get("description") or item.get("body") or "").strip()
        searchable = f"{title} {description}".lower()
        score = sum(2 for word in topic_words if word in searchable)
        score += sum(2 for word in ("lesson", "tutorial", "explained", "classroom", "education") if word in searchable)
        videos.append({
            "title": title[:240],
            "url": url,
            "body": description[:500],
            "duration": str(item.get("duration") or ""),
            "source": "youtube.com",
            "score": str(score),
        })

    videos.sort(key=lambda item: int(item["score"]), reverse=True)
    return videos[:max(1, min(max_results, 10))]


def search_resources_safely(
    topic: str,
    student_level: str = "",
    student_context: str = "",
) -> Dict[str, Any]:
    """Return a UI-safe result object without allowing search errors to break the app."""
    try:
        resources = search_educational_resources(topic, student_level, student_context)
    except ImportError:
        return {"results": [], "videos": [], "error": "Resource search is not installed. Install the ddgs package from requirements.txt."}
    except Exception as exc:
        logger.warning("DuckDuckGo resource search failed: %s", exc)
        return {"results": [], "videos": [], "error": "DuckDuckGo could not complete the search right now. Please try again."}

    try:
        videos = search_youtube_content(topic, student_level, student_context)
    except Exception as exc:
        logger.warning("DuckDuckGo YouTube search failed: %s", exc)
        videos = []

    return {"results": resources, "videos": videos, "error": None}
