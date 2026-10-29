"""A reproducible multilingual showcase rendered with the dataset engine."""

from pathlib import Path

from PIL import Image, ImageDraw

from .config import Config, Generation
from .fonts import Coverage, discover
from .render import Renderer

SAMPLES = (
    ("English", "en", "notosans", "Make text. Train any model."),
    ("Kurdish", "ckb", "notonaskharabic", "سڵاو جیهان، زانیاری بۆ هەمووان"),
    ("Arabic", "ar", "amiri", "بيانات واضحة، نماذج أفضل"),
    ("Persian", "fa", "notosansarabic", "داده‌های روشن برای همه"),
    ("Hebrew", "he", "notosanshebrew", "מילים קטנות, אפשרויות גדולות"),
    ("Urdu", "ur", "notonastaliqurdu", "کتب خانہ سب کے لیے کھلا ہے"),
    ("Hindi", "hi", "notosansdevanagari", "हर शब्द एक नई संभावना है"),
    ("Bengali", "bn", "notosansbengali", "সবার জন্য নতুন সম্ভাবনা"),
    ("Chinese", "zh", "notosanssc", "每一种文字，都有新的可能"),
    ("Japanese", "ja", "notosansjp", "ことばから、新しい可能性へ"),
    ("Korean", "ko", "notosanskr", "모든 글자에 새로운 가능성"),
    ("Thai", "th", "notosansthai", "ทุกตัวอักษรสร้างโอกาสใหม่"),
)


def preview(config: Config, destination: Path) -> dict:
    """Use only fonts accompanied by OFL.txt; refuse to overwrite an existing file."""
    if destination.exists():
        raise ValueError(f"Preview already exists; choose a new output: {destination}")
    fonts = [
        font
        for font in discover([config.path(p) for p in config.fonts.directories])
        if Path(font.path).with_name("OFL.txt").is_file()
    ]
    if not fonts:
        raise ValueError("Preview requires fonts accompanied by OFL.txt; run synthocr fonts fetch")
    coverage = Coverage(config.fonts.coverage_cache_size)
    renderer = Renderer(
        Generation(
            font_size=44,
            padding=8,
            max_width=686,
            max_height=84,
            max_pixels=686 * 84,
            augmentation=0.35,
        ),
        config.fonts.cache_size,
    )
    preferred = sorted(fonts, key=lambda font: Path(font.path).parent.name != "notosans")
    ui = coverage.choose(preferred, "SynthOCR / Every writing system. Any recognition model.", 0)
    if ui is None:
        raise ValueError("Preview requires a licensed font with Latin glyphs")
    title = renderer.font(ui, 86)
    subtitle = renderer.font(ui, 28)
    label = renderer.font(ui, 17)
    small = renderer.font(ui, 14)
    image = Image.new("RGB", (1600, 1260), "#eef3f5")
    try:
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1600, 252), fill="#10282e")
        draw.polygon(((1270, 0), (1600, 0), (1600, 252), (1435, 252)), fill="#163b40")
        draw.rectangle((56, 48, 62, 128), fill="#74e2c1")
        draw.text((84, 25), "SynthOCR", font=title, fill="#f5faf9")
        draw.text(
            (60, 139),
            "Every writing system. Any recognition model.",
            font=subtitle,
            fill="#c7ddda",
        )
        x = 60
        for text in ("12 LANGUAGES", "BUILT-IN FONT CHECKS", "MODEL-NEUTRAL EXPORTS"):
            width = int(draw.textlength(text, font=small)) + 30
            draw.rounded_rectangle((x, 201, x + width, 232), radius=15, fill="#24464b")
            draw.text((x + 15, 205), text, font=small, fill="#95e7cf")
            x += width + 12
        for index, (name, language, family, text) in enumerate(SAMPLES):
            choices = sorted(fonts, key=lambda font: Path(font.path).parent.name != family)
            face = coverage.choose(choices, text, 0)
            if face is None:
                raise ValueError(f"No licensed font covers the {name} preview; fetch catalog fonts")
            x, y = 56 + (index % 2) * 754, 280 + (index // 2) * 152
            draw.rounded_rectangle((x, y + 3, x + 734, y + 139), radius=16, fill="#dfe8eb")
            draw.rounded_rectangle((x, y, x + 734, y + 136), radius=16, fill="white")
            draw.text(
                (x + 24, y + 13), f"{name.upper()}  /  {language}", font=label, fill="#29605d"
            )
            draw.text((x + 691, y + 15), f"{index + 1:02}", font=small, fill="#77918e")
            sample, _ = renderer.render(text, face, 17 + index, language)
            try:
                image.paste(sample, (x + 24, y + 43 + (84 - sample.height) // 2))
            finally:
                sample.close()
        draw.text(
            (60, 1207),
            "Real SynthOCR renders  /  RAQM shaping  /  OFL fonts  /  Authored sample text",
            font=label,
            fill="#456764",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also protects against a destination created during rendering.
        with destination.open("xb") as stream:
            image.save(stream, format="PNG", optimize=True)
        return {
            "output": str(destination),
            "width": image.width,
            "height": image.height,
            "samples": len(SAMPLES),
            "languages": [sample[1] for sample in SAMPLES],
        }
    finally:
        image.close()
