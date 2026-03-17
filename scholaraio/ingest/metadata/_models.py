"""Data structures and constants shared across metadata sub-modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import requests

# ============================================================================
#  Data Structures
# ============================================================================


@dataclass
class PaperMetadata:
    """一篇学术论文的完整元数据。

    Attributes:
        id: UUID，入库时生成，永不改变。
        title: 论文标题。
        authors: 作者列表。
        first_author: 第一作者全名。
        first_author_lastname: 第一作者姓氏（用于生成文件名）。
        year: 发表年份。
        doi: DOI 标识符（不含 ``https://doi.org/`` 前缀）。
        journal: 期刊或会议名称。
        abstract: 摘要文本。
        paper_type: 论文类型（article, review, conference-paper 等）。
        citation_count_s2: Semantic Scholar 引用数。
        citation_count_openalex: OpenAlex 引用数。
        citation_count_crossref: Crossref 引用数。
        citation_count_ads: ADS 引用数。
        s2_paper_id: Semantic Scholar 论文 ID。
        openalex_id: OpenAlex 论文 ID。
        crossref_doi: Crossref 返回的 DOI。
        ads_bibcode: ADS bibcode。
        api_sources: 成功返回数据的 API 列表。
        references: 参考文献 DOI 列表（从 Semantic Scholar 获取）。
        source_file: 原始文件名。
        extraction_method: 提取方式（``doi_lookup`` | ``title_search`` |
            ``title_search_relaxed`` | ``title_search_s2`` | ``local_only``）。
    """

    id: str = ""  # UUID, assigned at ingest time
    title: str = ""
    authors: list[str] = field(default_factory=list)
    first_author: str = ""
    first_author_lastname: str = ""
    year: int | None = None
    doi: str = ""
    journal: str = ""
    abstract: str = ""
    paper_type: str = ""  # article, review, conference-paper, etc.
    citation_count_s2: int | None = None
    citation_count_openalex: int | None = None
    citation_count_crossref: int | None = None
    citation_count_ads: int | None = None
    s2_paper_id: str = ""
    openalex_id: str = ""
    crossref_doi: str = ""
    ads_bibcode: str = ""
    api_sources: list[str] = field(default_factory=list)  # which APIs returned data
    references: list[str] = field(default_factory=list)  # reference DOIs from S2
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    issn: str = ""
    source_file: str = ""
    extraction_method: str = ""


# ============================================================================
#  Constants
# ============================================================================

DOI_CORE = r'10\.\d{4,9}/[^\s,;)\]>"\'}]+'

# H1 headings that are NOT paper titles
NON_TITLE_H1 = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^Annual\s+Review",
        r"^ANNUAL\s+REVIEW",
        r"^Invited\s+Article",
        r"^Review$",
        r"^Research\s+Paper$",
        r"^Keywords?$",
        r"^Key\s*Words?$",
        r"^Abstract$",
        r"^ABSTRACT$",
        r"^a\s*r\s*t\s*i\s*c\s*l\s*e\s+i\s*n\s*f\s*o",
        r"^a\s*b\s*s\s*t\s*r\s*a\s*c\s*t",
        r"^ARTICLE\s+INFO",
        r"^Contents?\s+lists?",
        r"^\d+\.?\s",  # Section numbers
        r"^#\s*$",  # Empty heading
        r"^Cite\s+as",
        r"^References?$",
        r"^Acknowledgments?$",
    ]
]

# Patterns indicating a line is an author name (short, personal name structure)
AUTHOR_H1_INDICATORS = [
    re.compile(r"<sup>", re.IGNORECASE),
    re.compile(r"\$\^\{"),
    re.compile(r"✉"),
]

# Stop markers when scanning for authors after title
AUTHOR_STOP = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^(?:Department|School|Institute|Faculty|Center|Centre|Laboratory)\b",
        r"@\w+\.\w+",
        r"^(?:Annu\.?\s*Rev|Annual\s+Review|Copyright|©)",
        r"^(?:doi|DOI|https?://doi)",
        r"^#\s+(?:Abstract|Keywords?|Key\s*Words?|\d+|ABSTRACT|ARTICLE)",
        r"^!\[image\]",
        r"^Phys\.\s+(?:Rev|Fluids)",
        r"^J\.\s+(?:Fluid|Comput)",
        r"^Int\.\s+J\.",
        r"^Computers?\s+(?:and|&)",
        r"^eScience\s",
        r"^ARTICLES\s+YOU\s+MAY",
        r"^APL\s+",
    ]
]

CR_BASE = "https://api.crossref.org/works"

S2_BASE = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,abstract,citationCount,year,externalIds,authors,venue,publicationTypes,references.externalIds"

OA_BASE = "https://api.openalex.org/works"

TITLE_MATCH_THRESHOLD = 0.85
RELAXED_THRESHOLD = 0.65

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "ScholarAIO/1.0 (https://github.com/scholaraio)",
    }
)
# Bypass local proxy for academic API calls — proxies cause CLOSE-WAIT hangs
SESSION.trust_env = False


def configure_session(contact_email: str) -> None:
    """Update SESSION User-Agent with contact email for Crossref polite pool."""
    if contact_email:
        SESSION.headers["User-Agent"] = f"ScholarAIO/1.0 (mailto:{contact_email})"


# Retry on connection/SSL errors (common in WSL2 or when hitting APIs rapidly)
_retry = requests.adapters.HTTPAdapter(
    max_retries=requests.packages.urllib3.util.retry.Retry(
        total=3,
        backoff_factor=1,  # 1s, 2s, 4s
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET"],
    ),
)
SESSION.mount("https://", _retry)
SESSION.mount("http://", _retry)
TIMEOUT = 10
