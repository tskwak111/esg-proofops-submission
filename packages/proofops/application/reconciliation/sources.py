"""Bounded, format-aware quote verification shared by source adapters."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from html.parser import HTMLParser
from io import BytesIO
from typing import Any

MAX_SOURCE_BYTES = 50 * 1024 * 1024
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class SourceVerificationError(ValueError):
    pass


class _NoDTD(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        # Called before processing the subset, independent of document encoding.
        raise SourceVerificationError("xml_dtd_forbidden")


class _HTMLText(HTMLParser):
    def __init__(self, target: str) -> None:
        super().__init__(convert_charrefs=True)
        self.target = target
        self.stack: list[str] = []
        self.count = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ids = [value for key, value in attrs if key == "id"]
        if self.target in ids:
            self.count += 1
            if self.count != 1 or len(ids) != 1:
                raise SourceVerificationError("locator_ambiguous")
            if tag not in _VOID:
                self.stack.append(tag)
        elif self.stack and tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and tag not in _VOID:
            if self.stack[-1] != tag:
                raise SourceVerificationError("html_target_unbalanced")
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.parts.append(data)


def validate_source_bytes(
    payload: bytes, ref: Mapping[str, Any], *, format_name: str | None = None
) -> None:
    """Verify literal text at the locator; a bare callable may omit the format hint."""
    if not isinstance(payload, bytes) or len(payload) > MAX_SOURCE_BYTES:
        raise SourceVerificationError("source_size_invalid")
    locator, quote = ref.get("locator"), ref.get("quote")
    if not isinstance(locator, str) or not isinstance(quote, str) or not quote:
        raise SourceVerificationError("source_reference_invalid")
    if format_name is None:
        if locator.startswith("chars:"):
            format_name = "text"
        elif locator.startswith("page:"):
            format_name = "pdf"
        elif locator.startswith("id:"):
            prefix = payload[:512].lower()
            format_name = "html" if b"<html" in prefix or b"<!doctype html" in prefix else "xml"
    if format_name == "text":
        match = re.fullmatch(r"chars:(0|[1-9][0-9]*):(0|[1-9][0-9]*)", locator)
        if match is None:
            raise SourceVerificationError("unsupported_locator")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceVerificationError("artifact_not_utf8") from exc
        start, end = map(int, match.groups())
        if end <= start or end > len(text):
            raise SourceVerificationError("locator_out_of_range")
        if text[start:end] != quote:
            raise SourceVerificationError("quote_mismatch")
        return
    if format_name in {"html", "xml"}:
        if not locator.startswith("id:") or not locator[3:]:
            raise SourceVerificationError("unsupported_locator")
        target = locator[3:]
        if format_name == "html":
            parser = _HTMLText(target)
            try:
                parser.feed(payload.decode("utf-8"))
                parser.close()
            except (UnicodeError, ValueError) as exc:
                raise SourceVerificationError("html_invalid_or_ambiguous") from exc
            if parser.count != 1 or parser.stack:
                raise SourceVerificationError("locator_missing_or_unclosed")
            text = "".join(parser.parts)
        else:
            try:
                root = ET.fromstring(payload, parser=ET.XMLParser(target=_NoDTD()))
            except ET.ParseError as exc:
                raise SourceVerificationError("xml_invalid") from exc
            xml_id = "{http://www.w3.org/XML/1998/namespace}id"
            matches = [
                node
                for node in root.iter()
                if node.attrib.get("id") == target or node.attrib.get(xml_id) == target
            ]
            if len(matches) != 1:
                raise SourceVerificationError("locator_missing_or_ambiguous")
            text = "".join(matches[0].itertext())
        if quote not in text:
            raise SourceVerificationError("quote_mismatch")
        return
    if format_name == "pdf":
        match = re.fullmatch(r"page:([1-9][0-9]*)(:whitespace-v1)?", locator)
        if match is None or not payload.startswith(b"%PDF-"):
            raise SourceVerificationError("unsupported_locator")
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise SourceVerificationError("pdf_support_unavailable") from exc
        try:
            reader = PdfReader(BytesIO(payload), strict=True)
            page = int(match.group(1))
            if reader.is_encrypted:
                raise SourceVerificationError("pdf_encrypted")
            if page > len(reader.pages):
                raise SourceVerificationError("page_out_of_range")
            text = reader.pages[page - 1].extract_text()
        except SourceVerificationError:
            raise
        except Exception as exc:
            raise SourceVerificationError("pdf_invalid") from exc
        if match.group(2) is not None:
            # Opt-in locator version: preserve every non-whitespace character.
            # Legacy page:N remains literal; no OCR, fuzzy matching or token joining.
            normalized = " ".join((text or "").split())
            canonical_quote = " ".join(quote.split())
            if not canonical_quote or quote != canonical_quote:
                raise SourceVerificationError("quote_not_canonical")
            start = normalized.find(quote)
            if start < 0:
                raise SourceVerificationError("quote_mismatch")
            if normalized.find(quote, start + 1) >= 0:
                raise SourceVerificationError("locator_ambiguous")
        elif text is None or quote not in text:
            raise SourceVerificationError("quote_mismatch")
        return
    raise SourceVerificationError("unsupported_locator")
