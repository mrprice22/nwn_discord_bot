"""Tests for rehosting: the transcode, the key, and the store."""

import io

import pytest

from nwnbot import attachments as att


def png(size=(40, 30), mode="RGB", colour=(200, 40, 40)):
    from PIL import Image

    image = Image.new(mode, size, colour if mode != "RGBA" else colour + (255,))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def dims(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def fmt(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.format


# --------------------------------------------------------------------------
# transcode
# --------------------------------------------------------------------------

def test_transcode_produces_webp():
    assert fmt(att.transcode(png())) == "WEBP"


def test_a_small_image_is_not_upscaled():
    assert dims(att.transcode(png((40, 30)))) == (40, 30)


def test_a_large_image_is_downscaled_to_the_long_edge():
    out = att.transcode(png((4000, 2000)), max_edge=1600)
    assert max(dims(out)) == 1600
    assert dims(out) == (1600, 800)          # aspect ratio preserved


def test_a_tall_image_is_bounded_by_its_long_edge_too():
    assert max(dims(att.transcode(png((500, 4000)), max_edge=1600))) == 1600


def test_transcode_shrinks_a_real_sized_screenshot():
    # The whole point of the module: this is the ratio that decides whether
    # rehosting is affordable at all.
    source = png((1920, 1080))
    assert len(att.transcode(source)) < len(source)


def test_transparency_is_flattened_onto_white():
    # Not cosmetic: the roadmap card background has been both white and black
    # in the same week, and alpha would render differently on each.
    out = att.transcode(png((20, 20), mode="RGBA"))
    from PIL import Image

    with Image.open(io.BytesIO(out)) as image:
        assert image.mode in ("RGB", "RGBX")


def test_empty_bytes_raise():
    with pytest.raises(att.TranscodeError):
        att.transcode(b"")


def test_non_image_bytes_raise_rather_than_pass_through():
    # Must NOT return the input: storing arbitrary bytes under a .webp name
    # would be indistinguishable from a successful rehost.
    with pytest.raises(att.TranscodeError):
        att.transcode(b"this is not an image, it is a sentence")


def test_an_oversized_source_is_refused_before_decoding():
    with pytest.raises(att.TranscodeError) as e:
        att.transcode(b"x" * (att.MAX_SOURCE_BYTES + 1))
    assert "over the" in str(e.value)


def test_a_truncated_image_raises():
    broken = png((100, 100))[:120]
    with pytest.raises(att.TranscodeError):
        att.transcode(broken)


# --------------------------------------------------------------------------
# key_for
# --------------------------------------------------------------------------

def test_the_key_is_content_addressed():
    data = att.transcode(png())
    assert att.key_for(data) == att.key_for(data)


def test_different_content_gets_a_different_key():
    a = att.transcode(png(colour=(10, 10, 200)))
    b = att.transcode(png(colour=(200, 10, 10)))
    assert att.key_for(a) != att.key_for(b)


def test_the_key_carries_no_user_supplied_filename():
    # Filenames come from a public forum; they are not worth republishing.
    key = att.key_for(att.transcode(png()))
    assert "png" not in key and key.endswith(".webp")


def test_the_key_is_sharded_so_one_directory_never_holds_everything():
    key = att.key_for(att.transcode(png()))
    prefix, shard, name = key.split("/")
    assert prefix == "discord" and len(shard) == 2 and name.startswith(shard)


# --------------------------------------------------------------------------
# looks_like_image
# --------------------------------------------------------------------------

def test_content_type_is_trusted_first():
    assert att.looks_like_image("image/png", "notes.txt") is True
    assert att.looks_like_image("application/pdf", "shot.png") is False


def test_the_extension_is_the_fallback_when_no_type_is_given():
    assert att.looks_like_image("", "shot.PNG") is True
    assert att.looks_like_image("", "save.zip") is False
    assert att.looks_like_image("", "") is False


# --------------------------------------------------------------------------
# LocalStore and store_image
# --------------------------------------------------------------------------

def test_local_store_round_trips(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example/x")
    url = store.put("discord/ab/abcd.webp", b"bytes", "image/webp")
    assert url == "https://img.example/x/discord/ab/abcd.webp"
    assert (tmp_path / "discord/ab/abcd.webp").read_bytes() == b"bytes"
    assert store.exists("discord/ab/abcd.webp") is True


def test_a_key_cannot_escape_the_store_root(tmp_path):
    store = att.LocalStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("../../etc/passwd", b"x", "image/webp")


def test_store_image_transcodes_and_returns_a_url(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")
    url, size = att.store_image(png((800, 600)), store)
    assert url.startswith("https://img.example/discord/")
    assert url.endswith(".webp") and size > 0


def test_storing_the_same_image_twice_uploads_once(tmp_path):
    calls = []

    class Counting(att.LocalStore):
        def put(self, key, data, content_type):
            calls.append(key)
            return super().put(key, data, content_type)

    store = Counting(tmp_path, base_url="https://img.example")
    first, _ = att.store_image(png(), store)
    second, _ = att.store_image(png(), store)
    assert first == second
    assert len(calls) == 1        # the retry is free, not a duplicate object


# --------------------------------------------------------------------------
# rehost_images: the I/O step that runs BEFORE planning, so planners stay pure.
# --------------------------------------------------------------------------
import pytest_asyncio  # noqa: E402,F401
import pytest as _pytest  # noqa: E402

from nwnbot.bot import rehost_images  # noqa: E402
from nwnbot.forum import (Attachment, ForumMessage, ForumSnapshot,  # noqa: E402
                          ForumThread)


def _snapshot(*atts, content="look"):
    starter = ForumMessage(id="m-1", author_id="u-1", content=content,
                           is_starter=True, attachments=atts)
    return ForumSnapshot((ForumThread(id="t-1", channel_id="c-1", title="T",
                                      author_id="u-1", starter=starter),))


def _img(**kw):
    kw.setdefault("content_type", "image/png")
    kw.setdefault("url", "https://cdn.discordapp.com/a/b/c.png?ex=1&hm=2")
    return Attachment(id=kw.pop("id", "a-1"), **kw)


def _atts_of(snap):
    return snap.threads[0].starter.attachments


@_pytest.mark.asyncio
async def test_rehosting_fills_in_the_permanent_url(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")

    async def fetch(url):
        return png()

    out = await rehost_images(_snapshot(_img()), store, fetch=fetch)
    url = _atts_of(out)[0].rehosted_url
    assert url.startswith("https://img.example/discord/") and url.endswith(".webp")


@_pytest.mark.asyncio
async def test_a_fetch_failure_leaves_the_image_un_rehosted(tmp_path):
    # The report is worth more than the screenshot: one bad image must not
    # take down the run, and must NOT fall back to the signed url.
    store = att.LocalStore(tmp_path, base_url="https://img.example")

    async def fetch(url):
        raise OSError("connection reset")

    out = await rehost_images(_snapshot(_img()), store, fetch=fetch)
    item = _atts_of(out)[0]
    assert item.rehosted_url == ""
    assert item.permanent_url == ""          # never the signed link


@_pytest.mark.asyncio
async def test_undecodable_bytes_leave_the_image_un_rehosted(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")

    async def fetch(url):
        return b"not an image at all"

    out = await rehost_images(_snapshot(_img()), store, fetch=fetch)
    assert _atts_of(out)[0].rehosted_url == ""


@_pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_others(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")
    good = "https://cdn.discordapp.com/good.png?ex=1"

    async def fetch(url):
        if url == good:
            return png()
        raise OSError("nope")

    snap = _snapshot(_img(id="bad", url="https://cdn.discordapp.com/bad.png?ex=1"),
                     _img(id="good", url=good))
    out = await rehost_images(snap, store, fetch=fetch)
    bad, ok = _atts_of(out)
    assert bad.rehosted_url == "" and ok.rehosted_url != ""


@_pytest.mark.asyncio
async def test_the_same_url_is_fetched_once(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")
    seen = []

    async def fetch(url):
        seen.append(url)
        return png()

    same = "https://cdn.discordapp.com/same.png?ex=1"
    out = await rehost_images(_snapshot(_img(id="a", url=same),
                                        _img(id="b", url=same)), store, fetch=fetch)
    assert len(seen) == 1
    a, b = _atts_of(out)
    assert a.rehosted_url == b.rehosted_url != ""


@_pytest.mark.asyncio
async def test_an_already_rehosted_image_is_not_fetched_again(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")
    seen = []

    async def fetch(url):
        seen.append(url)
        return png()

    snap = _snapshot(_img(rehosted_url="https://img.example/already.webp"))
    out = await rehost_images(snap, store, fetch=fetch)
    assert seen == []
    assert _atts_of(out)[0].rehosted_url == "https://img.example/already.webp"


@_pytest.mark.asyncio
async def test_non_images_are_left_alone(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")
    seen = []

    async def fetch(url):
        seen.append(url)
        return png()

    snap = _snapshot(Attachment(id="z", filename="save.zip",
                                content_type="application/zip",
                                url="https://cdn.discordapp.com/z.zip?ex=1"))
    await rehost_images(snap, store, fetch=fetch)
    assert seen == []


@_pytest.mark.asyncio
async def test_a_snapshot_with_nothing_to_do_is_returned_unchanged(tmp_path):
    store = att.LocalStore(tmp_path, base_url="https://img.example")
    snap = _snapshot()

    async def fetch(url):  # pragma: no cover - must not be called
        raise AssertionError("should not fetch")

    assert await rehost_images(snap, store, fetch=fetch) == snap
