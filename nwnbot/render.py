"""Conversion between markdown and the editor's rich-text HTML.

Filled in by ``[b4-render]``. Will hold:

- ``md_to_html()`` — the ``<div>``-per-line shape the editor's rich-text box
  produces; bold, italic, code, links and lists only, everything else escaped,
  never ``<script>`` or ``<style>``;
- ``html_to_md()`` — strips pasted Discord DOM chrome, collapses
  ``<div><span>`` nesting, keeps links and images as plain URLs.

``cdn.discordapp.com`` links are signed and expire: store them as plain links
with a note that they may 404, and never rehost. Discord-bound text is
truncated with a link back to the editor.
"""

# Discord's message limit is 2000; the plan truncates well inside an embed's
# 4096 so a link back to the editor always fits.
DISCORD_TEXT_LIMIT = 4000

# Signed, expiring attachment host — link only, never rehost.
DISCORD_CDN_HOST = "cdn.discordapp.com"

__all__ = ["DISCORD_CDN_HOST", "DISCORD_TEXT_LIMIT"]
