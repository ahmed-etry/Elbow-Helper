"""Small synthetic stills shared by image-input tests."""

import io

from PIL import Image


def png(colour):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), colour).save(output, format="PNG")
    return output.getvalue()
