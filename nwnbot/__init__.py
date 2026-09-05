"""nwnbot — two-way sync between the Discord forums and the roadmap editor.

Package layout (each module is filled in by its own backlog item in ``plan.md``):

- ``config``   env + tag/group mapping and the player identity map    (b5)
- ``store``    local sqlite state: thread<->idea links, content hashes (b6)
- ``roadmap``  async HTTP client for the roadmap editor's API         (b3)
- ``render``   markdown <-> editor rich-text HTML conversion          (b4)
- ``forum``    Discord forum snapshot + action execution              (b6/b7)
- ``sync``     pure planners producing action lists, no side effects  (b6/b9)
- ``cli``      ``doctor`` / ``plan`` / ``apply`` / ``backfill`` / ``serve`` (b7)
- ``bot``      the long-running ``discord.Client`` runtime            (b7)

Nothing here talks to Discord or the roadmap at import time; every network
client is constructed explicitly by the CLI or the bot runtime.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
