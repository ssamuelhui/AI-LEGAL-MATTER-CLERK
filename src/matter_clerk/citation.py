from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class Citation(BaseModel):
    """A single pointer to a verifiable source.

    Day 1 populates source + page_or_paragraph + text_snippet only.
    `url` and `court_rank` are reserved for CanLII authorities (Phase 2).
    """

    source: str
    page_or_paragraph: str
    text_snippet: str
    url: Optional[str] = None
    court_rank: Optional[str] = None

    def inline(self) -> str:
        return f"[{self.source} {self.page_or_paragraph}]"
