"""Shared image-decoding safeguards for uploads and generated derivatives."""

import warnings

from PIL import Image

MAX_DECODED_PIXELS = 60_000_000


class ImageTooLarge(ValueError):
    pass


class ImageUnreadable(ValueError):
    pass


# Pillow warns at MAX_IMAGE_PIXELS and raises only above twice that value.
# Enforce the app's limit from header dimensions before decoding any pixels;
# all image decoding goes through this helper.
def open_checked_image(path: str):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', Image.DecompressionBombWarning)
        try:
            img = Image.open(path)
        except Image.DecompressionBombError as e:
            raise ImageTooLarge(f'Image exceeds the {MAX_DECODED_PIXELS:,} pixel limit') from e
    width, height = img.size
    if width * height > MAX_DECODED_PIXELS:
        img.close()
        raise ImageTooLarge(
            f'Image is {width} x {height} pixels; the limit is {MAX_DECODED_PIXELS:,} pixels'
        )
    return img


def check_upload_image(path: str) -> None:
    try:
        with open_checked_image(path) as img:
            img.verify()
    except ImageTooLarge:
        raise
    except Exception as e:
        raise ImageUnreadable('Image appears corrupt or unreadable.') from e
