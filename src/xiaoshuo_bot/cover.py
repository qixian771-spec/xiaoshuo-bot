from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import io
import os
from pathlib import Path
import random
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .llm.base import (
    BudgetCircuitBreakerError,
    ImageGenerationError,
    ImageGenerationRequest,
    LLMConfigurationError,
    LLMError,
    QuotaExhaustedError,
)
from .llm.router import RoleModelRouter
from .models import NovelProject, ProjectMeta

COVER_WIDTH = 600
COVER_HEIGHT = 800
SAFE_MARGIN_PX = 42
MIN_FILE_SIZE_BYTES = 100 * 1024  # 100 KB
MAX_FILE_SIZE_BYTES = int(4.5 * 1024 * 1024)  # 4.5 MB
VALID_BASE_MODES: Tuple[str, ...] = ("ai_image", "procedural")
VALID_OUTPUT_FORMATS: Tuple[str, ...] = ("PNG", "JPEG")
DEFAULT_FONTS_DIR = Path(__file__).resolve().parent / "fonts"


class CoverError(RuntimeError):
    """Base error for cover asset validation and synthesis."""


class FontAssetMissingError(CoverError, FileNotFoundError):
    """Raised when the bundled SIL OFL 1.1 Chinese font or OFL.txt license is missing or invalid."""


class ImageProviderNotConfiguredError(CoverError):
    """Raised when base_mode='ai_image' is used without a configured 'image' role in RoleModelRouter."""


class CoverGenerationError(CoverError):
    """Raised when cover generation, rendering, or compliance validation fails."""


@dataclass(frozen=True)
class CoverGenerationResult:
    output_path: Path
    width: int
    height: int
    mode: str
    format: str
    file_size_bytes: int
    base_mode: str
    title: str
    pen_name: str
    font_path: Path
    ofl_license_path: Path
    title_bbox: Tuple[int, int, int, int]
    author_bbox: Tuple[int, int, int, int]
    safe_margin_px: int = SAFE_MARGIN_PX

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
            "format": self.format,
            "file_size_bytes": self.file_size_bytes,
            "base_mode": self.base_mode,
            "title": self.title,
            "pen_name": self.pen_name,
            "font_path": str(self.font_path),
            "ofl_license_path": str(self.ofl_license_path),
            "title_bbox": list(self.title_bbox),
            "author_bbox": list(self.author_bbox),
            "safe_margin_px": self.safe_margin_px,
        }


def validate_bundled_font_assets(
    fonts_dir: Optional[Path | str] = None,
) -> Tuple[Path, Path]:
    """Strictly validate bundled SIL OFL 1.1 Chinese font and OFL.txt. Never fallback to OS fonts."""
    resolved_dir = Path(fonts_dir).resolve() if fonts_dir is not None else DEFAULT_FONTS_DIR
    if not resolved_dir.is_dir():
        raise FontAssetMissingError(
            f"Bundled fonts directory does not exist: '{resolved_dir}'"
        )

    ofl_path = resolved_dir / "OFL.txt"
    if not ofl_path.is_file():
        raise FontAssetMissingError(
            f"Missing SIL OFL 1.1 license file 'OFL.txt' in '{resolved_dir}'"
        )
    try:
        ofl_text = ofl_path.read_text(encoding="utf-8", errors="strict")
    except OSError as exc:
        raise FontAssetMissingError(
            f"Unable to read OFL.txt at '{ofl_path}': {exc}"
        ) from exc

    normalized_ofl = ofl_text.upper()
    if "SIL OPEN FONT LICENSE" not in normalized_ofl or "VERSION 1.1" not in normalized_ofl:
        raise FontAssetMissingError(
            f"License file '{ofl_path}' is not a valid SIL Open Font License Version 1.1 document."
        )

    font_candidates = sorted(
        [
            p
            for p in resolved_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".ttf", ".otf"}
        ],
        key=lambda p: p.name.lower(),
    )
    if not font_candidates:
        raise FontAssetMissingError(
            f"No bundled .ttf/.otf font asset found in '{resolved_dir}'."
        )

    font_path = font_candidates[0]
    if font_path.stat().st_size < 1024:
        raise FontAssetMissingError(
            f"Bundled font '{font_path}' is unexpectedly small ({font_path.stat().st_size} bytes)."
        )

    try:
        test_font = ImageFont.truetype(str(font_path), 40)
        bbox = test_font.getbbox("白雾渡口测试著")
    except Exception as exc:
        raise FontAssetMissingError(
            f"Bundled font '{font_path}' failed PIL FreeType verification: {exc}"
        ) from exc

    if not bbox or (bbox[2] - bbox[0]) <= 0 or (bbox[3] - bbox[1]) <= 0:
        raise FontAssetMissingError(
            f"Bundled font '{font_path}' did not produce valid CJK glyph bounding boxes."
        )

    return font_path, ofl_path


def load_bundled_font(
    size: int,
    fonts_dir: Optional[Path | str] = None,
) -> ImageFont.FreeTypeFont:
    """Load the verified bundled OFL CJK font at the requested pixel size."""
    if size <= 0:
        raise ValueError(f"Font size must be positive, got {size}")
    font_path, _ = validate_bundled_font_assets(fonts_dir)
    try:
        return ImageFont.truetype(str(font_path), size)
    except Exception as exc:
        raise FontAssetMissingError(
            f"Failed to load bundled font '{font_path}' at size {size}: {exc}"
        ) from exc


def build_cover_illustration_prompt(
    title: str,
    genre: str = "",
    synopsis: str = "",
    visual_motif: str = "",
) -> str:
    """Build a text-free 3:4 vertical illustration prompt reserving top/bottom dark bands for typography."""
    genre_text = genre.strip() or "东方都市悬疑幻想"
    motif_text = visual_motif.strip() or synopsis.strip()[:160] or f"与《{title}》意境相契合的强氛围核心意象"
    return (
        "Vertical 3:4 book cover background illustration only. "
        f"Genre mood: {genre_text}. Core visual motif inspired by: {motif_text}. "
        "Cinematic lighting, rich painterly digital illustration, high visual depth, dramatic focal composition in the middle third. "
        "Leave clean dark atmospheric negative space in the top 30% band and bottom 18% band for external typography overlay. "
        "STRICT REQUIREMENT: absolutely NO text, NO Chinese characters, NO English letters, NO numbers, NO watermarks, NO logos, NO signatures anywhere in the image."
    )


def _to_rgb_600x800(img: Image.Image) -> Image.Image:
    """Convert any decoded image mode to clean RGB and fit/crop to exact 600x800."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        backdrop = Image.new("RGBA", rgba.size, (13, 17, 23, 255))
        img = Image.alpha_composite(backdrop, rgba).convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return ImageOps.fit(
        img,
        (COVER_WIDTH, COVER_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _genre_palette(genre: str, seed_bytes: bytes) -> Dict[str, Tuple[int, int, int]]:
    g = genre.lower()
    if any(k in g for k in ("玄幻", "修仙", "仙侠", "xuanhuan", "ancient")):
        return {
            "top": (12, 18, 36),
            "mid": (28, 44, 78),
            "bot": (10, 14, 24),
            "glow": (212, 168, 86),
            "accent": (245, 224, 170),
        }
    if any(k in g for k in ("末世", "科幻", "apocalypse", "sci")):
        return {
            "top": (14, 20, 26),
            "mid": (38, 56, 68),
            "bot": (12, 14, 18),
            "glow": (96, 186, 214),
            "accent": (228, 242, 248),
        }
    if any(k in g for k in ("悬疑", "怪谈", "惊悚", "suspense", "mystery")):
        return {
            "top": (10, 14, 18),
            "mid": (30, 38, 44),
            "bot": (8, 10, 14),
            "glow": (184, 72, 72),
            "accent": (240, 234, 224),
        }
    if any(k in g for k in ("现言", "言情", "甜宠", "重生", "female", "era")):
        return {
            "top": (32, 16, 30),
            "mid": (78, 36, 58),
            "bot": (18, 10, 20),
            "glow": (236, 178, 134),
            "accent": (252, 240, 228),
        }
    # Urban / default palette varied subtly by title hash
    shift = seed_bytes[0] % 18
    return {
        "top": (12 + shift // 3, 18, 34),
        "mid": (26 + shift // 2, 48, 84),
        "bot": (10, 12, 22),
        "glow": (228, 176, 82),
        "accent": (246, 242, 230),
    }


def _generate_procedural_base(title: str, genre: str) -> Image.Image:
    """Generate a rich deterministic 600x800 RGB illustration for offline/test procedural mode."""
    seed_bytes = hashlib.sha256(f"{title}|{genre}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))
    palette = _genre_palette(genre, seed_bytes)

    # Multi-stop vertical gradient using C-level Image.linear_gradient
    grad = Image.linear_gradient("L").resize((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.BILINEAR)
    top_img = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), palette["top"])
    mid_img = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), palette["mid"])
    bot_img = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT), palette["bot"])

    # Upper half top->mid, lower half mid->bot via alpha mask
    upper_mask = grad.point(lambda v: min(255, int(v * 2)))
    lower_mask = grad.point(lambda v: max(0, int((v - 128) * 2)))
    canvas = Image.composite(mid_img, top_img, upper_mask)
    canvas = Image.composite(bot_img, canvas, lower_mask)

    # Atmospheric radial glow in middle band
    glow_layer = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow_layer)
    cx = COVER_WIDTH // 2 + rng.randint(-55, 55)
    cy = int(COVER_HEIGHT * 0.54) + rng.randint(-30, 30)
    for radius, alpha in ((250, 24), (190, 36), (130, 48), (75, 58)):
        gdraw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(*palette["glow"], alpha),
        )
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=24))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), glow_layer)

    # Layered geometric silhouettes, celestial rings & light rays in center zone
    art = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    adraw = ImageDraw.Draw(art)
    adraw.ellipse(
        (cx - 140, cy - 140, cx + 140, cy + 140),
        outline=(*palette["accent"], 90),
        width=2,
    )
    adraw.ellipse(
        (cx - 102, cy - 102, cx + 102, cy + 102),
        outline=(*palette["glow"], 120),
        width=2,
    )
    # Diagonal light beams
    for _ in range(7):
        x0 = rng.randint(40, COVER_WIDTH - 40)
        x1 = x0 + rng.randint(-120, 120)
        adraw.line(
            [(x0, 210), (x1, 660)],
            fill=(*palette["accent"], rng.randint(18, 42)),
            width=rng.randint(2, 6),
        )
    # Layered mountain/city skyline silhouette in lower-mid third
    horizon_y = 575
    for layer_idx, (shade_scale, step_h) in enumerate(((0.45, 52), (0.30, 38), (0.18, 26))):
        pts: List[Tuple[int, int]] = [(0, COVER_HEIGHT), (0, horizon_y + layer_idx * 24)]
        x = 0
        while x < COVER_WIDTH:
            x = min(COVER_WIDTH, x + rng.randint(28, 64))
            y = horizon_y + layer_idx * 24 + rng.randint(-step_h, step_h // 2)
            pts.append((x, y))
        pts.append((COVER_WIDTH, COVER_HEIGHT))
        col = tuple(max(4, int(c * shade_scale)) for c in palette["mid"])
        adraw.polygon(pts, fill=(*col, 220))

    # Crisp fine stars / dust particles
    for _ in range(180):
        px = rng.randint(24, COVER_WIDTH - 24)
        py = rng.randint(180, COVER_HEIGHT - 130)
        pr = rng.choice((1, 1, 2))
        adraw.ellipse(
            (px - pr, py - pr, px + pr, py + pr),
            fill=(*palette["accent"], rng.randint(45, 160)),
        )

    canvas = Image.alpha_composite(canvas, art).convert("RGB")
    return canvas


def _apply_anti_banding_grain(img: Image.Image, sigma: float = 12.0, blend: float = 0.05) -> Image.Image:
    """Apply subtle spatial anti-banding texture so 8-bit gradients never band and compress cleanly."""
    noise = Image.effect_noise((COVER_WIDTH, COVER_HEIGHT), sigma).convert("RGB")
    return Image.blend(img, noise, blend)


def _apply_typography_scrims(base: Image.Image) -> Image.Image:
    """Apply subtle top & bottom dark gradient scrims for crisp readable typography."""
    scrim = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scrim)
    top_band = 290
    for y in range(top_band):
        alpha = int(165 * (1.0 - y / top_band) ** 1.4)
        draw.line([(0, y), (COVER_WIDTH, y)], fill=(8, 10, 16, alpha))
    bot_band = 170
    for offset in range(bot_band):
        y = COVER_HEIGHT - 1 - offset
        alpha = int(185 * (1.0 - offset / bot_band) ** 1.3)
        draw.line([(0, y), (COVER_WIDTH, y)], fill=(8, 10, 16, alpha))
    return Image.alpha_composite(base.convert("RGBA"), scrim).convert("RGB")


def _wrap_title_lines(title: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    """Wrap CJK/mixed title cleanly into 1..3 balanced lines within max_width."""
    probe = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(probe)

    def _w(text: str) -> int:
        bb = draw.textbbox((0, 0), text, font=font, stroke_width=2)
        return bb[2] - bb[0]

    if _w(title) <= max_width:
        return [title]

    # Try 2 lines then 3 lines balancing character count
    chars = list(title)
    n = len(chars)
    best_split: Optional[List[str]] = None
    best_max_w = 10**9
    for idx in range(1, n):
        l1 = "".join(chars[:idx]).strip()
        l2 = "".join(chars[idx:]).strip()
        if not l1 or not l2:
            continue
        mw = max(_w(l1), _w(l2))
        if mw <= max_width and mw < best_max_w:
            best_max_w = mw
            best_split = [l1, l2]
    if best_split is not None:
        return best_split

    # Greedy line break up to 3 lines
    lines: List[str] = []
    cur = ""
    for ch in chars:
        cand = cur + ch
        if cur and _w(cand) > max_width:
            lines.append(cur)
            cur = ch
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def _render_text_block_strictly_inside_safe_box(
    *,
    lines: List[str],
    fonts_dir: Optional[Path | str],
    start_size: int,
    min_size: int,
    top_y: int,
    bottom_limit_y: int,
    fill: Tuple[int, int, int, int],
    stroke_fill: Tuple[int, int, int, int],
    stroke_width: int,
    line_spacing: int,
    align_bottom: bool = False,
) -> Tuple[Image.Image, Tuple[int, int, int, int]]:
    """Render lines onto a transparent 600x800 RGBA layer guaranteeing all pixels sit inside >= 42px margin."""
    safe_left = SAFE_MARGIN_PX
    safe_top = SAFE_MARGIN_PX
    safe_right = COVER_WIDTH - SAFE_MARGIN_PX
    safe_bottom = COVER_HEIGHT - SAFE_MARGIN_PX
    max_w = safe_right - safe_left - 12  # extra room for oblique glyph lean

    for size in range(start_size, min_size - 1, -2):
        font = load_bundled_font(size, fonts_dir=fonts_dir)
        active_lines = _wrap_title_lines("".join(lines), font, max_w) if len(lines) == 1 else lines

        layer = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        line_metrics: List[Tuple[str, int, int, Tuple[int, int, int, int]]] = []
        total_h = 0
        for idx, ln in enumerate(active_lines):
            bb = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke_width)
            lw = bb[2] - bb[0]
            lh = bb[3] - bb[1]
            line_metrics.append((ln, lw, lh, bb))
            total_h += lh + (line_spacing if idx > 0 else 0)

        if align_bottom:
            cur_y = bottom_limit_y - total_h
        else:
            cur_y = top_y

        if cur_y < safe_top or (cur_y + total_h) > bottom_limit_y:
            continue

        for idx, (ln, lw, lh, bb) in enumerate(line_metrics):
            x = (COVER_WIDTH - lw) // 2 - bb[0]
            y = cur_y - bb[1]
            # Soft drop shadow (kept inside safe zone by 2px offset)
            draw.text(
                (x + 2, y + 3),
                ln,
                font=font,
                fill=(0, 0, 0, 150),
                stroke_width=stroke_width,
                stroke_fill=(0, 0, 0, 150),
            )
            draw.text(
                (x, y),
                ln,
                font=font,
                fill=fill,
                stroke_width=stroke_width,
                stroke_fill=stroke_fill,
            )
            cur_y += lh + line_spacing

        actual_bbox = layer.getbbox()
        if actual_bbox is None:
            continue
        x0, y0, x1, y1 = actual_bbox
        if (
            x0 >= safe_left
            and y0 >= safe_top
            and x1 <= safe_right
            and y1 <= safe_bottom
            and y1 <= bottom_limit_y + 4
        ):
            return layer, actual_bbox

    raise CoverGenerationError(
        f"Failed to fit typography {lines!r} strictly inside {SAFE_MARGIN_PX}px safe margin."
    )


def _encode_cover_bytes(img: Image.Image, output_format: str) -> bytes:
    """Encode 600x800 RGB image into PNG or JPEG within [100KB, 4.5MB]."""
    fmt = output_format.upper()
    if fmt not in VALID_OUTPUT_FORMATS:
        raise CoverGenerationError(
            f"Unsupported output_format {output_format!r}; expected one of {VALID_OUTPUT_FORMATS}"
        )

    def _save(target_img: Image.Image, **kwargs: Any) -> bytes:
        buf = io.BytesIO()
        target_img.save(buf, format=fmt, **kwargs)
        return buf.getvalue()

    if fmt == "PNG":
        data = _save(img, optimize=False, compress_level=6)
        if len(data) < MIN_FILE_SIZE_BYTES:
            # Apply gentle anti-banding texture if source illustration was flat/synthetic
            textured = _apply_anti_banding_grain(img, sigma=18.0, blend=0.06)
            data = _save(textured, optimize=False, compress_level=4)
        if len(data) > MAX_FILE_SIZE_BYTES:
            data = _save(img, optimize=True, compress_level=9)
    else:
        data = _save(img, quality=95, subsampling=0)
        if len(data) < MIN_FILE_SIZE_BYTES:
            textured = _apply_anti_banding_grain(img, sigma=28.0, blend=0.14)
            data = _save(textured, quality=98, subsampling=0)
        if len(data) > MAX_FILE_SIZE_BYTES:
            for q in (90, 84, 78, 72):
                data = _save(img, quality=q, optimize=True)
                if len(data) <= MAX_FILE_SIZE_BYTES:
                    break

    if not (MIN_FILE_SIZE_BYTES <= len(data) <= MAX_FILE_SIZE_BYTES):
        raise CoverGenerationError(
            f"Encoded cover size {len(data)} bytes violates range "
            f"[{MIN_FILE_SIZE_BYTES}, {MAX_FILE_SIZE_BYTES}]."
        )
    return data


def generate_cover(
    *,
    title: str,
    pen_name: str,
    output_path: str | Path,
    genre: str = "",
    synopsis: str = "",
    visual_motif: str = "",
    base_mode: str = "ai_image",
    router: Optional[RoleModelRouter] = None,
    output_format: str = "PNG",
    fonts_dir: Optional[Path | str] = None,
) -> CoverGenerationResult:
    """Dual-stage 600x800 compliant book cover generator using bundled SIL OFL 1.1 CJK font."""
    font_path, ofl_path = validate_bundled_font_assets(fonts_dir)

    if not isinstance(title, str) or not title.strip():
        raise CoverGenerationError("Cover title must be a non-empty string.")
    if title != title.strip():
        raise CoverGenerationError("Cover title must match project title verbatim without leading/trailing whitespace.")
    if len(title) > 15:
        raise CoverGenerationError(f"Cover title length {len(title)} exceeds Tomato 15-char maximum: {title!r}")
    if "《" in title or "》" in title:
        raise CoverGenerationError(f"Cover title must not contain book title marks 《》: {title!r}")

    if not isinstance(pen_name, str) or not pen_name.strip():
        raise CoverGenerationError("pen_name is required for cover author imprint.")
    clean_pen_name = pen_name.strip()

    if base_mode not in VALID_BASE_MODES:
        raise CoverGenerationError(
            f"Unsupported base_mode {base_mode!r}; expected one of {VALID_BASE_MODES}."
        )

    # Stage 1: Base illustration (ai_image default OR explicit procedural)
    if base_mode == "ai_image":
        if router is None or not router.has_role("image"):
            raise ImageProviderNotConfiguredError(
                "base_mode='ai_image' requires RoleModelRouter with a configured 'image' role."
            )
        prompt = build_cover_illustration_prompt(
            title=title,
            genre=genre,
            synopsis=synopsis,
            visual_motif=visual_motif,
        )
        req = ImageGenerationRequest(
            prompt=prompt,
            request_id=f"cover_{hashlib.sha1(title.encode('utf-8')).hexdigest()[:8]}",
            size="768x1024",
            aspect_ratio="3:4",
        )
        try:
            resp = router.generate_image(req)
        except LLMConfigurationError as exc:
            raise ImageProviderNotConfiguredError(str(exc)) from exc
        except (ImageGenerationError, QuotaExhaustedError, BudgetCircuitBreakerError, LLMError) as exc:
            raise CoverGenerationError(f"AI base image generation failed: {exc}") from exc

        try:
            with Image.open(io.BytesIO(resp.image_bytes)) as raw_img:
                raw_img.load()
                base_img = _to_rgb_600x800(raw_img)
        except Exception as exc:
            raise CoverGenerationError(
                f"Failed to decode generated image bytes ({resp.mime_type}): {exc}"
            ) from exc
    else:
        base_img = _generate_procedural_base(title=title, genre=genre)

    # Stage 2: Scrims + anti-banding dither + bundled OFL font typography strictly inside >=42px safe box
    graded = _apply_anti_banding_grain(_apply_typography_scrims(base_img), sigma=10.0, blend=0.04)

    title_layer, title_bbox = _render_text_block_strictly_inside_safe_box(
        lines=[title],
        fonts_dir=fonts_dir,
        start_size=68,
        min_size=24,
        top_y=SAFE_MARGIN_PX + 24,
        bottom_limit_y=320,
        fill=(252, 245, 228, 255),
        stroke_fill=(10, 12, 18, 235),
        stroke_width=2,
        line_spacing=12,
        align_bottom=False,
    )

    author_credit = f"{clean_pen_name} 著"
    author_layer, author_bbox = _render_text_block_strictly_inside_safe_box(
        lines=[author_credit],
        fonts_dir=fonts_dir,
        start_size=30,
        min_size=18,
        top_y=COVER_HEIGHT - 150,
        bottom_limit_y=COVER_HEIGHT - SAFE_MARGIN_PX - 14,
        fill=(232, 212, 168, 255),
        stroke_fill=(10, 12, 18, 220),
        stroke_width=1,
        line_spacing=6,
        align_bottom=True,
    )

    # Subtle golden rule above author credit inside safe horizontal margin
    composed = graded.convert("RGBA")
    rule_layer = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(rule_layer)
    rule_y = max(SAFE_MARGIN_PX + 4, author_bbox[1] - 16)
    rdraw.line(
        [(COVER_WIDTH // 2 - 72, rule_y), (COVER_WIDTH // 2 + 72, rule_y)],
        fill=(232, 198, 128, 175),
        width=2,
    )

    composed = Image.alpha_composite(composed, rule_layer)
    composed = Image.alpha_composite(composed, title_layer)
    composed = Image.alpha_composite(composed, author_layer).convert("RGB")

    if composed.size != (COVER_WIDTH, COVER_HEIGHT) or composed.mode != "RGB":
        raise CoverGenerationError(
            f"Final cover invariant violated: size={composed.size}, mode={composed.mode}"
        )

    fmt = output_format.upper()
    encoded = _encode_cover_bytes(composed, fmt)

    out_path = Path(output_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(f"{out_path.suffix}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, out_path)

    return CoverGenerationResult(
        output_path=out_path,
        width=COVER_WIDTH,
        height=COVER_HEIGHT,
        mode="RGB",
        format=fmt,
        file_size_bytes=len(encoded),
        base_mode=base_mode,
        title=title,
        pen_name=clean_pen_name,
        font_path=font_path,
        ofl_license_path=ofl_path,
        title_bbox=title_bbox,
        author_bbox=author_bbox,
        safe_margin_px=SAFE_MARGIN_PX,
    )


def generate_cover_for_project(
    project: NovelProject | ProjectMeta,
    output_path: str | Path,
    *,
    base_mode: str = "ai_image",
    router: Optional[RoleModelRouter] = None,
    output_format: str = "PNG",
    fonts_dir: Optional[Path | str] = None,
) -> CoverGenerationResult:
    """Convenience wrapper extracting title/pen_name/genre/synopsis from NovelProject or ProjectMeta."""
    meta = project.meta if isinstance(project, NovelProject) else project
    pen_name = meta.pen_name or meta.author_name
    return generate_cover(
        title=meta.title,
        pen_name=pen_name,
        output_path=output_path,
        genre=meta.genre,
        synopsis=meta.synopsis,
        base_mode=base_mode,
        router=router,
        output_format=output_format,
        fonts_dir=fonts_dir,
    )
