"""Server-side safe markdown for requirement review cards (design R4 SafeMarkdown).

The renderer never passes source HTML through. Raw HTML in the source is
removed, every text run is escaped, and the output only contains tags this
module emits itself from an allowlist. Links are limited to https:/mailto:
and images become plain links, so no external resource is ever loaded.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

ALLOWED_TAGS = frozenset({
    "p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "blockquote",
    "h3", "h4", "h5", "a", "table", "thead", "tbody", "tr", "th", "td", "hr", "div",
})
ALLOWED_ATTRS = {"a": {"href", "rel", "target"}, "div": {"class", "tabindex", "role", "aria-label"}}

_RAW_BLOCK = re.compile(r"<(script|style|iframe|object|embed|svg|math|template|noscript|textarea|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_RAW_TAG = re.compile(r"</?[A-Za-z!][^>]*>|<!--.*?-->", re.DOTALL)
_SAFE_SCHEMES = ("https://", "mailto:")


@dataclass
class Rendered:
    html: str
    sanitized: bool


class _Renderer:
    def __init__(self) -> None:
        self.sanitized = False

    # ---------- inline ----------
    def _href(self, url: str) -> str | None:
        candidate = html.unescape(url).strip()
        compact = re.sub(r"[\s\x00-\x1f]", "", candidate).lower()
        if not compact.startswith(_SAFE_SCHEMES):
            self.sanitized = True
            return None
        return candidate

    def inline(self, text: str) -> str:
        # Tokenise code spans first so their content is never interpreted.
        parts: list[str] = []
        for index, chunk in enumerate(re.split(r"(`[^`]*`)", text)):
            if index % 2 == 1:
                parts.append(f"<code>{html.escape(chunk[1:-1], quote=True)}</code>")
            else:
                parts.append(self._inline_text(chunk))
        return "".join(parts)

    def _inline_text(self, text: str) -> str:
        out: list[str] = []
        pattern = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
        position = 0
        for match in pattern.finditer(text):
            out.append(self._emphasis(text[position:match.start()]))
            is_image, label, url = match.group(1), match.group(2), match.group(3)
            href = self._href(url)
            if is_image:
                self.sanitized = True
                label_html = f"[รูป: {html.escape(label or 'ไม่มีคำอธิบาย', quote=True)}] ↗"
            else:
                label_html = self._emphasis(label) or html.escape(url, quote=True)
            if href:
                out.append(f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer" target="_blank">{label_html}</a>')
            else:
                out.append(label_html)
            position = match.end()
        out.append(self._emphasis(text[position:]))
        return "".join(out)

    def _emphasis(self, text: str) -> str:
        escaped = html.escape(text, quote=True)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", escaped)
        return escaped

    # ---------- blocks ----------
    def render(self, source: str) -> str:
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        lines = source.split("\n")
        blocks: list[str] = []
        i = 0
        paragraph: list[str] = []

        def flush() -> None:
            if paragraph:
                blocks.append("<p>" + "<br>".join(self.inline(line.strip()) for line in paragraph) + "</p>")
                paragraph.clear()

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("```"):
                flush()
                code: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code.append(lines[i])
                    i += 1
                i += 1
                blocks.append('<div class="md-scroll" tabindex="0" role="region" aria-label="โค้ด"><pre><code>'
                              + html.escape("\n".join(code), quote=True) + "</code></pre></div>")
                continue
            if not stripped:
                flush()
                i += 1
                continue
            heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if heading:
                flush()
                level = min(5, max(3, len(heading.group(1)) + 2))
                blocks.append(f"<h{level}>{self.inline(heading.group(2).strip())}</h{level}>")
                i += 1
                continue
            if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
                flush()
                blocks.append("<hr>")
                i += 1
                continue
            if stripped.startswith(">"):
                flush()
                quoted: list[str] = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    quoted.append(lines[i].strip()[1:].lstrip())
                    i += 1
                blocks.append("<blockquote>" + _Renderer._nested(self, "\n".join(quoted)) + "</blockquote>")
                continue
            if stripped.startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", lines[i + 1].strip()):
                flush()
                header = _cells(stripped)
                i += 2
                rows: list[list[str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append(_cells(lines[i].strip()))
                    i += 1
                head = "".join(f"<th>{self.inline(cell)}</th>" for cell in header)
                body = "".join("<tr>" + "".join(f"<td>{self.inline(cell)}</td>" for cell in row) + "</tr>" for row in rows)
                blocks.append('<div class="md-scroll" tabindex="0" role="region" aria-label="ตาราง"><table><thead><tr>'
                              + head + "</tr></thead><tbody>" + body + "</tbody></table></div>")
                continue
            list_match = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
            if list_match:
                flush()
                ordered = list_match.group(2)[0].isdigit()
                items: list[str] = []
                while i < len(lines):
                    current = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                    if current and current.group(2)[0].isdigit() == ordered:
                        items.append(current.group(3))
                        i += 1
                    elif items and lines[i].startswith(("  ", "\t")) and lines[i].strip():
                        items[-1] += " " + lines[i].strip()
                        i += 1
                    else:
                        break
                tag = "ol" if ordered else "ul"
                blocks.append(f"<{tag}>" + "".join(f"<li>{self.inline(item.strip())}</li>" for item in items) + f"</{tag}>")
                continue
            paragraph.append(line)
            i += 1
        flush()
        return "".join(blocks)

    def _nested(self, text: str) -> str:
        return self.render(text)


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def strip_raw_html(source: str) -> tuple[str, bool]:
    """Remove raw HTML (and dangerous element bodies) outside code fences/spans."""
    changed = False
    out: list[str] = []
    for index, chunk in enumerate(re.split(r"(```.*?(?:```|$))", source, flags=re.DOTALL)):
        if index % 2 == 1:
            out.append(chunk)
            continue
        pieces = re.split(r"(`[^`\n]*`)", chunk)
        for piece_index, piece in enumerate(pieces):
            if piece_index % 2 == 1:
                out.append(piece)
                continue
            cleaned = _RAW_BLOCK.sub("", piece)
            cleaned = _RAW_TAG.sub("", cleaned)
            if cleaned != piece:
                changed = True
            out.append(cleaned)
    return "".join(out), changed


def render_markdown(source: str | None) -> Rendered:
    if not source or not source.strip():
        return Rendered(html="", sanitized=False)
    cleaned, changed = strip_raw_html(source)
    renderer = _Renderer()
    output = renderer.render(cleaned)
    return Rendered(html=output, sanitized=changed or renderer.sanitized)


def render_inline(source: str | None) -> Rendered:
    """Inline-only markdown (code, emphasis, safe links) for list items such as AC."""
    if not source or not source.strip():
        return Rendered(html="", sanitized=False)
    cleaned, changed = strip_raw_html(source.replace("\n", " "))
    renderer = _Renderer()
    output = renderer.inline(cleaned.strip())
    return Rendered(html=output, sanitized=changed or renderer.sanitized)
