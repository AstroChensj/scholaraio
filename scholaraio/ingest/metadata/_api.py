"""API query functions (Crossref, Semantic Scholar, OpenAlex, ADS) and metadata enrichment."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlencode

import requests

from scholaraio.config import Config

from ._extract import _extract_lastname
from ._models import (
    CR_BASE,
    OA_BASE,
    RELAXED_THRESHOLD,
    S2_BASE,
    S2_FIELDS,
    SESSION,
    TIMEOUT,
    TITLE_MATCH_THRESHOLD,
    PaperMetadata,
)

_log = logging.getLogger(__name__)
ADS_BASE = "https://api.adsabs.harvard.edu/v1/search/query"


# ============================================================================
#  API Query Functions
# ============================================================================


def query_semantic_scholar(doi: str = "", title: str = "") -> dict:
    """查询 Semantic Scholar API。

    Args:
        doi: DOI 标识符（优先使用）。
        title: 论文标题（DOI 为空时用于搜索）。

    Returns:
        API 返回的论文数据字典，未找到时返回空字典。
    """
    if doi:
        url = f"{S2_BASE}/DOI:{doi}?fields={S2_FIELDS}"
    elif title:
        params = {"query": title, "limit": "3", "fields": S2_FIELDS}
        url = f"{S2_BASE}/search?{urlencode(params)}"
    else:
        return {}

    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            _log.warning("[S2] Rate limited, waiting %ds", wait)
            time.sleep(min(wait, 30))
            resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()

        # Search endpoint returns list — pick best match
        if "data" in data:
            for item in data.get("data", []):
                if title and _fuzzy_title_match(title, item.get("title", "")) >= TITLE_MATCH_THRESHOLD:
                    return item
            return {}
        return data
    except (requests.RequestException, ValueError, KeyError) as e:
        _log.warning("[S2] %s", e)
        return {}


def query_openalex(doi: str = "", title: str = "") -> dict:
    """查询 OpenAlex API。

    Args:
        doi: DOI 标识符（优先使用）。
        title: 论文标题（DOI 为空时用于搜索）。

    Returns:
        API 返回的论文数据字典，未找到时返回空字典。
    """
    if doi:
        url = f"{OA_BASE}/doi:{doi}"
    elif title:
        keywords = _title_keywords(title, max_words=8)
        params = {
            "filter": f"title.search:{keywords}",
            "per_page": "3",
            "select": "id,doi,title,publication_year,cited_by_count,authorships,primary_location,type,abstract_inverted_index",
        }
        url = f"{OA_BASE}?{urlencode(params)}"
    else:
        return {}

    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()

        # Search endpoint returns list
        if "results" in data:
            for item in data.get("results", []):
                if title and _fuzzy_title_match(title, item.get("title", "")) >= TITLE_MATCH_THRESHOLD:
                    return item
            return {}
        return data
    except (requests.RequestException, ValueError, KeyError) as e:
        _log.warning("[OA] %s", e)
        return {}


def query_crossref(doi: str = "", title: str = "") -> dict:
    """查询 Crossref API（DOI 权威注册中心）。

    Crossref 提供最规范的期刊名、作者列表和论文类型分类。

    Args:
        doi: DOI 标识符（优先使用）。
        title: 论文标题（DOI 为空时用于搜索）。

    Returns:
        API 返回的论文数据字典，未找到时返回空字典。
    """
    if doi:
        url = f"{CR_BASE}/{doi}"
    elif title:
        keywords = _title_keywords(title, max_words=8)
        params = {
            "query.title": keywords,
            "rows": "3",
            "select": "DOI,title,author,container-title,published-print,"
            "published-online,type,is-referenced-by-count,abstract",
        }
        url = f"{CR_BASE}?{urlencode(params)}"
    else:
        return {}


def query_ads(*, bibcode: str = "", doi: str = "", title: str = "", ads_api_token: str = "") -> dict:
    """Query ADS search API by bibcode, DOI, or title.

    Args:
        bibcode: ADS bibcode (highest priority).
        doi: DOI identifier.
        title: Paper title.
        ads_api_token: ADS API token.

    Returns:
        ADS document dict, or empty dict when not found / unavailable.
    """
    if not ads_api_token:
        return {}

    if bibcode:
        query = f'bibcode:"{bibcode}"'
    elif doi:
        query = f'doi:"{doi}"'
    elif title:
        escaped = title.replace('"', '\\"')
        query = f'title:"{escaped}"'
    else:
        return {}

    params = {
        "q": query,
        "rows": "3",
        "fl": "bibcode,title,author,year,doi,abstract,citation_count,pub,doctype",
    }
    headers = {"Authorization": f"Bearer {ads_api_token}"}
    try:
        resp = SESSION.get(ADS_BASE, params=params, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        if doi or bibcode:
            return docs[0] if docs else {}
        for item in docs:
            item_title = _ads_title(item)
            if title and _fuzzy_title_match(title, item_title) >= TITLE_MATCH_THRESHOLD:
                return item
        return {}
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        _log.warning("[ADS] %s", e)
        return {}

    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data = resp.json()

        # DOI lookup returns {"message": {...}}
        if doi:
            return data.get("message", {})

        # Title search returns {"message": {"items": [...]}}
        items = data.get("message", {}).get("items", [])
        for item in items:
            cr_titles = item.get("title", [])
            cr_title = cr_titles[0] if cr_titles else ""
            if title and _fuzzy_title_match(title, cr_title) >= TITLE_MATCH_THRESHOLD:
                return item
        return {}
    except (requests.RequestException, ValueError, KeyError) as e:
        _log.warning("[CR] %s", e)
        return {}


# ============================================================================
#  Helpers
# ============================================================================


def _fuzzy_title_match(a: str, b: str) -> float:
    """Dice coefficient on word sets (case-insensitive, stripped punctuation).

    Dice = 2*|A∩B| / (|A|+|B|).  More forgiving than Jaccard for
    different-length strings (e.g. subtitle included in only one source).
    """

    def words(s: str) -> set[str]:
        return set(re.sub(r"[^\w\s]", "", s.lower()).split())

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return 2 * len(wa & wb) / (len(wa) + len(wb))


def _title_keywords(title: str, max_words: int = 8) -> str:
    """Extract significant keywords from title for API search."""
    stop = {"a", "an", "the", "of", "in", "on", "for", "and", "by", "to", "with", "its", "their"}
    words = re.sub(r"[^\w\s-]", "", title).replace("-", " ").split()
    significant = [w for w in words if w.lower() not in stop]
    return " ".join(significant[:max_words])


def _normalize_lastname(name: str) -> str:
    name = _extract_lastname(name).strip().lower()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", name)


def _is_generic_title(title: str) -> bool:
    words = re.findall(r"\w+", title.lower())
    generic = {
        "study",
        "review",
        "article",
        "dust",
        "active",
        "galactic",
        "nuclei",
        "quasar",
        "quasars",
        "spectral",
        "energy",
        "distribution",
    }
    if len(words) <= 5:
        return True
    non_generic = [w for w in words if w not in generic]
    return len(non_generic) <= 2


def _ads_title(ads_data: dict) -> str:
    title_val = ads_data.get("title") or []
    if isinstance(title_val, list):
        return title_val[0] if title_val else ""
    return title_val or ""


def _candidate_title(ads_data: dict, cr_data: dict, s2_data: dict, oa_data: dict) -> str:
    if ads_data:
        return _ads_title(ads_data)
    if cr_data:
        return ((cr_data.get("title") or [""]) or [""])[0]
    if s2_data:
        return s2_data.get("title", "")
    if oa_data:
        return oa_data.get("title", "")
    return ""


def _candidate_authors(ads_data: dict, cr_data: dict, s2_data: dict, oa_data: dict) -> list[str]:
    if ads_data.get("author"):
        return ads_data.get("author") or []
    if cr_data.get("author"):
        return [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in cr_data["author"]]
    if s2_data.get("authors"):
        return [a.get("name", "") for a in s2_data["authors"]]
    if oa_data.get("authorships"):
        return [a.get("author", {}).get("display_name", "") for a in oa_data["authorships"]]
    return []


def _candidate_year(ads_data: dict, cr_data: dict, s2_data: dict, oa_data: dict) -> int | None:
    if ads_data.get("year"):
        try:
            return int(ads_data["year"])
        except (TypeError, ValueError):
            pass
    for date_key in ("published-print", "published-online"):
        parts = cr_data.get(date_key, {}).get("date-parts", [[]])
        if parts and parts[0] and parts[0][0]:
            return parts[0][0]
    if s2_data.get("year"):
        return s2_data["year"]
    if oa_data.get("publication_year"):
        return oa_data["publication_year"]
    return None


def _candidate_journal(ads_data: dict, cr_data: dict, s2_data: dict, oa_data: dict) -> str:
    if ads_data.get("pub"):
        return ads_data["pub"]
    ct = cr_data.get("container-title", [])
    if ct:
        return ct[0]
    if s2_data.get("venue"):
        return s2_data["venue"]
    loc = oa_data.get("primary_location", {}) or {}
    source = loc.get("source", {}) or {}
    return source.get("display_name", "")


def _trusted_api_match(
    meta: PaperMetadata,
    ads_data: dict,
    cr_data: dict,
    s2_data: dict,
    oa_data: dict,
    *,
    relaxed: bool = False,
    doi_lookup: bool = False,
) -> tuple[bool, str]:
    api_title = _candidate_title(ads_data, cr_data, s2_data, oa_data)
    title_score = _fuzzy_title_match(meta.title, api_title) if (meta.title and api_title) else 1.0
    api_authors = _candidate_authors(ads_data, cr_data, s2_data, oa_data)
    api_year = _candidate_year(ads_data, cr_data, s2_data, oa_data)
    api_journal = _candidate_journal(ads_data, cr_data, s2_data, oa_data)

    local_last = _normalize_lastname(meta.first_author or meta.first_author_lastname)
    author_match = True
    if local_last and api_authors:
        author_match = local_last in {_normalize_lastname(a) for a in api_authors if a}

    year_match = True
    if meta.year and api_year:
        year_match = abs(int(meta.year) - int(api_year)) <= (2 if doi_lookup else 1)

    journal_match = True
    if meta.journal and api_journal:
        journal_match = _fuzzy_title_match(meta.journal, api_journal) >= (0.5 if doi_lookup else 0.6)

    if title_score < (RELAXED_THRESHOLD if doi_lookup else TITLE_MATCH_THRESHOLD):
        return False, f"title mismatch ({title_score:.2f})"
    if relaxed and local_last and api_authors and not author_match:
        return False, "author mismatch in relaxed title search"
    if _is_generic_title(meta.title) and local_last and api_authors and not author_match:
        return False, "generic title without author agreement"
    if meta.year and api_year and not year_match and not author_match:
        return False, "year and author mismatch"
    if meta.journal and api_journal and not journal_match and not author_match and _is_generic_title(meta.title):
        return False, "journal mismatch for generic title"
    return True, ""


# ============================================================================
#  Relaxed Queries (Tier 3)
# ============================================================================


def _query_crossref_relaxed(title: str) -> dict:
    """Crossref title search with relaxed matching."""
    keywords = _title_keywords(title, max_words=8)
    params = {
        "query.title": keywords,
        "rows": "5",
        "select": "DOI,title,author,container-title,published-print,"
        "published-online,type,is-referenced-by-count,abstract",
    }
    url = f"{CR_BASE}?{urlencode(params)}"
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return {}
        items = resp.json().get("message", {}).get("items", [])
        for item in items:
            cr_title = (item.get("title") or [""])[0]
            if _fuzzy_title_match(title, cr_title) >= RELAXED_THRESHOLD:
                return item
        return {}
    except (requests.RequestException, ValueError) as e:
        _log.warning("[CR-relaxed] %s", e)
        return {}


def _query_oa_relaxed(title: str) -> dict:
    """OpenAlex title search with relaxed matching."""
    keywords = _title_keywords(title, max_words=8)
    params = {
        "filter": f"title.search:{keywords}",
        "per_page": "5",
        "select": "id,doi,title,publication_year,cited_by_count,authorships,primary_location,type,abstract_inverted_index",
    }
    url = f"{OA_BASE}?{urlencode(params)}"
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return {}
        for item in resp.json().get("results", []):
            if _fuzzy_title_match(title, item.get("title", "")) >= RELAXED_THRESHOLD:
                return item
        return {}
    except (requests.RequestException, ValueError) as e:
        _log.warning("[OA-relaxed] %s", e)
        return {}


# ============================================================================
#  Enrichment
# ============================================================================


def _reconstruct_oa_abstract(inverted_index: dict) -> str:
    """Reconstruct abstract text from OpenAlex inverted index format.

    Format: {"word": [pos0, pos1, ...], ...} → ordered sentence.
    """
    if not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


def enrich_metadata(meta: PaperMetadata, cfg: Config | None = None) -> PaperMetadata:
    """通过 API 查询补全和覆盖元数据。

    查询策略（多层降级）:
      1. **Tier 1** — DOI 直查（三个 API 并行，无限流）
      2. **Tier 2** — Crossref + OA 标题搜索（严格匹配 ≥0.85）
      3. **Tier 3** — Crossref + OA 放宽搜索（匹配 ≥0.65）
      4. **Tier 4** — S2 标题搜索（最后手段，可能被限流）
      5. **Tier 5** — 本地数据（无 API 结果可用）

    合并优先级: Crossref > Semantic Scholar > OpenAlex > 正则提取。

    Args:
        meta: 已提取的元数据（至少需要 ``title`` 或 ``doi``）。

    Returns:
        同一个 :class:`PaperMetadata` 实例，字段已被 API 数据覆盖/补全。
    """
    ads_data: dict = {}
    cr_data: dict = {}
    s2_data: dict = {}
    oa_data: dict = {}
    ads_api_token = cfg.resolved_ads_api_token() if cfg is not None else ""

    # ---- Tier 1: DOI lookup (all three, DOI queries are not rate-limited) ----
    if meta.doi:
        ads_data = query_ads(doi=meta.doi, ads_api_token=ads_api_token)
        cr_data = query_crossref(doi=meta.doi)
        s2_data = query_semantic_scholar(doi=meta.doi)
        oa_data = query_openalex(doi=meta.doi)
        if ads_data or cr_data or s2_data or oa_data:
            ok, reason = _trusted_api_match(meta, ads_data, cr_data, s2_data, oa_data, doi_lookup=True)
            if not ok:
                _log.debug("DOI lookup rejected: %s", reason)
                meta.doi = ""
                meta.extraction_method = "metadata_conflict"
                ads_data, cr_data, s2_data, oa_data = {}, {}, {}, {}
            else:
                meta.extraction_method = "doi_lookup"

    # ---- Tier 2: Title search via Crossref + OA (no rate limit) ----
    if not ads_data and not cr_data and not s2_data and not oa_data and meta.title:
        _log.debug("No DOI match, trying title search")
        ads_data = query_ads(title=meta.title, ads_api_token=ads_api_token)
        cr_data = query_crossref(title=meta.title)
        oa_data = query_openalex(title=meta.title)

        # If Crossref found a DOI, use it for S2 + OA DOI lookup
        found_doi = ""
        if cr_data and cr_data.get("DOI"):
            found_doi = cr_data["DOI"]
        elif oa_data and oa_data.get("doi"):
            found_doi = oa_data["doi"].replace("https://doi.org/", "")

        if found_doi:
            if not s2_data:
                s2_data = query_semantic_scholar(doi=found_doi)
            if not oa_data:
                oa_data = query_openalex(doi=found_doi)

        if ads_data or cr_data or s2_data or oa_data:
            ok, reason = _trusted_api_match(meta, ads_data, cr_data, s2_data, oa_data)
            if ok:
                meta.extraction_method = "title_search"
            else:
                _log.debug("title search rejected: %s", reason)
                meta.extraction_method = "metadata_conflict"
                ads_data, cr_data, s2_data, oa_data = {}, {}, {}, {}

    # ---- Tier 3: Relaxed title search (Crossref + OA, lower threshold) ----
    if not ads_data and not cr_data and not s2_data and not oa_data and meta.title:
        _log.debug("Strict match failed, trying relaxed search")
        ads_data = query_ads(title=meta.title, ads_api_token=ads_api_token)
        cr_data = _query_crossref_relaxed(meta.title)
        oa_data = _query_oa_relaxed(meta.title)

        # Again, use found DOI for S2
        found_doi = ""
        if cr_data and cr_data.get("DOI"):
            found_doi = cr_data["DOI"]
        elif oa_data and oa_data.get("doi"):
            found_doi = oa_data["doi"].replace("https://doi.org/", "")
        if found_doi and not s2_data:
            s2_data = query_semantic_scholar(doi=found_doi)

        if ads_data or cr_data or s2_data or oa_data:
            ok, reason = _trusted_api_match(meta, ads_data, cr_data, s2_data, oa_data, relaxed=True)
            if ok:
                meta.extraction_method = "title_search_relaxed"
            else:
                _log.debug("relaxed title search rejected: %s", reason)
                meta.extraction_method = "metadata_conflict"
                ads_data, cr_data, s2_data, oa_data = {}, {}, {}, {}

    # ---- Tier 4: S2 title search (last resort, may be rate-limited) ----
    if not ads_data and not cr_data and not s2_data and not oa_data and meta.title:
        _log.debug("Trying S2 title search (last resort)")
        s2_data = query_semantic_scholar(title=meta.title)
        if s2_data:
            ok, reason = _trusted_api_match(meta, {}, {}, s2_data, {}, relaxed=True)
            if not ok:
                _log.debug("S2 title search rejected: %s", reason)
                meta.extraction_method = "metadata_conflict"
                s2_data = {}
            else:
                meta.extraction_method = "title_search_s2"

    # ---- Tier 5: local_only ----
    if not ads_data and not cr_data and not s2_data and not oa_data:
        meta.extraction_method = meta.extraction_method or "local_only"
        return meta

    # ---- Merge strategy: API data OVERRIDES md-extracted data ----
    # Priority for authors/title/year/journal: Crossref > S2 > OA > md fallback
    # For DOI/IDs/citations: always collect from all sources

    # ---- 1. ADS (high priority for bib metadata and citations; especially strong in astronomy) ----
    if ads_data:
        meta.api_sources.append("ads")
        meta.citation_count_ads = ads_data.get("citation_count")
        if ads_data.get("bibcode"):
            meta.ads_bibcode = ads_data["bibcode"]
        doi_vals = ads_data.get("doi") or []
        if not meta.doi:
            if isinstance(doi_vals, list) and doi_vals:
                meta.doi = doi_vals[0]
            elif isinstance(doi_vals, str):
                meta.doi = doi_vals
        if ads_data.get("year"):
            try:
                meta.year = int(ads_data["year"])
            except (TypeError, ValueError):
                pass
        ads_title = _ads_title(ads_data)
        if ads_title:
            meta.title = ads_title
        if ads_data.get("pub"):
            meta.journal = ads_data["pub"]
        if ads_data.get("author"):
            meta.authors = ads_data["author"]
            meta.first_author = meta.authors[0]
            meta.first_author_lastname = _extract_lastname(meta.first_author)
        if ads_data.get("doctype") and not meta.paper_type:
            meta.paper_type = ads_data["doctype"]
        if ads_data.get("abstract") and not meta.abstract:
            meta.abstract = ads_data["abstract"]

    # ---- 2. Crossref (highest quality for journal/authors/type among general metadata sources) ----
    if cr_data:
        meta.api_sources.append("crossref")
        meta.citation_count_crossref = cr_data.get("is-referenced-by-count")
        if cr_data.get("DOI"):
            meta.doi = cr_data["DOI"]
        meta.crossref_doi = cr_data.get("DOI", "")
        # Year: prefer published-print, fallback published-online
        for date_key in ("published-print", "published-online"):
            parts = cr_data.get(date_key, {}).get("date-parts", [[]])
            if parts and parts[0] and parts[0][0]:
                meta.year = parts[0][0]
                break
        # Title — Crossref is authoritative, override md-extracted title
        cr_titles = cr_data.get("title", [])
        if cr_titles and cr_titles[0] and not ads_data:
            meta.title = cr_titles[0]
        # Journal (Crossref has standardized container-title)
        ct = cr_data.get("container-title", [])
        if ct and not ads_data:
            meta.journal = ct[0]
        # Authors — Crossref is the most authoritative (has structured given/family)
        if cr_data.get("author") and not ads_data.get("author"):
            meta.authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in cr_data["author"]]
            meta.first_author = meta.authors[0]
            # Use Crossref's structured family name directly (handles double
            # surnames like García-Villalba, Ouyang, etc. that _extract_lastname misses)
            first_a = cr_data["author"][0]
            meta.first_author_lastname = first_a.get("family", "") or _extract_lastname(meta.first_author)
        # Paper type
        if cr_data.get("type"):
            meta.paper_type = cr_data["type"]
        # Bibliographic details
        if cr_data.get("volume"):
            meta.volume = cr_data["volume"]
        if cr_data.get("issue"):
            meta.issue = cr_data["issue"]
        if cr_data.get("page"):
            meta.pages = cr_data["page"]
        elif cr_data.get("article-number"):
            meta.pages = cr_data["article-number"]
        if cr_data.get("publisher"):
            meta.publisher = cr_data["publisher"]
        if cr_data.get("ISSN"):
            issns = cr_data["ISSN"]
            if isinstance(issns, list) and issns:
                meta.issn = issns[0]
            elif isinstance(issns, str):
                meta.issn = issns

    # ---- 3. Semantic Scholar (best abstract, good authors) ----
    if s2_data:
        meta.api_sources.append("semantic_scholar")
        meta.citation_count_s2 = s2_data.get("citationCount")
        meta.s2_paper_id = s2_data.get("paperId", "")
        if not meta.doi and s2_data.get("externalIds", {}).get("DOI"):
            meta.doi = s2_data["externalIds"]["DOI"]
        # Title: override only if Crossref didn't provide one
        if not ads_data and not cr_data and s2_data.get("title"):
            meta.title = s2_data["title"]
        if not meta.year and s2_data.get("year"):
            meta.year = s2_data["year"]
        if not meta.journal and s2_data.get("venue"):
            meta.journal = s2_data["venue"]
        # Authors: override only if Crossref didn't provide them
        if not ads_data and not cr_data and s2_data.get("authors"):
            meta.authors = [a.get("name", "") for a in s2_data["authors"]]
            if meta.authors:
                meta.first_author = meta.authors[0]
                meta.first_author_lastname = _extract_lastname(meta.first_author)
        # Paper type
        if not meta.paper_type and s2_data.get("publicationTypes"):
            meta.paper_type = s2_data["publicationTypes"][0]
        # Abstract — S2 gives clean plain text (preferred)
        if s2_data.get("abstract"):
            meta.abstract = s2_data["abstract"]
        # References — extract DOIs from S2 references list
        if s2_data.get("references"):
            ref_dois = []
            for ref in s2_data["references"]:
                ext_ids = ref.get("externalIds") or {}
                doi = ext_ids.get("DOI")
                if doi:
                    ref_dois.append(doi)
            if ref_dois:
                meta.references = ref_dois

    # ---- 4. Crossref abstract (strip HTML/JATS tags, collapse whitespace) ----
    if cr_data and not meta.abstract and cr_data.get("abstract"):
        raw = cr_data["abstract"]
        raw = re.sub(r"<[^>]+>", "", raw)  # strip HTML/JATS tags
        raw = re.sub(r"[\n\t\r]+", " ", raw)  # collapse newlines/tabs
        raw = re.sub(r"\s{2,}", " ", raw)  # collapse multiple spaces
        meta.abstract = raw.strip()

    # ---- 5. OpenAlex (fallback for everything) ----
    if oa_data:
        meta.api_sources.append("openalex")
        meta.citation_count_openalex = oa_data.get("cited_by_count")
        meta.openalex_id = oa_data.get("id", "")
        if not meta.doi and oa_data.get("doi"):
            meta.doi = oa_data["doi"].replace("https://doi.org/", "")
        # Title: override only if neither Crossref nor S2 provided one
        if not cr_data and not s2_data and oa_data.get("title"):
            meta.title = oa_data["title"]
        if not meta.year and oa_data.get("publication_year"):
            meta.year = oa_data["publication_year"]
        # Authors: override only if neither Crossref nor S2 provided them
        if not cr_data and not s2_data and oa_data.get("authorships"):
            meta.authors = [a.get("author", {}).get("display_name", "") for a in oa_data["authorships"]]
            if meta.authors:
                meta.first_author = meta.authors[0]
                meta.first_author_lastname = _extract_lastname(meta.first_author)
        # Journal from primary_location
        if not meta.journal:
            loc = oa_data.get("primary_location", {}) or {}
            source = loc.get("source", {}) or {}
            meta.journal = source.get("display_name", "")
        # Paper type
        if not meta.paper_type and oa_data.get("type"):
            meta.paper_type = oa_data["type"]
        # Bibliographic details from OA (fallback)
        biblio = oa_data.get("biblio", {}) or {}
        if not meta.volume and biblio.get("volume"):
            meta.volume = biblio["volume"]
        if not meta.issue and biblio.get("issue"):
            meta.issue = biblio["issue"]
        if not meta.pages:
            fp, lp = biblio.get("first_page", ""), biblio.get("last_page", "")
            if fp:
                meta.pages = f"{fp}-{lp}" if lp and lp != fp else fp
        if not meta.publisher:
            loc = oa_data.get("primary_location", {}) or {}
            source = loc.get("source", {}) or {}
            if source.get("host_organization_name"):
                meta.publisher = source["host_organization_name"]
        if not meta.issn:
            loc = oa_data.get("primary_location", {}) or {}
            source = loc.get("source", {}) or {}
            issns = source.get("issn", [])
            if issns:
                meta.issn = issns[0]
        # Abstract from OA inverted index (last resort)
        if not meta.abstract and oa_data.get("abstract_inverted_index"):
            meta.abstract = _reconstruct_oa_abstract(oa_data["abstract_inverted_index"])

    return meta
