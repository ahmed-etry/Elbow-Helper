"""Small labelled contact sheets for GIF choices and sampled animation frames."""

import io

from PIL import Image, ImageDraw


def contact_sheet(frames, *, labels=None):
    if not frames:
        raise ValueError("No frames")
    columns = min(3, len(frames))
    sheet = Image.new("RGB", (columns * 240, ((len(frames) + columns - 1) // columns) * 200),
                      "white")
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        cell = frame.copy()
        cell.thumbnail((240, 180))
        left, top = index % columns * 240, index // columns * 200
        sheet.paste(cell.convert("RGB"), (left, top + 20))
        draw.text((left + 5, top + 3), labels[index] if labels else str(index + 1), fill="black")
    output = io.BytesIO()
    sheet.save(output, format="PNG")
    return output.getvalue()


def still_sheet(images):
    frames = []
    for data in images:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Image is too large")
            frames.append(image.copy())
    return contact_sheet(frames)


def animation_sheet(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("Image is too large")
        count = min(4, image.n_frames)
        if image.n_frames <= 512:
            durations = []
            for index in range(image.n_frames):
                image.seek(index)
                durations.append(max(1, image.info.get("duration", 100)))
            total = sum(durations)
            indices = []
            for sample in range(count):
                target, elapsed = total * sample / count, 0
                for index, duration in enumerate(durations):
                    elapsed += duration
                    if elapsed > target:
                        indices.append(index)
                        break
        else:
            indices = [image.n_frames * index // count for index in range(count)]
        frames = []
        for index in indices:
            image.seek(index)
            frame = image.copy()
            frame.thumbnail((240, 180))
            frames.append(frame)
        return contact_sheet(frames, labels=[f"Frame {index + 1}" for index in range(count)])
