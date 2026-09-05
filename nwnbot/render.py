"""Conversion between markdown and the editor's rich-text HTML.

Two pure, stdlib-only functions and no I/O:

- :func:`md_to_html` — Discord-flavoured markdown to the ``<div>``-per-line
  shape the roadmap editor's contenteditable box produces. Bold, italic,
  underline, links, lists, blockquotes and rules only; every other character is
  escaped, so ``<script>``/``<style>``/event handlers/``javascript:`` URLs can
  never survive.
- :func:`html_to_md` — the inverse, hardened against a whole pasted Discord
  message DOM: unknown tags are unwrapped, ``<script>``/``<style>`` subtrees are
  dropped, empty ``<div><span>`` chrome collapses away, and links and images
  come back as plain URLs.

Shapes verified against ``nwn_homers_lotr`` (read-only), not guessed:

- the editor is a ``contenteditable`` driven by ``document.execCommand``
  (``bin/roadmap-editor.py:4925`` and ``:4961``), so Chrome emits one ``<div>``
  per line and ``<div><br></div>`` for a blank one;
- an idea link is ``<a href="#idea-ID">label</a>`` (``:4560``) and a web link is
  ``<a href="URL" target="_blank" rel="noopener">label</a>`` (``:5109``);
- the authoritative save-time whitelist is ``bin/roadmap_sanitize.py``
  (``ALLOWED_TAGS`` / ``ALLOWED_ATTRS``), mirrored in JS at ``:4995``. Every tag
  and attribute emitted here is inside that whitelist, so a round trip through
  the editor's own sanitizer is a no-op.

There is deliberately **no** ``<code>``/``<pre>``: neither is on the roadmap
sanitizer's whitelist, so emitting one would be silently unwrapped on save and
break the fixed point. Inline code keeps its literal backticks as text instead,
which round-trips exactly.

``cdn.discordapp.com`` links are signed and expire: they are stored as plain
links, never rehosted, and :func:`annotate_expiring_links` appends a one-line
warning that they may 404.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

# Discord's plain-message limit is 2000 and an embed description is 4096; the
# plan fixes Discord-bound text at 4000 so a link back to the editor always fits.
DISCORD_TEXT_LIMIT = 4000

# Signed, expiring attachment hosts — link only, never rehost.
DISCORD_CDN_HOST = "cdn.discordapp.com"
DISCORD_CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")

# Appended once when a stored blob carries an expiring attachment link.
# PROVISIONAL WORDING — see plan.md review item [r8]. Placeholder chosen to invent as
# little as possible; the admin owns the final text.
CDN_EXPIRY_NOTE = (
    "Note: Discord attachment links above are signed and expire, so they may "
    "404 later. The attachment was not rehosted."
)

# --- the tag vocabulary, all inside roadmap_sanitize.ALLOWED_TAGS -----------

_BLOCK_TAGS = {"div", "p", "blockquote"}
_LIST_TAGS = {"ul", "ol"}
_VOID_TAGS = {
    "br", "hr", "img", "area", "base", "col", "embed", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# Subtrees whose *content* is dropped rather than unwrapped.
_DROP_TAGS = {"script", "style", "head", "title", "noscript", "svg", "iframe",
              "object", "template"}

_SAFE_SCHEMES = ("http://", "https://", "mailto:")

__all__ = [
    "CDN_EXPIRY_NOTE",
    "DISCORD_CDN_HOST",
    "DISCORD_CDN_HOSTS",
    "DISCORD_TEXT_LIMIT",
    "annotate_expiring_links",
    "find_expiring_links",
    "html_to_md",
    "md_to_html",
    "truncate_for_discord",
]


def _safe_href(value: str) -> str | None:
    """Mirror ``roadmap_sanitize._safe_href``: ``#anchor``, http(s), mailto."""
    v = (value or "").strip()
    if not v:
        return None
    if v.startswith("#"):
        return v
    if v.lower().startswith(_SAFE_SCHEMES):
        return v
    return None


def _safe_src(value: str) -> str | None:
    v = (value or "").strip()
    return v if v.lower().startswith(("http://", "https://")) else None


# ===========================================================================
# markdown -> editor HTML
# ===========================================================================

# One pass over a line's inline markup. Order matters: escapes, then code
# (literal), then links, then emphasis, then bare URLs.
_INLINE_RE = re.compile(
    r"""
      (?P<esc>\\[\\*_`\[\]()>#+\-.!~])
    | (?P<code>`[^`\n]*`)
    | (?P<img>!\[(?P<ialt>(?:\\.|[^\]\\])*)\]\((?P<iurl>[^)\s]*)\))
    | (?P<link>\[(?P<ltext>(?:\\.|[^\]\\])*)\]\((?P<lurl>[^)\s]*)\))
    | (?P<bold>\*\*(?P<btext>.+?)\*\*)
    | (?P<und>__(?P<utext>.+?)__)
    | (?P<ital>\*(?P<itext>[^*\n]+?)\*)
    | (?P<url>(?:https?://|mailto:)[^\s<>()\[\]"']+)
    """,
    re.VERBOSE,
)

# Bare URLs swallow trailing sentence punctuation; hand it back to the text.
_URL_TRAIL = ".,;:!?"


def _text_html(s: str) -> str:
    """Escape a run of plain text for the editor's HTML."""
    return escape(s, quote=False)


def _attr(value: str) -> str:
    return escape(value, quote=True)


def _anchor_html(href: str, label_html: str) -> str:
    """Emit a link in exactly the shape the editor's own pickers produce."""
    if href.startswith("#"):
        return f'<a href="{_attr(href)}">{label_html}</a>'
    return (f'<a href="{_attr(href)}" target="_blank" rel="noopener">'
            f"{label_html}</a>")


def _inline_to_html(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(_text_html(text[pos:m.start()]))
        pos = m.end()
        kind = m.lastgroup
        # ``lastgroup`` is the last *matched* group, which for the alternations
        # carrying inner captures is the inner one; branch on membership.
        if m.group("esc"):
            out.append(_text_html(m.group("esc")[1]))
        elif m.group("code"):
            out.append(_text_html(m.group("code")))
        elif m.group("img") is not None and m.group("img"):
            url = _safe_src(m.group("iurl"))
            if url:
                out.append(_anchor_html(url, _text_html(url)))
            else:
                out.append(_inline_to_html(m.group("ialt")))
        elif m.group("link") is not None and m.group("link"):
            href = _safe_href(m.group("lurl"))
            label = m.group("ltext")
            label_html = _inline_to_html(label) if label else ""
            if href is None:
                # Unsafe or missing scheme (javascript:, data:, ...): the label
                # survives as plain text, the URL is dropped entirely.
                out.append(label_html or _text_html(m.group("lurl")))
            else:
                out.append(_anchor_html(href, label_html or _text_html(href)))
        elif m.group("bold") is not None and m.group("bold"):
            out.append("<b>" + _inline_to_html(m.group("btext")) + "</b>")
        elif m.group("und") is not None and m.group("und"):
            out.append("<u>" + _inline_to_html(m.group("utext")) + "</u>")
        elif m.group("ital") is not None and m.group("ital"):
            out.append("<i>" + _inline_to_html(m.group("itext")) + "</i>")
        elif kind == "url" or m.group("url"):
            url = m.group("url")
            trail = ""
            while url and url[-1] in _URL_TRAIL:
                trail = url[-1] + trail
                url = url[:-1]
            if url:
                out.append(_anchor_html(url, _text_html(url)))
            out.append(_text_html(trail))
    if pos < len(text):
        out.append(_text_html(text[pos:]))
    return "".join(out)


_LIST_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])[ \t]+(?P<body>.*)$")
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?(?P<body>.*)$")
_RULE_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")


def _indent_width(raw: str) -> int:
    return len(raw.replace("\t", "  "))


def _render_list(lines: list[str], start: int, depth: int) -> tuple[str, int]:
    """Render one list starting at ``lines[start]``; return (html, next index)."""
    first = _LIST_RE.match(lines[start])
    assert first is not None
    ordered = first.group("marker")[-1] in ".)"
    tag = "ol" if ordered else "ul"
    items: list[str] = []
    i = start
    while i < len(lines):
        m = _LIST_RE.match(lines[i])
        if not m:
            break
        width = _indent_width(m.group("indent"))
        if width < depth:
            break
        if width > depth:
            nested, i = _render_list(lines, i, width)
            if items:
                items[-1] += nested
            else:
                items.append(nested)
            continue
        if (m.group("marker")[-1] in ".)") != ordered:
            break
        items.append(_inline_to_html(m.group("body").strip()))
        i += 1
    body = "".join(f"<li>{it}</li>" if not it.startswith("<ul")
                   and not it.startswith("<ol") else it for it in items)
    return f"<{tag}>{body}</{tag}>", i


def md_to_html(md: str | None) -> str:
    """Render markdown as the editor's rich-text HTML.

    Only bold, italic, underline, inline code, links, lists, blockquotes and
    horizontal rules are recognised; everything else is HTML-escaped text, so
    no tag, attribute or URL scheme from the input can ever reach the output.
    """
    if not md:
        return ""
    lines = str(md).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return _blocks_to_html(lines)


def _blocks_to_html(lines: list[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _RULE_RE.match(line):
            out.append("<hr>")
            i += 1
        elif _LIST_RE.match(line):
            html, i = _render_list(lines, i, _indent_width(_LIST_RE.match(line).group("indent")))
            out.append(html)
        elif _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                quoted.append(_QUOTE_RE.match(lines[i]).group("body"))
                i += 1
            out.append("<blockquote>" + _blocks_to_html(quoted) + "</blockquote>")
        elif line.strip() == "":
            out.append("<div><br></div>")
            i += 1
        else:
            out.append("<div>" + _inline_to_html(line.strip()) + "</div>")
            i += 1
    return "".join(out)


# ===========================================================================
# editor / Discord HTML -> markdown
# ===========================================================================

class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []


class _TreeBuilder(HTMLParser):
    """Build a forgiving tree: unknown tags stay, stray end tags are ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("")
        self.stack = [self.root]
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth or tag in _DROP_TAGS:
            self.drop_depth += 1
            return
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.drop_depth or tag in _DROP_TAGS:
            return
        self.stack[-1].children.append(
            _Node(tag, {k.lower(): (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth -= 1
            return
        if tag in _VOID_TAGS:
            return
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return
        # Stray close tag: dropped, never allowed to pop one of our containers.

    def handle_data(self, data):
        if self.drop_depth:
            return
        self.stack[-1].children.append(data)


# Markdown metacharacters that must not be re-parsed as markup on the way back.
_ESC_SPECIALS = re.compile(r"([\\*\[\]])")
_ESC_UNDERLINE = re.compile(r"_{2,}")
_LEADING_NUMBER = re.compile(r"^(\d+)([.)])")


def _protect_body(body: str) -> str:
    """Escape a leading ``-``/``>``/``1.`` so plain text is not read as markup.

    Runs on the line body only, before any list marker or quote prefix is
    attached, so the markers this module emits are never double-escaped.
    """
    if not body:
        return body
    m = _LEADING_NUMBER.match(body)
    if m:
        return m.group(1) + "\\" + body[m.end(1):]
    if body[0] in "-+>#":
        return "\\" + body
    return body


def _escape_md(text: str) -> str:
    text = _ESC_SPECIALS.sub(r"\\\1", text)
    return _ESC_UNDERLINE.sub(lambda m: r"\_" * len(m.group()), text)


class _MdWriter:
    """Accumulates rendered lines, carrying list/quote prefixes."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cur: list[str] = []
        self.prefix = ""          # pending list marker, consumed by one line
        self.indent = ""          # continuation indent inside list items
        self.quote = ""

    def text(self, s: str) -> None:
        if s:
            self.cur.append(s)

    def flush(self, force_blank: bool = False) -> None:
        body = _protect_body(re.sub(r"[ \t]{2,}", " ", "".join(self.cur)).strip())
        self.cur = []
        if not body:
            if force_blank:
                self.lines.append("")
            return
        head = self.quote + (self.prefix or self.indent)
        self.prefix = ""
        self.lines.append((head + body).rstrip())

    def rule(self) -> None:
        self.flush()
        self.lines.append(self.quote + "---")


def _inline_of(node: _Node) -> str:
    """Render a subtree as a single inline string (used for link labels)."""
    w = _MdWriter()
    _walk(node.children, w)
    w.flush()
    return " ".join(x for x in w.lines if x).strip()


def _walk(nodes: list, w: _MdWriter) -> None:
    for node in nodes:
        if isinstance(node, str):
            collapsed = re.sub(r"\s+", " ", node)
            if collapsed.strip() == "":
                if w.cur:
                    w.text(" ")
                continue
            w.text(_escape_md(collapsed))
            continue

        tag = node.tag
        if tag == "br":
            w.flush(force_blank=True)
        elif tag == "hr":
            w.rule()
        elif tag == "img":
            src = _safe_src(node.attrs.get("src", ""))
            if src:
                if w.cur:
                    w.text(" ")
                w.text(src)
        elif tag == "a":
            href = _safe_href(node.attrs.get("href", ""))
            label = _inline_of(node)
            if href is None:
                w.text(label)
            elif not label or label == _escape_md(href) or label == href:
                w.text(href)
            else:
                w.text(f"[{label}]({href})")
        elif tag in ("b", "strong", "i", "em", "u"):
            mark = {"b": "**", "strong": "**", "i": "*", "em": "*", "u": "__"}[tag]
            inner = _inline_of(node)
            if not inner:
                continue
            if w.cur and not "".join(w.cur).endswith(" "):
                w.text(" ")
            w.text(f"{mark}{inner}{mark} ")
        elif tag in _LIST_TAGS:
            w.flush()
            saved_prefix, saved_indent = w.prefix, w.indent
            depth = len(w.indent) // 2
            counter = [0]
            for child in node.children:
                if isinstance(child, _Node) and child.tag == "li":
                    counter[0] += 1
                    w.indent = "  " * depth
                    w.prefix = w.indent + (
                        f"{counter[0]}. " if tag == "ol" else "- ")
                    w.indent = "  " * (depth + 1)
                    _walk(child.children, w)
                    w.flush()
                else:
                    w.indent = "  " * (depth + 1)
                    _walk([child], w)
            w.flush()
            w.prefix, w.indent = saved_prefix, saved_indent
        elif tag == "li":
            # A bare <li> outside any list — the roadmap sanitizer unwraps these
            # (roadmap_sanitize._Sanitizer.handle_starttag); do the same.
            w.flush()
            _walk(node.children, w)
            w.flush()
        elif tag == "blockquote":
            w.flush()
            saved = w.quote
            w.quote = saved + "> "
            _walk(node.children, w)
            w.flush()
            w.quote = saved
        elif tag in _BLOCK_TAGS:
            w.flush()
            _walk(node.children, w)
            w.flush()
        else:
            # span, font and every scrap of Discord chrome: unwrap.
            _walk(node.children, w)


def html_to_md(html: str | None) -> str:
    """Reduce editor or pasted-Discord HTML to plain markdown.

    Chrome is stripped, ``<div><span>`` nesting collapses, links and images come
    back as plain URLs, and no tag or attribute survives into the output.
    """
    if not html:
        return ""
    parser = _TreeBuilder()
    parser.feed(str(html))
    parser.close()

    w = _MdWriter()
    _walk(parser.root.children, w)
    w.flush()

    lines: list[str] = []
    for line in w.lines:
        if line == "" and (not lines or lines[-1] == ""):
            continue  # collapse the runs of empty chrome divs
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


# ===========================================================================
# Discord-bound text
# ===========================================================================

_CDN_RE = re.compile(
    r"https?://(?:%s)/[^\s<>\"')\]]+" % "|".join(
        re.escape(h) for h in DISCORD_CDN_HOSTS))


def find_expiring_links(text: str | None) -> list[str]:
    """Return the signed, expiring Discord CDN links in ``text``, in order."""
    if not text:
        return []
    seen: list[str] = []
    for url in _CDN_RE.findall(str(text)):
        if url not in seen:
            seen.append(url)
    return seen


def annotate_expiring_links(text: str | None,
                            note: str = CDN_EXPIRY_NOTE) -> str:
    """Append the may-404 note once when ``text`` carries a CDN link.

    The attachment is never rehosted — the link is stored as-is.
    """
    body = "" if text is None else str(text)
    if not find_expiring_links(body) or note in body:
        return body
    return (body.rstrip() + "\n\n" + note) if body.strip() else note


def truncate_for_discord(text: str | None,
                         editor_url: str | None = None,
                         limit: int = DISCORD_TEXT_LIMIT) -> str:
    """Cut ``text`` to ``limit`` characters, keeping a link back to the editor.

    The result — ellipsis and link included — is never longer than ``limit``.

    The visible marker is a bare "…" plus the editor URL. PROVISIONAL WORDING —
    see plan.md review item [r8]; players see this. The 4000-char limit itself
    is settled.
    """
    body = "" if text is None else str(text)
    suffix = "…" + ("\n\n" + editor_url if editor_url else "")
    if len(body) <= limit:
        return body
    room = limit - len(suffix)
    if room <= 0:
        return suffix[:limit]
    head = body[:room]
    cut = head.rfind(" ")
    nl = head.rfind("\n")
    boundary = max(cut, nl)
    if boundary > room // 2:
        head = head[:boundary]
    return head.rstrip() + suffix
