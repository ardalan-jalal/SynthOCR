"""RAQM shaping, bounded font reuse, and deterministic Pillow augmentation."""

import hashlib
import math
import random
import unicodedata
from collections import OrderedDict
from pathlib import Path

from PIL import (
    Image,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    features,
)

from .config import (
    Generation,
    background_distribution,
    document_layers,
    resolve_document_settings,
    validate_augmentation,
)
from .downloads import sha256
from .fonts import Font

AUGMENT_MARGIN = 4  # Room for bicubic resampling and the blur halo.


def _color(value: str, mode: str):
    """Match Pillow's RGB-to-luminance conversion without a full image buffer."""
    red, green, blue = ImageColor.getrgb(value)
    if mode == "RGB":
        return red, green, blue
    return (red * 299 + green * 587 + blue * 114 + 500) // 1000


def _background(dimensions: tuple[int, int], settings: Generation, rng: random.Random):
    """Build a background over the complete canvas using bounded noise buffers."""
    mode = "RGB" if settings.color_mode == "rgb" else "L"
    start = _color(settings.background_color, mode)
    if settings.background == "plain":
        return Image.new(mode, dimensions, start)
    if settings.background == "gradient":
        end = _color(settings.background_color_end, mode)
        # A fixed-size ramp avoids a Python array proportional to image height.
        ramp = Image.new(mode, (1, 256))
        if mode == "RGB":
            ramp.putdata(
                [
                    tuple(round(a + (b - a) * index / 255) for a, b in zip(start, end, strict=True))
                    for index in range(256)
                ]
            )
        else:
            ramp.putdata([round(start + (end - start) * index / 255) for index in range(256)])
        try:
            return ramp.resize(dimensions, Image.Resampling.BILINEAR)
        finally:
            ramp.close()
    if not settings.texture_strength:
        return Image.new(mode, dimensions, start)
    if settings.background == "noise":
        # Fill independent tiles, rather than stretching grain into visible blocks
        # on wide lines or allocating a full-image temporary bytes object.
        texture = Image.new("L", dimensions)
        for top in range(0, dimensions[1], 256):
            for left in range(0, dimensions[0], 256):
                tile_size = (min(256, dimensions[0] - left), min(256, dimensions[1] - top))
                tile = Image.frombytes("L", tile_size, rng.randbytes(math.prod(tile_size)))
                texture.paste(tile, (left, top))
                tile.close()
    else:
        texture_size = tuple(max(1, min(256, size // 8)) for size in dimensions)
        source = Image.frombytes("L", texture_size, rng.randbytes(math.prod(texture_size)))
        try:
            texture = source.resize(dimensions, Image.Resampling.BILINEAR)
        finally:
            source.close()
    spread = 255 * settings.texture_strength / 2
    channels = start if mode == "RGB" else (start,)
    low = tuple(max(0, round(channel - spread)) for channel in channels)
    high = tuple(min(255, round(channel + spread)) for channel in channels)
    try:
        if mode == "RGB":
            return ImageOps.colorize(texture, low, high)
        return texture.point(
            [round(low[0] + (high[0] - low[0]) * value / 255) for value in range(256)]
        )
    finally:
        texture.close()


def _effect_rng(seed: int, effect: str) -> random.Random:
    """A layer consumes only its own stream, keeping comparisons matched."""
    digest = hashlib.sha256(f"{seed}:{effect}".encode()).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def _mask_size(dimensions: tuple[int, int]) -> tuple[int, int]:
    return tuple(max(8, min(256, math.ceil(size / 4))) for size in dimensions)


def _apply_mask(image: Image.Image, mask: Image.Image, opacity: float, color=0) -> None:
    """Consume a small mask, resize after scaling alpha, and release it promptly."""
    if opacity == 1:
        scaled = mask
    else:
        scaled = mask.point([round(value * opacity) for value in range(256)])
        mask.close()
    if scaled.size != image.size:
        try:
            expanded = scaled.resize(image.size, Image.Resampling.BILINEAR)
        finally:
            scaled.close()
    else:
        expanded = scaled
    try:
        image.paste(color, (0, 0, image.width, image.height), expanded)
    finally:
        expanded.close()


def _document_background(image: Image.Image, layers: dict[str, float], seed: int, font_size: int):
    """Compose document layers before ink, with at most 256² source buffers.

    Layers shade a complete canvas rather than a rectangle behind the text. Ink
    is pasted afterwards, so stains, ruling and folds cannot erase characters.
    """
    dimensions = image.size
    small = _mask_size(dimensions)
    if strength := layers["paper_texture"]:
        rng = _effect_rng(seed, "paper_texture")
        # Coarse pulp density, fine mottling and short fibers occupy different
        # scales. One stretched noise texture makes paper look like a cloudy
        # rectangle, and loses all fine detail on long lines.
        for scale, opacity in ((8, 0.12), (1, 0.08)):
            size = tuple(max(2, value // scale) for value in small)
            source = Image.frombytes("L", size, rng.randbytes(math.prod(size)))
            try:
                mask = source.filter(ImageFilter.GaussianBlur(0.6))
            finally:
                source.close()
            _apply_mask(image, mask, strength * opacity)
        fibers = Image.new("L", dimensions)
        draw = ImageDraw.Draw(fibers)
        for _ in range(min(4000, max(20, math.prod(dimensions) // 100))):
            x, y = rng.randrange(dimensions[0]), rng.randrange(dimensions[1])
            length = rng.uniform(2, 8)
            angle = rng.uniform(-0.6, 0.6)
            draw.line(
                (x, y, x + length * math.cos(angle), y + length * math.sin(angle)),
                fill=rng.randrange(50, 170),
                width=1,
            )
        _apply_mask(image, fibers, strength * 0.2)
    if strength := layers["shadow"]:
        rng = _effect_rng(seed, "shadow")
        angle = rng.uniform(0, math.tau)
        dx, dy = math.cos(angle), math.sin(angle)
        scale = max(abs(dx) + abs(dy), 0.01)
        # A broad off-center light source plus a smooth directional falloff,
        # continuous across the full canvas rather than repeated on tiles.
        center_x, center_y = rng.uniform(-0.4, 0.4), rng.uniform(-0.4, 0.4)
        pixels = bytearray(math.prod(small))
        for y in range(small[1]):
            ny = 2 * y / max(1, small[1] - 1) - 1
            for x in range(small[0]):
                nx = 2 * x / max(1, small[0] - 1) - 1
                directional = (nx * dx + ny * dy + scale) / (2 * scale)
                directional = directional * directional * (3 - 2 * directional)
                edge = min(1, ((nx - center_x) ** 2 + (ny - center_y) ** 2) / 2)
                pixels[y * small[0] + x] = round(255 * (0.72 * directional + 0.28 * edge))
        _apply_mask(image, Image.frombytes("L", small, bytes(pixels)), strength * 0.44)
    if strength := layers["stains"]:
        rng = _effect_rng(seed, "stains")
        mask = Image.new("L", small)
        draw = ImageDraw.Draw(mask)
        for _ in range(rng.randrange(3, 7)):
            x, y = rng.uniform(-0.2, 1.2) * small[0], rng.uniform(-0.2, 1.2) * small[1]
            rx, ry = rng.uniform(0.08, 0.35) * small[0], rng.uniform(0.12, 0.65) * small[1]
            radii = [rng.uniform(0.65, 1.25) for _ in range(16)]
            # Uneven wet edges, with a faint darker tide mark surrounding a
            # softer interior. Fixed 16-point polygons keep allocations small.
            for inset, fill in ((1, rng.randint(100, 180)), (0.78, rng.randint(55, 105))):
                draw.polygon(
                    [
                        (
                            x + math.cos(index * math.tau / 16) * rx * radius * inset,
                            y + math.sin(index * math.tau / 16) * ry * radius * inset,
                        )
                        for index, radius in enumerate(radii)
                    ],
                    fill=fill,
                )
        original = mask
        mask = original.filter(ImageFilter.GaussianBlur(max(0.6, min(small) / 30)))
        original.close()
        _apply_mask(image, mask, strength * 0.3, (112, 75, 36) if image.mode == "RGB" else 80)
    if strength := layers["creases"]:
        rng = _effect_rng(seed, "creases")
        mask = Image.new("L", small)
        highlight = Image.new("L", small)
        draw, light = ImageDraw.Draw(mask), ImageDraw.Draw(highlight)
        for _ in range(5):
            start = (rng.uniform(-0.15, 1.15) * small[0], -0.1 * small[1])
            end = (rng.uniform(-0.15, 1.15) * small[0], 1.1 * small[1])
            midpoint = (
                (start[0] + end[0]) / 2 + rng.uniform(-0.08, 0.08) * small[0],
                rng.uniform(0.3, 0.7) * small[1],
            )
            points = (start, midpoint, end)
            draw.line(points, fill=rng.randrange(110, 220), width=2, joint="curve")
            light.line(
                [(x + 1.5, y + 0.8) for x, y in points],
                fill=180,
                width=1,
                joint="curve",
            )
        original, original_light = mask, highlight
        mask = original.filter(ImageFilter.GaussianBlur(0.8))
        highlight = original_light.filter(ImageFilter.GaussianBlur(0.6))
        original.close()
        original_light.close()
        _apply_mask(image, mask, strength * 0.27)
        _apply_mask(
            image, highlight, strength * 0.35, (255, 255, 255) if image.mode == "RGB" else 255
        )
    if strength := layers["ruled_lines"]:
        rng = _effect_rng(seed, "ruled_lines")
        # Keep actual line spacing tied to type size, rather than to a stretched
        # low-resolution source. This mask is only the current image, not data.
        mask = Image.new("L", dimensions)
        draw = ImageDraw.Draw(mask)
        spacing = max(12, round(font_size * 0.66))
        offset = rng.randrange(spacing)
        for y in range(offset, dimensions[1], spacing):
            draw.line((0, y, dimensions[0], y), fill=round(255 * strength * 0.35), width=1)
        _apply_mask(image, mask, 1, (95, 139, 170) if image.mode == "RGB" else 128)
    if strength := layers["binding_shadow"]:
        rng = _effect_rng(seed, "binding_shadow")
        right = rng.choice((False, True))
        width = rng.uniform(0.055, 0.13)
        pixels = bytearray(math.prod(small))
        for y in range(small[1]):
            # The bend varies slowly down the gutter; the opposite edge stays
            # lit. This shades only the paper, without inventing warped labels.
            curvature = 0.92 + 0.08 * math.cos(y / max(1, small[1] - 1) * math.pi)
            for x in range(small[0]):
                distance = x / max(1, small[0] - 1)
                if right:
                    distance = 1 - distance
                pixels[y * small[0] + x] = round(255 * math.exp(-distance / (width * curvature)))
        _apply_mask(image, Image.frombytes("L", small, bytes(pixels)), strength * 0.52)
    if strength := layers["scan_banding"]:
        rng = _effect_rng(seed, "scan_banding")
        # Paper-feed / scan-density bands run horizontally at actual pixel
        # scale. A small smooth ramp and sparse one-pixel rows are distinct
        # from ruled writing lines and do not allocate a full-image byte array.
        ramp = Image.frombytes("L", (1, 16), rng.randbytes(16))
        _apply_mask(image, ramp, strength * 0.09)
        mask = Image.new("L", dimensions)
        draw = ImageDraw.Draw(mask)
        step = max(3, round(font_size / 10))
        for y in range(rng.randrange(step), dimensions[1], step):
            draw.line((0, y, dimensions[0], y), fill=rng.randrange(40, 160), width=1)
        _apply_mask(image, mask, strength * 0.2)
    if strength := layers["speckles"]:
        rng = _effect_rng(seed, "speckles")
        mask = Image.new("L", dimensions)
        draw = ImageDraw.Draw(mask)
        # Fine irregular paper/toner flecks, sparsely distributed; sample
        # coordinates one at a time instead of retaining a particle array.
        for _ in range(min(5000, max(8, math.prod(dimensions) // 120))):
            x, y = rng.randrange(dimensions[0]), rng.randrange(dimensions[1])
            draw.line(
                (x, y, x + rng.randrange(1, 4), y + rng.choice((-1, 0, 1))),
                fill=rng.randrange(65, 225),
                width=1,
            )
        _apply_mask(image, mask, strength * 0.55, (80, 67, 47) if image.mode == "RGB" else 60)
    if strength := layers["grain"]:
        rng = _effect_rng(seed, "grain")
        # Independent 256² byte chunks preserve fine grain on wide text lines
        # without stretching noise or constructing a full-image Python array.
        table = [round(value * strength * 0.16) for value in range(256)]
        for top in range(0, dimensions[1], 256):
            for left in range(0, dimensions[0], 256):
                size = (min(256, dimensions[0] - left), min(256, dimensions[1] - top))
                source = Image.frombytes("L", size, rng.randbytes(math.prod(size)))
                mask = source.point(table)
                source.close()
                try:
                    image.paste(0, (left, top, left + size[0], top + size[1]), mask)
                finally:
                    mask.close()
    return image


def require_shaping():
    if not features.check_feature("raqm"):
        raise ValueError(
            "Pillow requires libraqm for reliable multilingual shaping. "
            "Install an official Pillow wheel or build Pillow with libraqm."
        )


def direction_for(text: str, direction: str) -> str:
    if direction != "auto":
        return direction
    for char in text:
        bidi = unicodedata.bidirectional(char)
        if bidi in {"R", "AL"}:
            return "rtl"
        if bidi == "L":
            return "ltr"
    return "ltr"


class Renderer:
    def __init__(self, settings: Generation, cache_size: int = 64):
        validate_augmentation(settings)
        require_shaping()
        self.settings = settings
        self.background_distribution = background_distribution(settings)
        self.cache_size = cache_size
        self.cache: OrderedDict[tuple[Font, int], ImageFont.FreeTypeFont] = OrderedDict()

    def font(self, face: Font, size: int):
        key = (face, size)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        value = ImageFont.truetype(
            face.path, size, index=face.index, layout_engine=ImageFont.Layout.RAQM
        )
        self.cache[key] = value
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)
        return value

    def _background_preset(self, seed: int) -> str:
        rng = _effect_rng(seed, "preset")
        if not self.settings.background_weights:
            # Preserve existing datasets' seeded choices when weights are omitted.
            return rng.choice(self.settings.background_presets)
        return rng.choices(
            self.settings.background_presets,
            weights=list(self.background_distribution.values()),
            k=1,
        )[0]

    def render(self, text: str, face: Font, seed: int, language: str = ""):
        g = self.settings
        rng = random.Random(seed)
        augment = rng.random() < g.augmentation
        preset = self._background_preset(seed) if augment and g.background_presets else ""
        appearance = resolve_document_settings(g, preset)
        layers = document_layers(g, preset)
        angle = rng.uniform(-g.rotation, g.rotation) if augment else 0
        # Sample independent effects before background generation consumes random
        # bytes, so changing the background alone keeps blur and contrast matched.
        blur = rng.uniform(0, g.blur) if augment else 0
        contrast = rng.uniform(1 - g.contrast, 1 + g.contrast) if augment else 1
        padding = (
            max(g.padding, AUGMENT_MARGIN, math.ceil(3 * g.blur) + 2) if augment else g.padding
        )
        if 2 * padding >= min(g.max_width, g.max_height):
            raise ValueError(
                "Image bounds leave no text area inside the augmentation safety margin"
            )
        direction = direction_for(text, g.direction)
        layout = {"direction": direction}
        if g.language or language:
            layout["language"] = g.language or language
        size = g.font_size
        # A fixed point size is both faster and typographically consistent across lines.
        # Reduce oversized lines before allocating an image; never crop the transcription.
        for _ in range(8):
            font = self.font(face, size)
            left, top, right, bottom = font.getbbox(text, **layout)
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                raise ValueError("Text has no visible ink")
            ratio = min(
                (g.max_width - 2 * padding) / width, (g.max_height - 2 * padding) / height, 1
            )
            outer_width, outer_height = width + 2 * padding, height + 2 * padding
            if augment:
                sine = abs(math.sin(math.radians(angle)))
                cosine = abs(math.cos(math.radians(angle)))
                rotated_width = math.ceil(outer_width * cosine + outer_height * sine) + 2
                rotated_height = math.ceil(outer_height * cosine + outer_width * sine) + 2
            else:
                rotated_width, rotated_height = outer_width, outer_height
            ratio = min(
                ratio,
                g.max_width / rotated_width,
                g.max_height / rotated_height,
                math.sqrt(g.max_pixels / (rotated_width * rotated_height)),
            )
            if ratio == 1:
                break
            next_size = max(1, int(size * ratio))
            if next_size >= size:
                raise ValueError("Text cannot fit the configured image bounds")
            size = next_size
        else:
            raise ValueError("Text cannot fit the configured image bounds")
        dimensions = (width + 2 * padding, height + 2 * padding)
        if dimensions[0] * dimensions[1] > g.max_pixels:
            raise ValueError("Rendered image exceeds max_pixels")
        mask = Image.new("L", dimensions, 0)
        ImageDraw.Draw(mask).text(
            (padding - left, padding - top), text, font=font, fill=255, **layout
        )
        augmentation = {}
        if augment:
            # Skew only the ink. Generate paper over the expanded canvas so
            # rotation never exposes a solid-white frame around the texture.
            original = mask
            mask = original.rotate(
                angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=0
            )
            original.close()
            image = _background(mask.size, appearance, rng)
            _document_background(image, layers, seed, size)
            image.paste(_color(appearance.text_color, image.mode), mask=mask)
            if blur:
                original, original_mask = image, mask
                image = original.filter(ImageFilter.GaussianBlur(blur))
                mask = original_mask.filter(ImageFilter.GaussianBlur(blur))
                original.close()
                original_mask.close()
            if contrast != 1:
                original = image
                image = ImageEnhance.Contrast(original).enhance(contrast)
                original.close()
            augmentation = {
                "background": appearance.background,
                "color_mode": appearance.color_mode,
                "background_color": appearance.background_color,
                "text_color": appearance.text_color,
                "texture_strength": (
                    appearance.texture_strength
                    if appearance.background in {"paper", "noise"}
                    else 0
                ),
                "background_preset": preset,
                "layers": layers,
                "rotation": angle,
                "blur": blur,
                "contrast": contrast,
            }
            if preset:
                augmentation["background_probability"] = self.background_distribution[preset]
            if appearance.background == "gradient":
                augmentation["background_color_end"] = appearance.background_color_end
        else:
            image = ImageOps.invert(mask)
        if (
            image.width > g.max_width
            or image.height > g.max_height
            or image.width * image.height > g.max_pixels
        ):
            image.close()
            mask.close()
            raise ValueError(
                "Augmented image exceeds configured bounds; increase bounds or padding"
            )
        bbox = mask.getbbox()
        mask.close()
        if bbox is None:
            image.close()
            raise ValueError("Font rendered no visible ink")
        if augment and (
            bbox[0] == 0 or bbox[1] == 0 or bbox[2] == image.width or bbox[3] == image.height
        ):
            image.close()
            raise ValueError("Augmented ink reaches the canvas edge; increase padding or bounds")
        return image, {
            "width": image.width,
            "height": image.height,
            "bbox": list(bbox),
            "font_size": size,
            "direction": direction,
            "language": layout.get("language", ""),
            "augmentation": augmentation,
        }


_renderer: Renderer | None = None


def initialize(settings: Generation, cache_size: int):
    global _renderer
    _renderer = Renderer(settings, cache_size)


def release():
    """Release serial native font objects when a generation job ends."""
    global _renderer
    _renderer = None


def render_task(task: dict) -> dict:
    if _renderer is None:
        raise RuntimeError("Renderer is not initialized")
    image, info = _renderer.render(task["text"], task["face"], task["seed"], task["language"])
    path = Path(task["destination"])
    try:
        options = {"compress_level": 1} if path.suffix == ".png" else {}
        if path.suffix == ".jpg":
            options = {"quality": 95}
        image.save(path, **options)
    finally:
        image.close()
    face = task["face"]
    # A shaping hint must not relabel the source language in annotations/exports.
    info["shaping_language"] = info["language"]
    info["language"] = task["language"]
    # Hash source labels for deterministic, disjoint text splits across font variants.
    settings = _renderer.settings
    split_value = (
        int.from_bytes(hashlib.sha256(f"{settings.seed}:{task['text']}".encode()).digest()[:8])
        / 2**64
    )
    split = (
        "train"
        if split_value < settings.train_ratio
        else "validation"
        if split_value < settings.train_ratio + settings.validation_ratio
        else "test"
    )
    return {
        "id": task["id"],
        "image": f"images/{path.name}",
        "text": task["text"],
        "text_id": task["text_id"],
        "font": task["font_name"],
        "font_id": task["font_id"],
        "face_index": face.index,
        "source": task["source"],
        "source_line": task["line"],
        "genre": task["genre"],
        "split": split,
        "attribution": task["attribution"],
        "image_sha256": sha256(path),
        "seed": task["seed"],
        **info,
    }
