"""Unit tests for turning an upload into an avatar. No app, no database."""

import io
from pathlib import Path

import pytest
from PIL import Image

from app.core.settings.security import AVATAR_MAX_PIXELS, AVATAR_MAX_PIXELS_FULL_DECODE
from app.users.avatars import InvalidImageError, make_avatar
from tests.users.pictures import opened, picture, pixel

GPS_INFO = 0x8825  # the EXIF tag under which a camera stores where it was
ORIENTATION = 0x0112


@pytest.mark.parametrize("kind", ["PNG", "JPEG", "WEBP"])
def test_each_accepted_format_becomes_a_small_square_webp(kind: str) -> None:
    avatar = opened(make_avatar(picture(kind, (640, 480))))

    assert (avatar.format, avatar.size) == ("WEBP", (256, 256))


@pytest.mark.parametrize("size", [(16, 16), (2000, 100), (100, 2000)])
def test_any_shape_becomes_the_same_square(size: tuple[int, int]) -> None:
    assert opened(make_avatar(picture("PNG", size))).size == (256, 256)


def test_transparency_is_kept() -> None:
    avatar = opened(make_avatar(picture("PNG", mode="RGBA")))

    assert avatar.mode == "RGBA"
    assert pixel(avatar, (0, 0))[3] < 255


def test_where_a_photo_was_taken_is_not_kept() -> None:
    """The privacy leak this guards: phone photos carry GPS coordinates."""
    exif = Image.Exif()
    exif[GPS_INFO] = {1: "N", 2: (31.0, 57.0, 0.0)}
    exif[0x010F] = "A camera maker"
    upload = picture("JPEG", exif=exif)
    assert opened(upload).getexif()  # the upload really carries it

    avatar = opened(make_avatar(upload))

    assert not avatar.getexif()
    assert "exif" not in avatar.info


def test_a_photo_taken_sideways_is_turned_upright() -> None:
    exif = Image.Exif()
    exif[ORIENTATION] = 6  # "rotate a quarter turn to view"
    image = Image.new("RGB", (400, 200))
    image.paste((255, 0, 0), (0, 0, 200, 200))  # red on the left as stored
    output = io.BytesIO()
    image.save(output, "PNG", exif=exif)

    avatar = opened(make_avatar(output.getvalue())).convert("RGB")

    # Turned, the red half is on top.
    top, bottom = pixel(avatar, (128, 20)), pixel(avatar, (128, 236))
    assert top[0] > 200
    assert bottom[0] < 50


@pytest.mark.parametrize(
    "upload",
    [
        b"",
        b"just some text",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"GIF89a" + b"\x00" * 64,
        b"%PDF-1.7 not a picture",
    ],
    ids=["empty", "text", "svg", "gif-header", "pdf"],
)
def test_what_is_not_a_picture_is_refused(upload: bytes) -> None:
    with pytest.raises(InvalidImageError, match="PNG, JPEG or WebP"):
        make_avatar(upload)


@pytest.mark.parametrize("kind", ["GIF", "BMP", "TIFF"])
def test_a_real_picture_in_another_format_is_refused(kind: str) -> None:
    with pytest.raises(InvalidImageError, match="PNG, JPEG or WebP"):
        make_avatar(picture(kind))


def test_a_picture_cut_short_is_refused() -> None:
    upload = picture("PNG", (640, 480))

    with pytest.raises(InvalidImageError, match="damaged"):
        make_avatar(upload[: len(upload) // 2])


def test_a_small_file_declaring_a_huge_picture_is_refused_before_decoding() -> None:
    """
    The attack: a "decompression bomb". One flat colour compresses to almost
    nothing, so a few kilobytes can unpack into gigabytes of pixels.
    """
    side = int(AVATAR_MAX_PIXELS**0.5) + 1
    bomb = picture("JPEG", (side, side), mode="L")
    assert len(bomb) < 200_000

    with pytest.raises(InvalidImageError, match="too many pixels"):
        make_avatar(bomb)


@pytest.mark.parametrize(
    ("kind", "limit"),
    [("JPEG", AVATAR_MAX_PIXELS), ("PNG", AVATAR_MAX_PIXELS_FULL_DECODE)],
)
def test_the_largest_picture_allowed_is_accepted(kind: str, limit: int) -> None:
    side = int(limit**0.5)

    assert opened(make_avatar(picture(kind, (side, side), mode="L"))).size == (256, 256)


def test_a_picture_with_damaged_metadata_is_not_a_crash() -> None:
    """
    Found by feeding the decoder thousands of damaged files. This one is a
    fine picture whose EXIF block is nonsense; rewriting that block, which
    turning the picture used to involve, raised an error nothing caught.
    """
    upload = (Path(__file__).parent / "broken_exif.jpg").read_bytes()

    avatar = opened(make_avatar(upload))

    assert avatar.size == (256, 256)


@pytest.mark.parametrize(
    ("kind", "mode"),
    [("PNG", "L"), ("PNG", "LA"), ("PNG", "P"), ("PNG", "1"), ("JPEG", "CMYK"), ("JPEG", "L")],
)
def test_pictures_stored_in_less_common_ways_are_accepted(kind: str, mode: str) -> None:
    avatar = opened(make_avatar(picture(kind, mode=mode)))

    assert avatar.size == (256, 256)
    assert avatar.mode == ("RGBA" if mode == "LA" else "RGB")


def test_a_sixteen_bit_greyscale_picture_keeps_its_shades() -> None:
    image = Image.new("I;16", (64, 64), color=20000)  # a dark grey, out of 65535
    output = io.BytesIO()
    image.save(output, "PNG")

    avatar = opened(make_avatar(output.getvalue())).convert("L")

    assert 60 < pixel(avatar, (32, 32))[0] < 100


@pytest.mark.parametrize("kind", ["PNG", "WEBP"])
def test_formats_decoded_at_full_size_have_a_lower_pixel_limit(kind: str) -> None:
    """
    A flat colour of 4096 x 4096 is under a kilobyte as WebP and needed
    about 460 MB to decode. A JPEG is decoded at a fraction of its size, so
    it alone keeps the larger limit.
    """
    side = int(AVATAR_MAX_PIXELS_FULL_DECODE**0.5) + 1

    with pytest.raises(InvalidImageError, match="too many pixels"):
        make_avatar(picture(kind, (side, side)))

    assert opened(make_avatar(picture("JPEG", (side, side)))).size == (256, 256)
