"""Pluggable source adapters."""

from .base import SourceAdapter
from .cnki import CNKIExportAdapter
from .mediawiki import MediaWikiAdapter

__all__ = ["SourceAdapter", "CNKIExportAdapter", "MediaWikiAdapter"]

