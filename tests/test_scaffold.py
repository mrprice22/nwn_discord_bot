"""Smoke tests for the package scaffold.

These assert the shape the rest of the backlog builds on: every module named in
``[b1-scaffold]`` exists and imports cleanly, and the constants that are already
settled facts (the 12 group ids, merit by type) are present and correct.
"""

import importlib

import pytest

SUBMODULES = [
    "nwnbot",
    "nwnbot.config",
    "nwnbot.store",
    "nwnbot.roadmap",
    "nwnbot.render",
    "nwnbot.forum",
    "nwnbot.sync",
    "nwnbot.cli",
    "nwnbot.bot",
]


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_imports(name):
    module = importlib.import_module(name)
    assert module.__doc__, f"{name} should carry a module docstring"


def test_twelve_group_ids():
    from nwnbot import config

    assert len(config.GROUP_IDS) == 12
    assert len(set(config.GROUP_IDS)) == 12


def test_merit_by_type():
    from nwnbot import config

    assert config.MERIT_BY_TYPE == {"Defect": 1, "Enhancement": 2, "Exploit": 3}


def test_terminal_statuses_are_never_written_by_the_bot():
    from nwnbot import config

    assert "awarded" in config.FORBIDDEN_STATUSES
    assert "merit_awarded" in config.FORBIDDEN_FIELDS
    assert "notes" in config.FORBIDDEN_FIELDS


def test_scraper_is_gone():
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    assert not (repo_root / "scraper.py").exists()
