"""
Turning an uploaded picture into an avatar. Pure functions; no app, no disk.

Whatever arrives is decoded and drawn again as a small WebP. Nothing of the
uploaded file is kept: not its metadata (a phone photo carries where it was
taken), and not anything hidden in it (a file can be a valid image and
something else at once). What is stored is always a picture we made.
"""

import io
import secrets

from anyio import CapacityLimiter
from anyio.lowlevel import RunVar
from asyncer import asyncify
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.settings.security import (
    AVATAR_MAX_PIXELS,
    AVATAR_MAX_PIXELS_FULL_DECODE,
    AVATAR_PROCESSING_CONCURRENCY,
    AVATAR_SIZE,
)

# The decoders Pillow may try. A file in any other format isn't opened at
# all, whatever its name or declared type says.
ACCEPTED_FORMATS = ("PNG", "JPEG", "WEBP")
MEDIA_TYPE = "image/webp"
_QUALITY = 85

# What an avatar is called: sixteen random bytes in hexadecimal. The name
# can't be guessed from the user, and a new upload never reuses one, so a
# cached copy of an old picture can't be mistaken for the new. The pattern is
# what the file route accepts; the two are kept side by side on purpose.
_KEY_BYTES = 16
AVATAR_KEY_PATTERN = rf"^[0-9a-f]{{{_KEY_BYTES * 2}}}\.webp$"

_ORIENTATION_TAG = 0x0112
# What each value of the EXIF orientation tag asks for, to see the picture
# the right way up. 1 means "as stored".
_UPRIGHT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_TOO_MANY_PIXELS = "The picture has too many pixels"

# One limiter per event loop, as for password hashing (see core.security).
_limiter: RunVar[CapacityLimiter] = RunVar("avatar_limiter")


class InvalidImageError(Exception):
    """The upload can't be made into an avatar. The message is for the user."""


def new_avatar_key() -> str:
    return f"{secrets.token_hex(_KEY_BYTES)}.webp"


def make_avatar(data: bytes) -> bytes:
    """A square WebP of AVATAR_SIZE pixels, from a PNG, JPEG or WebP."""
    try:
        with Image.open(io.BytesIO(data), formats=ACCEPTED_FORMATS) as image:
            # Opening reads only the header. The size is checked before any
            # pixel is decoded: a few kilobytes can declare a picture that
            # needs gigabytes of memory once unpacked.
            limit = AVATAR_MAX_PIXELS if image.format == "JPEG" else AVATAR_MAX_PIXELS_FULL_DECODE
            if image.width * image.height > limit:
                raise InvalidImageError(_TOO_MANY_PIXELS)
            # A JPEG can be decoded at a fraction of its size straight away,
            # which is why it is allowed more pixels. A no-op for the others.
            image.draft("RGB", (AVATAR_SIZE * 2, AVATAR_SIZE * 2))
            # A phone stores "this was taken sideways" as metadata. It is
            # read here and applied at the end, since the metadata itself is
            # not kept.
            orientation = image.getexif().get(_ORIENTATION_TAG)
            square = ImageOps.fit(
                _in_displayable_mode(image), (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS
            )
    except InvalidImageError:
        raise
    except UnidentifiedImageError as error:
        raise InvalidImageError("The file must be a PNG, JPEG or WebP image") from error
    except Image.DecompressionBombError as error:
        # Pillow's own, far higher, limit: it refuses before we get to look.
        raise InvalidImageError(_TOO_MANY_PIXELS) from error
    except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError, SyntaxError) as error:
        # A file that starts like an image and then isn't one. The list is
        # long because a decoder fed a damaged file fails in many ways, and
        # none of them is the server's fault. `struct.error` is a ValueError.
        raise InvalidImageError("The picture is damaged and can't be read") from error

    # Turned after shrinking, when it costs nothing. Cropping the middle
    # square and turning give the same picture in either order.
    if orientation in _UPRIGHT:
        square = square.transpose(_UPRIGHT[orientation])
    output = io.BytesIO()
    square.save(output, "WEBP", quality=_QUALITY)
    return output.getvalue()


def _in_displayable_mode(image: Image.Image) -> Image.Image:
    """The picture as RGB, or RGBA if it has transparency. No copy if it already is."""
    if image.mode.startswith("I"):
        # Sixteen or thirty-two bits a pixel. Converting directly cuts each
        # value off at 255, which turns a photograph white; scale it instead.
        image = image.point(lambda value: value / 256).convert("L")
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    wanted = "RGBA" if has_alpha else "RGB"
    return image if image.mode == wanted else image.convert(wanted)


def _avatar_limiter() -> CapacityLimiter:
    try:
        return _limiter.get()
    except LookupError:
        limiter = CapacityLimiter(AVATAR_PROCESSING_CONCURRENCY)
        _limiter.set(limiter)
        return limiter


async def make_avatar_async(data: bytes) -> bytes:
    """`make_avatar` in a thread, a few at a time: decoding costs CPU and memory."""
    return await asyncify(make_avatar, limiter=_avatar_limiter())(data)
