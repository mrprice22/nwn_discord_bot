"""Tests for ``nwnbot.render`` — [b4-render].

Two properties carry the weight here:

1. **Second round trip is a fixed point.** The first conversion is allowed to
   normalize (a Discord paste collapses hard); after that, converting again must
   change nothing. Asserted as a property over a table of inputs, in both
   directions, plus over every real ``notes`` blob in ``tests/fixtures``.
2. **No markup leaks.** Real pasted-Discord DOM lifted out of ``roadmap.yaml``
   must come back through ``html_to_md`` as text, and nothing an attacker can
   put in markdown may come out of ``md_to_html`` as a tag, handler or
   ``javascript:`` URL.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from nwnbot.render import (
    CDN_EXPIRY_NOTE,
    DISCORD_TEXT_LIMIT,
    annotate_expiring_links,
    find_expiring_links,
    html_to_md,
    md_to_html,
    truncate_for_discord,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "discord_notes.yaml"


def _fixtures() -> list[dict]:
    return yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))


DISCORD_NOTES = _fixtures()
NOTE_IDS = [n["id"] for n in DISCORD_NOTES]


# --- markdown inputs, exercised in both directions -------------------------

MD_CASES = [
    "",
    "hello world",
    "**bold** and *italic* and __underline__",
    "a `code()` span stays literal",
    "line one\nline two",
    "para one\n\npara two",
    "- one\n- two\n- three",
    "- one\n- two\n  - nested\n- three",
    "1. first\n2. second",
    "> quoted\n> still quoted",
    "---",
    "[label](#idea-forge-thing)",
    "[Balrogs Battle Claw](https://homerslotr.com/items/it_crewpsp022)",
    "see https://example.com/a_b_c for details",
    "mail me at mailto:nobody@example.com",
    "https://cdn.discordapp.com/attachments/1/2/image.png",
    "a * b _ c [ d ] e \\ f",
    "trailing spaces   \nand   inner   runs",
    "unicode — em dash and 🤘 emoji",
    "text with <div>angle</div> brackets",
    "1985. a year, not a list, when escaped: 1985\\. a year",
]

HTML_CASES = [
    "",
    "<div>plain</div>",
    "<div><br></div>",
    "<div>a</div><div><br></div><div>b</div>",
    "<div>a<br>b</div>",
    "<span><span>collapse</span></span>",
    "<div><div><div><span>deep chrome</span></div></div></div>",
    "<div><div></div></div><div><div></div></div>",
    "<ul><li>one</li><li>two</li></ul>",
    "<ol><li>one</li><li>two</li></ol>",
    "<ul><li>outer<ul><li>inner</li></ul></li></ul>",
    "<div>bare <li>list item</li> outside a list</div>",
    '<div><a href="#idea-x">label</a></div>',
    '<div><a href="https://example.com" target="_blank" rel="noopener">'
    "https://example.com</a></div>",
    '<div><img src="https://cdn.discordapp.com/attachments/1/2/i.png" alt="Image"></div>',
    '<div><font color="#26a269">coloured admin note</font></div>',
    "<blockquote><div>quoted</div></blockquote>",
    "<div>a</div><hr><div>b</div>",
    "<div>unclosed <b>bold</div>",
    "</div></span><div>stray closers</div>",
    "<div>&amp; &lt; &gt; &quot; entities</div>",
]


def _md_trip(text: str) -> str:
    return html_to_md(md_to_html(text))


def _html_trip(html: str) -> str:
    return md_to_html(html_to_md(html))


@pytest.mark.parametrize("src", MD_CASES)
def test_markdown_second_round_trip_is_a_fixed_point(src):
    once = _md_trip(src)
    twice = _md_trip(once)
    assert twice == once


@pytest.mark.parametrize("src", HTML_CASES)
def test_html_second_round_trip_is_a_fixed_point(src):
    once = _html_trip(src)
    twice = _html_trip(once)
    assert twice == once


@pytest.mark.parametrize("note", DISCORD_NOTES, ids=NOTE_IDS)
def test_real_discord_notes_second_round_trip_is_a_fixed_point(note):
    once = _html_trip(note["notes"])
    twice = _html_trip(once)
    assert twice == once


@pytest.mark.parametrize("note", DISCORD_NOTES, ids=NOTE_IDS)
def test_real_discord_notes_survive_html_to_md_without_markup(note):
    md = html_to_md(note["notes"])
    assert md.strip(), "a real note must not render empty"
    # No tags, no entities, no stray attributes left behind.
    assert not re.search(r"</?[a-zA-Z][^>]*>", md)
    assert "&amp;" not in md and "&lt;" not in md and "&nbsp;" not in md
    for attr in ("href=", "src=", "class=", "style=", "target=", "rel=",
                 "alt=", "color="):
        assert attr not in md
    # Chrome collapsed: no runs of blank lines, no leading/trailing blanks.
    assert "\n\n\n" not in md
    assert md == md.strip("\n")


@pytest.mark.parametrize("note", DISCORD_NOTES, ids=NOTE_IDS)
def test_real_discord_notes_keep_their_links_and_images(note):
    md = html_to_md(note["notes"])
    for url in re.findall(r'(?:href|src)="(https?://[^"]+)"', note["notes"]):
        # Entities in the stored attribute are decoded by the parser.
        assert url.replace("&amp;", "&") in md


# --- escaping is a security boundary ---------------------------------------

HOSTILE = [
    "<script>alert(1)</script>",
    "<style>body{display:none}</style>",
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">click</a>',
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    "[click](data:text/html;base64,PHNjcmlwdD4=)",
    "[click](vbscript:msgbox(1))",
    "![x](javascript:alert(1))",
    '<div onclick="alert(1)">x</div>',
    "<iframe src=https://evil.example></iframe>",
    "**bold <script>alert(1)</script>**",
    "- <script>alert(1)</script>",
    "> <script>alert(1)</script>",
]


# A real tag in the output — i.e. one that was *not* escaped into text.
_REAL_TAG = re.compile(
    r"</?(?:a|b|strong|i|em|u|ul|ol|li|p|br|hr|div|span|font|img|blockquote)"
    r"(?:\s[^<>]*)?>", re.I)


@pytest.mark.parametrize("src", HOSTILE)
def test_md_to_html_never_emits_script_style_or_handlers(src):
    out = md_to_html(src)
    # Every angle bracket that is not one of our own tags must be escaped text.
    assert "<" not in _REAL_TAG.sub("", out)
    for tag in _REAL_TAG.findall(out):
        low = tag.lower()
        assert not re.search(r"\son[a-z]+\s*=", low), tag
        for scheme in ("javascript:", "vbscript:", "data:"):
            assert f'="{scheme}' not in low, tag
            assert f"='{scheme}" not in low, tag
    lowered = out.lower()
    assert "<script" not in lowered and "<style" not in lowered
    assert "<iframe" not in lowered


@pytest.mark.parametrize("src", HOSTILE)
def test_md_to_html_only_emits_whitelisted_tags(src):
    # Mirrors roadmap_sanitize.ALLOWED_TAGS; anything else would be unwrapped
    # (or worse) by the editor's own sanitizer on save.
    allowed = {"a", "b", "strong", "i", "em", "u", "ul", "ol", "li",
               "p", "br", "hr", "div", "span", "font", "img", "blockquote"}
    for tag in re.findall(r"</?([a-zA-Z0-9]+)", md_to_html(src)):
        assert tag.lower() in allowed


def test_hostile_html_survives_html_to_md_as_nothing_dangerous():
    md = html_to_md(
        '<script>alert(1)</script><style>b{}</style>'
        '<div onclick="alert(1)">text</div>'
        '<a href="javascript:alert(1)">label</a>'
    )
    assert "alert(1)" not in md
    assert "javascript:" not in md
    assert "text" in md and "label" in md


def test_md_to_html_escapes_quotes_inside_link_urls():
    out = md_to_html('[x](https://example.com/"onmouseover="alert(1))')
    assert 'onmouseover="alert' not in out
    assert "&quot;" in out


# --- shape: what the editor's rich-text box actually produces ---------------

def test_md_to_html_emits_one_div_per_line():
    assert md_to_html("a\nb") == "<div>a</div><div>b</div>"


def test_md_to_html_emits_div_br_div_for_a_blank_line():
    assert md_to_html("a\n\nb") == "<div>a</div><div><br></div><div>b</div>"


def test_md_to_html_uses_the_editors_idea_link_shape():
    assert md_to_html("[t](#idea-x)") == '<div><a href="#idea-x">t</a></div>'


def test_md_to_html_uses_the_editors_web_link_shape():
    assert md_to_html("[t](https://e.com)") == (
        '<div><a href="https://e.com" target="_blank" rel="noopener">t</a></div>'
    )


def test_md_to_html_inline_marks():
    assert md_to_html("**b** *i* __u__") == "<div><b>b</b> <i>i</i> <u>u</u></div>"


def test_md_to_html_lists():
    assert md_to_html("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"
    assert md_to_html("1. a\n2. b") == "<ol><li>a</li><li>b</li></ol>"


def test_md_to_html_never_emits_code_or_pre_tags():
    # Neither is on the roadmap sanitizer's whitelist, so inline code keeps its
    # literal backticks instead of becoming a tag that would be unwrapped.
    out = md_to_html("run `make test` now")
    assert "<code" not in out and "<pre" not in out
    assert "`make test`" in out


def test_html_to_md_collapses_div_span_nesting():
    assert html_to_md("<div><div><span><span>x</span></span></div></div>") == "x"


def test_html_to_md_drops_empty_chrome_blocks():
    assert html_to_md("<div><div></div></div><div>x</div><div><div></div></div>") == "x"


def test_html_to_md_keeps_images_as_plain_urls():
    md = html_to_md('<div><img src="https://media.discordapp.net/a/b.png" alt="Image"></div>')
    assert md == "https://media.discordapp.net/a/b.png"


def test_html_to_md_escapes_text_that_looks_like_markup():
    assert html_to_md("<div>*except 9th lvl spells</div>") == r"\*except 9th lvl spells"
    assert html_to_md("<div>- not a list, just a dash</div>").startswith("\\-")


def test_html_to_md_unwraps_a_bare_li_like_the_sanitizer_does():
    # roadmap_sanitize unwraps <li> outside a list because a browser would
    # otherwise close the surrounding card; match that rather than invent a list.
    assert html_to_md("<div>a<li>b</li></div>") == "a\nb"


# --- Discord CDN links ------------------------------------------------------

CDN_BLOB = (
    "look: https://cdn.discordapp.com/attachments/1/2/image.png?ex=0&is=0&hm=0& "
    "and https://media.discordapp.net/attachments/1/2/image.png?ex=0&is=0&hm=0&"
)


def test_find_expiring_links_finds_both_cdn_hosts():
    found = find_expiring_links(CDN_BLOB)
    assert len(found) == 2
    assert found[0].startswith("https://cdn.discordapp.com/")
    assert found[1].startswith("https://media.discordapp.net/")


def test_find_expiring_links_ignores_ordinary_urls():
    assert find_expiring_links("https://homerslotr.com/x and https://discord.com/y") == []


def test_annotate_expiring_links_adds_the_may_404_note_once():
    once = annotate_expiring_links(CDN_BLOB)
    assert once.endswith(CDN_EXPIRY_NOTE)
    assert annotate_expiring_links(once) == once
    assert once.count(CDN_EXPIRY_NOTE) == 1


def test_annotate_expiring_links_leaves_clean_text_alone():
    assert annotate_expiring_links("nothing here") == "nothing here"
    assert annotate_expiring_links(None) == ""


def test_cdn_links_are_never_rehosted_only_linked():
    # The blob must come out carrying the same URL, not a copy of the bytes.
    html = ('<div><a href="https://cdn.discordapp.com/attachments/1/2/i.png?ex=0">'
            "</a></div>")
    md = html_to_md(html)
    assert md == "https://cdn.discordapp.com/attachments/1/2/i.png?ex=0"


# --- truncation -------------------------------------------------------------

EDITOR_URL = "https://roadmap.homerslotr.com/#idea-example"


def test_truncate_leaves_short_text_untouched():
    assert truncate_for_discord("short", EDITOR_URL) == "short"


def test_truncate_default_limit_is_4000():
    assert DISCORD_TEXT_LIMIT == 4000


@pytest.mark.parametrize("size", [3999, 4000, 4001, 12000])
def test_truncate_never_exceeds_the_limit(size):
    out = truncate_for_discord("word " * size, EDITOR_URL)
    assert len(out) <= DISCORD_TEXT_LIMIT


def test_truncate_keeps_a_link_back_to_the_editor():
    out = truncate_for_discord("word " * 5000, EDITOR_URL)
    assert out.endswith(EDITOR_URL)
    assert "…" in out


def test_truncate_without_a_url_still_marks_the_cut():
    out = truncate_for_discord("x" * 5000, None, limit=100)
    assert len(out) == 100
    assert out.endswith("…")


def test_truncate_prefers_a_word_boundary():
    out = truncate_for_discord("alpha beta gamma delta", None, limit=12)
    assert out == "alpha beta…"


def test_truncate_handles_none():
    assert truncate_for_discord(None, EDITOR_URL) == ""


# --- purity -----------------------------------------------------------------

def test_functions_do_not_mutate_their_input():
    src = DISCORD_NOTES[0]["notes"]
    before = str(src)
    html_to_md(src)
    md_to_html(before)
    assert src == before
