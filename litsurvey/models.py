from __future__ import annotations

import re
from dataclasses import dataclass, field


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def clean_arxiv_id(aid: str) -> str:
    aid = (aid or "").strip()
    aid = re.sub(r"^(https?://arxiv\.org/(abs|pdf)/)", "", aid)
    aid = re.sub(r"^arxiv:", "", aid, flags=re.I)
    aid = re.sub(r"\.pdf$", "", aid)
    return re.sub(r"v\d+$", "", aid)


def clean_doi(doi: str) -> str:
    doi = (doi or "").strip().lower()
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    venue: str = ""
    venue_type: str = ""          # journal | conference | preprint | other
    doi: str = ""
    arxiv_id: str = ""
    url: str = ""
    pdf_url: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""
    note: str = ""                # e.g. arXiv journal_ref
    citations: int = 0
    is_open: bool = True
    sources: set[str] = field(default_factory=set)
    # filled by the pipeline
    key: str = ""
    score: float = 0.0
    relevance: float = 0.0
    theme: int = -1
    summary: str = ""             # one-line contribution for tables

    @property
    def norm_title(self) -> str:
        return normalize_title(self.title)

    def merge(self, other: "Paper") -> None:
        """Merge another record of the same work into this one.
        Published-venue metadata wins over preprint metadata (IEEE prefers
        citing the version of record); arXiv id is kept either way."""
        other_published = other.venue_type in ("journal", "conference")
        self_published = self.venue_type in ("journal", "conference")
        if other_published and not self_published:
            for f in ("venue", "venue_type", "volume", "issue", "pages", "publisher"):
                setattr(self, f, getattr(other, f) or getattr(self, f))
            if other.year:
                self.year = other.year
        for f in ("doi", "arxiv_id", "url", "pdf_url", "volume", "issue", "pages",
                  "publisher", "venue", "note"):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))
        if len(other.abstract) > len(self.abstract):
            self.abstract = other.abstract
        if len(other.authors) > len(self.authors):
            self.authors = other.authors
        if not self.year and other.year:
            self.year = other.year
        self.citations = max(self.citations, other.citations)
        self.is_open = self.is_open or other.is_open
        self.sources |= other.sources
