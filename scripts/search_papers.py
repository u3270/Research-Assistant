from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import feedparser
import requests
from dateutil import parser as date_parser


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_FILE = DATA_DIR / "candidates.json"
SEEN_FILE = DATA_DIR / "seen_dois.txt"

LOOKBACK_DAYS = int(os.getenv("PAPER_LOOKBACK_DAYS", "45"))
MAX_CANDIDATES = int(os.getenv("PAPER_MAX_CANDIDATES", "300"))
REQUEST_TIMEOUT = 35

IEEE_API_KEY = os.getenv("IEEE_API_KEY", "").strip()
SEMANTIC_SCHOLAR_API_KEY = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO", "").strip()

USER_AGENT = (
    "DailyResearchPaperAssistant/2.0 "
    f"({CROSSREF_MAILTO or 'personal academic research'})"
)

# Research interests (search queries and Zotero topic tags) live in
# config/topics.json, the single source of truth. Edit that file to change the
# focus; this module derives SEARCH_QUERIES and ARXIV_QUERY from it to drive the
# API searches. Relevance judging, grading, and the final pick are done by Claude
# (see CLAUDE.md) — this module only fetches and de-duplicates candidates.
TOPICS_FILE = ROOT / "config" / "topics.json"


def _generic_query(terms: list[str]) -> str:
    # Quote the primary term as a phrase; append any remaining terms as plain qualifiers.
    return " ".join([f'"{terms[0]}"'] + terms[1:])


def _arxiv_subquery(terms: list[str]) -> str:
    # arXiv: prefix each term with all:, quoting multi-word or hyphenated phrases.
    parts = [
        f'all:"{term}"' if (" " in term or "-" in term) else f"all:{term}"
        for term in terms
    ]
    return " AND ".join(parts)


def load_topics() -> dict[str, Any]:
    try:
        raw = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
        topics = raw.get("topics")
        if not isinstance(topics, list) or not topics:
            raise ValueError("'topics' must be a non-empty list")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FATAL: cannot load topics config {TOPICS_FILE}: {exc}", file=sys.stderr)
        sys.exit(1)

    search_queries: list[str] = []
    arxiv_subqueries: list[str] = []

    for topic in topics:
        for query in topic.get("queries", []):
            terms = [str(t).strip() for t in query if str(t).strip()]
            if not terms:
                continue
            search_queries.append(_generic_query(terms))
            arxiv_subqueries.append(_arxiv_subquery(terms))

    return {
        "search_queries": search_queries,
        "arxiv_query": " OR ".join(arxiv_subqueries),
    }


_TOPICS = load_topics()

# Derived from config/topics.json — do not edit these here; edit that file.
SEARCH_QUERIES = _TOPICS["search_queries"]
ARXIV_QUERY = _TOPICS["arxiv_query"]


@dataclass
class SourceResult:
    name: str
    papers: list[dict[str, Any]]
    error: str | None = None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def normalize_doi(value: str | None) -> str:
    if not value:
        return ""
    doi = clean_text(value).lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.strip().rstrip(".,;)")


def normalize_title(value: str | None) -> str:
    text = clean_text(value).lower()
    return re.sub(r"[^a-z0-9가-힣]+", "", text)


def author_names(authors: Any) -> list[str]:
    result: list[str] = []
    if not authors:
        return result
    if isinstance(authors, str):
        return [clean_text(authors)]
    for author in authors:
        if isinstance(author, str):
            name = clean_text(author)
        elif isinstance(author, dict):
            name = clean_text(
                author.get("full_name")
                or author.get("name")
                or author.get("authorName")
                or ""
            )
        else:
            name = ""
        if name:
            result.append(name)
    return result


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, int):
        try:
            return datetime(value, 1, 1, tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        parsed = date_parser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def iso_date(value: Any) -> str:
    parsed = parse_date(value)
    return parsed.date().isoformat() if parsed else ""


def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    seen: set[str] = set()
    for line in SEEN_FILE.read_text(encoding="utf-8").splitlines():
        value = line.strip().lower()
        if value:
            seen.add(value)
    return seen


def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    merged_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    for attempt in range(5):
        response = requests.get(
            url,
            params=params,
            headers=merged_headers,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            return response.json()

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")

            if retry_after and retry_after.isdigit():
                wait_seconds = int(retry_after)
            else:
                # 2, 4, 8, 16, 32초 순서로 대기
                wait_seconds = 2 ** (attempt + 1)

            print(
                f"Rate limited. Retrying in {wait_seconds} seconds...",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)
            continue

        body = clean_text(response.text)[:300]
        raise RuntimeError(
            f"HTTP {response.status_code}: {body}"
        )

    raise RuntimeError(
        "HTTP 429: request failed after repeated rate-limit retries"
    )


def search_ieee() -> SourceResult:
    if not IEEE_API_KEY:
        return SourceResult("IEEE Xplore", [], "IEEE_API_KEY is not configured")

    papers: list[dict[str, Any]] = []
    try:
        for query in SEARCH_QUERIES:
            payload = request_json(
                "https://ieeexploreapi.ieee.org/api/v1/search/articles",
                params={
                    "apikey": IEEE_API_KEY,
                    "format": "json",
                    "max_records": 12,
                    "start_record": 1,
                    "sort_order": "desc",
                    "sort_field": "publication_year",
                    "querytext": query,
                },
            )
            for article in payload.get("articles", []):
                raw_authors = (
                    article.get("authors", {}).get("authors", [])
                    if isinstance(article.get("authors"), dict)
                    else article.get("authors", [])
                )
                papers.append(
                    {
                        "source": "IEEE Xplore",
                        "title": clean_text(article.get("title")),
                        "abstract": clean_text(article.get("abstract")),
                        "doi": normalize_doi(article.get("doi")),
                        "url": article.get("html_url") or article.get("pdf_url") or "",
                        "authors": author_names(raw_authors),
                        "venue": clean_text(
                            article.get("publication_title")
                            or article.get("publisher")
                        ),
                        "publication_date": iso_date(
                            article.get("publication_date")
                            or article.get("publication_year")
                        ),
                        "publication_year": article.get("publication_year"),
                        "citation_count": article.get("citing_paper_count"),
                        "query": query,
                    }
                )
            time.sleep(0.12)
        return SourceResult("IEEE Xplore", papers)
    except Exception as exc:
        return SourceResult("IEEE Xplore", papers, str(exc))


def search_arxiv() -> SourceResult:
    try:
        response = requests.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": ARXIV_QUERY,
                "start": 0,
                "max_results": 60,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.text)
        papers: list[dict[str, Any]] = []
        for entry in feed.entries:
            arxiv_id = clean_text(entry.get("id", "")).split("/abs/")[-1]
            papers.append(
                {
                    "source": "arXiv",
                    "title": clean_text(entry.get("title")),
                    "abstract": clean_text(entry.get("summary")),
                    "doi": normalize_doi(entry.get("arxiv_doi")),
                    "arxiv_id": arxiv_id,
                    "url": entry.get("link") or entry.get("id") or "",
                    "authors": author_names(entry.get("authors", [])),
                    "venue": "arXiv",
                    "publication_date": iso_date(entry.get("published")),
                    "publication_year": (
                        int(str(entry.get("published", ""))[:4])
                        if str(entry.get("published", ""))[:4].isdigit()
                        else None
                    ),
                    "citation_count": None,
                    "query": ARXIV_QUERY,
                }
            )
        return SourceResult("arXiv", papers)
    except Exception as exc:
        return SourceResult("arXiv", [], str(exc))


def search_semantic_scholar() -> SourceResult:
    papers: list[dict[str, Any]] = []
    headers: dict[str, str] = {}
    if SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = SEMANTIC_SCHOLAR_API_KEY

    try:
        last_request_time = 0.0

        for query in SEARCH_QUERIES[:4]:
            elapsed = time.monotonic() - last_request_time
            minimum_interval = 1.5

            if elapsed < minimum_interval:
                time.sleep(minimum_interval - elapsed)

            payload = request_json(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={
                    "query": query.replace('"', ""),
                    "limit": 10,
                    "fields": (
                        "title,abstract,authors,year,publicationDate,"
                        "venue,externalIds,url,citationCount"
                    ),
                },
                headers=headers,
            )

            last_request_time = time.monotonic()

            for item in payload.get("data", []):
                external_ids = item.get("externalIds") or {}
                papers.append(
                    {
                        "source": "Semantic Scholar",
                        "title": clean_text(item.get("title")),
                        "abstract": clean_text(item.get("abstract")),
                        "doi": normalize_doi(external_ids.get("DOI")),
                        "arxiv_id": clean_text(external_ids.get("ArXiv")),
                        "url": item.get("url") or "",
                        "authors": author_names(item.get("authors", [])),
                        "venue": clean_text(item.get("venue")),
                        "publication_date": iso_date(
                            item.get("publicationDate") or item.get("year")
                        ),
                        "publication_year": item.get("year"),
                        "citation_count": item.get("citationCount"),
                        "query": query,
                    }
                )
            # Anonymous access is more rate-limited.
            time.sleep(1.5) # 너무 빨라서 1.5로 바꿈
            # time.sleep(1/0 if not SEMANTIC_SCHOLAR_API_KEY else 0.25)
        return SourceResult("Semantic Scholar", papers)
    except Exception as exc:
        return SourceResult("Semantic Scholar", papers, str(exc))


def crossref_date(item: dict[str, Any]) -> str:
    for field in ("published-online", "published-print", "issued", "created"):
        value = item.get(field)
        date_parts = (
            value.get("date-parts", [[]])[0]
            if isinstance(value, dict)
            else []
        )
        if date_parts:
            year = date_parts[0]
            month = date_parts[1] if len(date_parts) > 1 else 1
            day = date_parts[2] if len(date_parts) > 2 else 1
            try:
                return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
            except ValueError:
                continue
    return ""


def search_crossref() -> SourceResult:
    papers: list[dict[str, Any]] = []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(LOOKBACK_DAYS, 90))).date()
        for query in SEARCH_QUERIES[:9]:
            params: dict[str, Any] = {
                "query.bibliographic": query.replace('"', ""),
                "rows": 10,
                "sort": "published",
                "order": "desc",
                "filter": f"from-pub-date:{cutoff.isoformat()},type:journal-article",
                "select": (
                    "DOI,title,abstract,author,URL,container-title,"
                    "published-online,published-print,issued,created,"
                    "is-referenced-by-count"
                ),
            }
            if CROSSREF_MAILTO:
                params["mailto"] = CROSSREF_MAILTO
            payload = request_json("https://api.crossref.org/works", params=params)
            for item in payload.get("message", {}).get("items", []):
                title_values = item.get("title") or []
                venue_values = item.get("container-title") or []
                authors = []
                for author in item.get("author") or []:
                    name = " ".join(
                        part for part in [
                            clean_text(author.get("given")),
                            clean_text(author.get("family")),
                        ] if part
                    )
                    if name:
                        authors.append(name)
                papers.append(
                    {
                        "source": "Crossref",
                        "title": clean_text(title_values[0] if title_values else ""),
                        "abstract": clean_text(item.get("abstract")),
                        "doi": normalize_doi(item.get("DOI")),
                        "url": item.get("URL") or "",
                        "authors": authors,
                        "venue": clean_text(venue_values[0] if venue_values else ""),
                        "publication_date": crossref_date(item),
                        "publication_year": (
                            int(crossref_date(item)[:4])
                            if crossref_date(item)[:4].isdigit()
                            else None
                        ),
                        "citation_count": item.get("is-referenced-by-count"),
                        "query": query,
                    }
                )
            time.sleep(0.15)
        return SourceResult("Crossref", papers)
    except Exception as exc:
        return SourceResult("Crossref", papers, str(exc))


def paper_identity(paper: dict[str, Any]) -> str:
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    arxiv_id = clean_text(paper.get("arxiv_id")).lower()
    if arxiv_id:
        return f"arxiv:{arxiv_id.split('v')[0]}"
    return f"title:{normalize_title(paper.get('title'))}"


def merge_record(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    source_set = set(target.get("sources", [target.get("source", "")]))
    source_set.add(incoming.get("source", ""))
    target["sources"] = sorted(source for source in source_set if source)

    for field in ("abstract", "doi", "arxiv_id", "url", "venue", "publication_date"):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]

    if len(incoming.get("abstract", "")) > len(target.get("abstract", "")):
        target["abstract"] = incoming["abstract"]

    target_authors = target.get("authors", [])
    incoming_authors = incoming.get("authors", [])
    if len(incoming_authors) > len(target_authors):
        target["authors"] = incoming_authors

    target_citations = target.get("citation_count")
    incoming_citations = incoming.get("citation_count")
    if incoming_citations is not None and (
        target_citations is None or incoming_citations > target_citations
    ):
        target["citation_count"] = incoming_citations


def deduplicate(papers: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for paper in papers:
        if not paper.get("title"):
            continue
        key = paper_identity(paper)
        if key not in unique:
            record = dict(paper)
            record["sources"] = [paper.get("source", "")]
            unique[key] = record
        else:
            merge_record(unique[key], paper)
    return list(unique.values())


def within_lookback(paper: dict[str, Any], cutoff: datetime) -> bool:
    date = parse_date(paper.get("publication_date"))
    return bool(date and date >= cutoff)


def select_candidates(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop papers already in the library, then return every remaining candidate
    (most recent first). Relevance judging, A/B/C grading, and the final pick are
    done by Claude — this no longer scores or ranks by keyword relevance."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen = load_seen()
    candidates: list[dict[str, Any]] = []

    for paper in papers:
        identity = paper_identity(paper)
        doi_key = normalize_doi(paper.get("doi"))
        url_key = f"url:{str(paper.get('url', '')).strip().lower()}"
        if identity in seen or doi_key in seen or url_key in seen:
            continue
        paper["within_lookback"] = within_lookback(paper, cutoff)
        candidates.append(paper)

    # Most recent first; papers with no parseable date sink to the bottom.
    candidates.sort(
        key=lambda paper: (
            paper.get("within_lookback", False),
            paper.get("publication_date") or "",
            paper.get("citation_count") or 0,
        ),
        reverse=True,
    )

    # Safety bound so a huge pool can't blow up the prompt; the pool is ~250.
    return candidates[:MAX_CANDIDATES]


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    results = [
        search_ieee(),
        search_arxiv(),
        search_semantic_scholar(),
        search_crossref(),
    ]

    all_papers: list[dict[str, Any]] = []
    source_status: dict[str, Any] = {}

    for result in results:
        all_papers.extend(result.papers)
        source_status[result.name] = {
            "retrieved": len(result.papers),
            "status": "ok" if result.error is None else "partial/error",
            "error": result.error,
        }
        if result.error:
            print(f"{result.name} warning: {result.error}", file=sys.stderr)

    unique_papers = deduplicate(all_papers)
    candidates = select_candidates(unique_papers)

    output = {
        "schema_version": 3,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "ranking": "none - candidates are unranked; Claude evaluates relevance",
        "candidate_count": len(candidates),
        "source_status": source_status,
        "papers": candidates,
        "acknowledgements": [
            "Thank you to arXiv for use of its open access interoperability."
        ],
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Retrieved {len(all_papers)} records from all sources.")
    print(f"Deduplicated to {len(unique_papers)} records.")
    print(f"Saved {len(candidates)} candidates (unranked) to {OUTPUT_FILE}.")
    for source, status in source_status.items():
        print(f"- {source}: {status['retrieved']} records ({status['status']})")
    return 0 if candidates else 2


if __name__ == "__main__":
    raise SystemExit(main())