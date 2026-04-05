"""Parse markdown tables from raw markdown content."""

from __future__ import annotations

import re
from typing import Any

from markdown_it import MarkdownIt


def _extract_link(html: str) -> dict[str, str]:
    """Extract text and href from an <a> tag, or return raw text."""
    m = re.search(r'href=["\']([^"\']+)["\']', html)
    text = re.sub(r"<[^>]+>", "", html).strip()
    if m:
        return {"text": text, "url": m.group(1)}
    return {"text": text, "url": ""}


def _clean_cell(raw: str) -> str | dict[str, str]:
    """Clean whitespace; if the cell contains a link return a dict."""
    raw = raw.strip()
    if "<a " in raw or "[" in raw:
        # markdown-it may have already rendered inline links to <a>.
        link = _extract_link(raw)
        if link["url"]:
            return link
    # Try markdown-style links that weren't rendered.
    m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", raw)
    if m:
        return {"text": m.group(1).strip(), "url": m.group(2).strip()}
    return raw


async def parse_markdown_tables(content: str) -> list[dict[str, Any]]:
    """Parse markdown content and extract all table rows as dicts.

    Each table produces rows whose keys come from the header row.
    If a cell contains a link, the value is ``{"text": ..., "url": ...}``.
    Otherwise it is a plain string.

    Returns a flat list of row dicts across all tables found.
    """
    md = MarkdownIt()
    tokens = md.parse(content)

    rows: list[dict[str, Any]] = []
    headers: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # ---------- header row ----------
        if tok.type == "thead_open":
            i += 1
            headers = []
            while i < len(tokens) and tokens[i].type != "thead_close":
                if tokens[i].type == "th_open":
                    i += 1
                    cell_parts: list[str] = []
                    while i < len(tokens) and tokens[i].type != "th_close":
                        if tokens[i].type == "inline" and tokens[i].content:
                            cell_parts.append(tokens[i].content.strip())
                        i += 1
                    headers.append(" ".join(cell_parts))
                i += 1
            i += 1  # skip thead_close
            continue

        # ---------- body rows ----------
        if tok.type == "tbody_open":
            i += 1
            while i < len(tokens) and tokens[i].type != "tbody_close":
                if tokens[i].type == "tr_open":
                    i += 1
                    cells: list[str | dict[str, str]] = []
                    while i < len(tokens) and tokens[i].type != "tr_close":
                        if tokens[i].type == "td_open":
                            i += 1
                            cell_parts = []
                            while i < len(tokens) and tokens[i].type != "td_close":
                                if tokens[i].type == "inline" and tokens[i].content:
                                    cell_parts.append(tokens[i].content)
                                i += 1
                            raw = " ".join(cell_parts)
                            cells.append(_clean_cell(raw))
                        i += 1
                    # Build row dict.
                    if headers and cells:
                        row: dict[str, Any] = {}
                        for idx, header in enumerate(headers):
                            row[header] = cells[idx] if idx < len(cells) else ""
                        rows.append(row)
                i += 1
            i += 1  # skip tbody_close
            continue

        # ---------- fallback: pipe-table not tokenised by markdown-it ----------
        # Some markdown-it builds don't enable the table plugin.  Handle the
        # common ``| col | col |`` format manually as a last resort.
        if tok.type == "inline" and "|" in (tok.content or ""):
            # Collect consecutive inline tokens that look like table rows.
            pipe_lines: list[str] = []
            while i < len(tokens):
                t = tokens[i]
                if t.type in ("inline", "paragraph_open", "paragraph_close"):
                    if t.type == "inline" and t.content and "|" in t.content:
                        for line in t.content.split("\n"):
                            stripped = line.strip()
                            if stripped.startswith("|") or "|" in stripped:
                                pipe_lines.append(stripped)
                    i += 1
                else:
                    break

            if len(pipe_lines) >= 2:
                # First line = headers, second = separator, rest = data.
                hdr_line = pipe_lines[0]
                hdr_cells = [c.strip() for c in hdr_line.strip("|").split("|")]
                data_start = 1
                # Skip separator line (e.g. |---|---|)
                if data_start < len(pipe_lines) and re.match(
                    r"^[\s|:-]+$", pipe_lines[data_start]
                ):
                    data_start += 1
                for dline in pipe_lines[data_start:]:
                    d_cells = [c.strip() for c in dline.strip("|").split("|")]
                    row = {}
                    for idx, h in enumerate(hdr_cells):
                        val = d_cells[idx] if idx < len(d_cells) else ""
                        row[h] = _clean_cell(val) if val else ""
                    rows.append(row)
            continue

        i += 1

    return rows
