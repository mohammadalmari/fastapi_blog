"""Pictures made in memory for the avatar tests. Not fixtures: plain helpers."""

import io

from PIL import Image

# One colour for every picture, in each mode's own terms.
_COLOUR: dict[str, int | tuple[int, ...]] = {
    "L": 120,
    "LA": (120, 128),
    "RGB": (200, 30, 30),
    "RGBA": (200, 30, 30, 128),
    "CMYK": (0, 200, 200, 30),
    "P": 1,
    "1": 1,
}


def picture(
    kind: str = "PNG",
    size: tuple[int, int] = (640, 480),
    *,
    mode: str = "RGB",
    exif: Image.Exif | None = None,
) -> bytes:
    """A real image file of the given format, as bytes."""
    image = Image.new(mode, size, color=_COLOUR[mode])
    output = io.BytesIO()
    if exif is not None:
        image.save(output, kind, exif=exif)
    else:
        image.save(output, kind)
    return output.getvalue()


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def pixel(image: Image.Image, at: tuple[int, int]) -> tuple[int, ...]:
    """The colour at a point, always as a tuple, whatever the picture's mode."""
    value = image.getpixel(at)
    if isinstance(value, tuple):
        return tuple(int(channel) for channel in value)
    assert value is not None
    return (int(value),)
