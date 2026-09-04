"""Adapter contract: sources produce candidates but never publish corpora."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Category, CollectionPage


class SourceAdapter(ABC):
    name = "base"
    version = "0"
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def healthcheck(self) -> dict[str, Any]:
        """Validate source prerequisites without changing remote state."""

    @abstractmethod
    def collect_page(self, category: Category, cursor: str | None, limit: int) -> CollectionPage:
        """Return one resumable page of source records for a CLC category."""

    def public_descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
        }

    def resume_identity(self) -> dict[str, Any]:
        """Stable adapter settings that must match when a run is resumed."""

        return self.public_descriptor()
