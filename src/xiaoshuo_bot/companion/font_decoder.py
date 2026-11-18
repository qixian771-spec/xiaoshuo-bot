from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont, TTLibError

from ..models import SHANGHAI_TZ
from datetime import datetime


PUA_RANGE_START = 0xE000
PUA_RANGE_END = 0xF8FF
PUA_RESIDUAL_RE = re.compile(r"[\uE000-\uF8FF\uFFFD]")


class FontDecodeError(RuntimeError):
    """Base error raised when parsing or decoding obfuscated WOFF2 fonts fails."""


class FontDecodeResidualError(FontDecodeError, ValueError):
    """Raised when decoded text still contains unmapped PUA (U+E000..U+F8FF) or U+FFFD codepoints."""

    def __init__(self, message: str, *, residual_codepoints: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.residual_codepoints: Tuple[str, ...] = tuple(residual_codepoints)


def is_pua_codepoint(codepoint: int) -> bool:
    return PUA_RANGE_START <= codepoint <= PUA_RANGE_END


def scan_residual_pua(text: str) -> List[str]:
    """Return sorted unique U+XXXX codepoints for any PUA or replacement chars in text."""
    if not text:
        return []
    found = {f"U+{ord(ch):04X}" for ch in PUA_RESIDUAL_RE.findall(text)}
    return sorted(found)


def assert_no_residual_pua(text: str, *, context_label: str = "decoded_text") -> str:
    """Strictly verify zero residual PUA (U+E000..U+F8FF) or U+FFFD chars remain."""
    residuals = scan_residual_pua(text)
    if residuals:
        snippet = text[:80].replace("\n", " ")
        raise FontDecodeResidualError(
            f"{context_label} still contains unmapped PUA/replacement codepoints "
            f"{residuals}; refusing to emit corrupted data (snippet={snippet!r})",
            residual_codepoints=residuals,
        )
    return text


@dataclass(frozen=True)
class GlyphOutlineSignatures:
    exact_hash: str
    shape_hash: str
    aspect_hash: str
    glyph_name: str
    contour_ops: int
    advance_width: int
    lsb: int


def _round_pt(pt: Tuple[float, float], scale: float, dx: float = 0.0, dy: float = 0.0, ndigits: int = 1) -> Tuple[float, float]:
    return (
        round((float(pt[0]) + dx) * scale, ndigits),
        round((float(pt[1]) + dy) * scale, ndigits),
    )


def extract_glyph_signatures(
    ttfont: TTFont,
    glyph_name: str,
) -> GlyphOutlineSignatures:
    """Extract canonical contour/outline/metrics SHA256 signatures for a glyph."""
    glyph_set = ttfont.getGlyphSet()
    if glyph_name not in glyph_set:
        raise FontDecodeError(f"Glyph '{glyph_name}' not found in font glyphSet")

    head_table = ttfont.get("head")
    if head_table is None or not getattr(head_table, "unitsPerEm", 0):
        raise FontDecodeError("Font missing valid 'head' table or unitsPerEm")
    units_per_em = float(head_table.unitsPerEm)
    em_scale = 1000.0 / units_per_em

    hmtx = ttfont.get("hmtx")
    if hmtx is not None and glyph_name in hmtx.metrics:
        adv_raw, lsb_raw = hmtx.metrics[glyph_name]
    else:
        adv_raw, lsb_raw = 0, 0
    adv_norm = int(round(float(adv_raw) * em_scale))
    lsb_norm = int(round(float(lsb_raw) * em_scale))

    rec_pen = RecordingPen()
    glyph_set[glyph_name].draw(rec_pen)

    bounds_pen = BoundsPen(glyph_set)
    glyph_set[glyph_name].draw(bounds_pen)
    bounds = bounds_pen.bounds  # (xMin, yMin, xMax, yMax) or None

    exact_commands: List[Tuple[str, Tuple[Tuple[float, float], ...]]] = []
    shape_commands: List[Tuple[str, Tuple[Tuple[float, float], ...]]] = []
    aspect_commands: List[Tuple[str, Tuple[Tuple[float, float], ...]]] = []

    if bounds is not None:
        xmin, ymin, xmax, ymax = bounds
        dx = -float(xmin)
        dy = -float(ymin)
        bbox_w = max(float(xmax - xmin), 1.0)
        bbox_h = max(float(ymax - ymin), 1.0)
        aspect_scale = 1000.0 / max(bbox_w, bbox_h)
        aspect_ratio = round(bbox_w / bbox_h, 3)
    else:
        dx = 0.0
        dy = 0.0
        aspect_scale = em_scale
        aspect_ratio = 0.0

    for op, pts in rec_pen.value:
        norm_pts_exact: List[Tuple[float, float]] = []
        norm_pts_shape: List[Tuple[float, float]] = []
        norm_pts_aspect: List[Tuple[float, float]] = []
        for item in pts:
            if item is None:
                continue
            pt_xy = (float(item[0]), float(item[1]))
            norm_pts_exact.append(_round_pt(pt_xy, em_scale, 0.0, 0.0, ndigits=1))
            norm_pts_shape.append(_round_pt(pt_xy, em_scale, dx, dy, ndigits=0))
            norm_pts_aspect.append(_round_pt(pt_xy, aspect_scale, dx, dy, ndigits=0))
        exact_commands.append((op, tuple(norm_pts_exact)))
        shape_commands.append((op, tuple(norm_pts_shape)))
        aspect_commands.append((op, tuple(norm_pts_aspect)))

    exact_payload = {
        "cmds": exact_commands,
        "adv": adv_norm,
        "lsb": lsb_norm,
    }
    shape_payload = {
        "cmds": shape_commands,
        "adv": adv_norm,
    }
    aspect_payload = {
        "cmds": aspect_commands,
        "ar": aspect_ratio,
    }

    def _digest(obj: Any) -> str:
        raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    return GlyphOutlineSignatures(
        exact_hash=_digest(exact_payload),
        shape_hash=_digest(shape_payload),
        aspect_hash=_digest(aspect_payload),
        glyph_name=glyph_name,
        contour_ops=len(exact_commands),
        advance_width=adv_norm,
        lsb=lsb_norm,
    )


MAX_OBFUSCATED_FONT_BYTES = 2 * 1024 * 1024


def _read_font_bytes(
    font_source: bytes | bytearray | Path | str,
    *,
    max_bytes: Optional[int] = None,
) -> bytes:
    if isinstance(font_source, (bytes, bytearray)):
        data = bytes(font_source)
        if not data:
            raise FontDecodeError("Font bytes cannot be empty")
        if max_bytes is not None and len(data) > max_bytes:
            raise FontDecodeError(f"Font bytes exceed size cap ({len(data)} > {max_bytes})")
        return data
    path = Path(font_source)
    if not path.is_file():
        raise FontDecodeError(f"Font file does not exist: '{path}'")
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise FontDecodeError(f"Font file '{path}' exceeds size cap ({max_bytes} bytes)")
    data = path.read_bytes()
    if not data:
        raise FontDecodeError(f"Font file is empty: '{path}'")
    return data


@dataclass
class FontDecodeResult:
    font_sha256: str
    decoded_text: str
    mapped_count: int
    total_pua_chars: int
    cache_hit: bool


class Woff2FontDecoder:
    """Dynamic WOFF2 PUA font obfuscation decoder backed by glyph outline canonical hashes."""

    def __init__(
        self,
        *,
        cache_dir: Optional[Path | str] = None,
        reference_profile_path: Optional[Path | str] = None,
    ) -> None:
        self.cache_dir: Optional[Path] = Path(cache_dir).resolve() if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._exact_refs: Dict[str, str] = {}
        self._shape_refs: Dict[str, str] = {}
        self._aspect_refs: Dict[str, str] = {}
        self._aspect_collisions: set[str] = set()
        self._font_map_cache: Dict[str, Dict[int, str]] = {}

        if reference_profile_path is not None:
            self.load_reference_profile(reference_profile_path)

    @property
    def reference_count(self) -> int:
        return len(self._exact_refs)

    def register_reference_font(
        self,
        font_source: bytes | bytearray | Path | str,
        *,
        charset: Optional[Iterable[str]] = None,
    ) -> int:
        """Register canonical glyph signatures from a clean reference TTF/OTF/WOFF/WOFF2 font."""
        font_bytes = _read_font_bytes(font_source)
        try:
            ttfont = TTFont(io.BytesIO(font_bytes))
        except Exception as exc:
            raise FontDecodeError(f"Failed to parse reference font: {exc}") from exc

        try:
            cmap = ttfont.getBestCmap()
            if not cmap:
                raise FontDecodeError("Reference font does not contain a Unicode cmap table")
            allowed_chars = set(charset) if charset is not None else None
            registered = 0
            for cp, glyph_name in cmap.items():
                if is_pua_codepoint(cp) or cp == 0xFFFD:
                    continue
                ch = chr(cp)
                if allowed_chars is not None and ch not in allowed_chars:
                    continue
                try:
                    sigs = extract_glyph_signatures(ttfont, glyph_name)
                except FontDecodeError:
                    raise
                except Exception as exc:
                    raise FontDecodeError(
                        f"Failed extracting reference glyph {glyph_name!r}: {exc}"
                    ) from exc
                if sigs.contour_ops == 0:
                    continue
                self._register_signatures_for_char(ch, sigs)
                registered += 1
            return registered
        except FontDecodeError:
            raise
        except Exception as exc:
            raise FontDecodeError(f"Failed to read reference font tables: {exc}") from exc
        finally:
            ttfont.close()

    def _register_signatures_for_char(self, ch: str, sigs: GlyphOutlineSignatures) -> None:
        self._exact_refs[sigs.exact_hash] = ch
        self._shape_refs.setdefault(sigs.shape_hash, ch)
        if sigs.aspect_hash in self._aspect_refs and self._aspect_refs[sigs.aspect_hash] != ch:
            self._aspect_collisions.add(sigs.aspect_hash)
            self._aspect_refs.pop(sigs.aspect_hash, None)
        elif sigs.aspect_hash not in self._aspect_collisions:
            self._aspect_refs[sigs.aspect_hash] = ch

    def register_reference_map(self, signature_to_char: Mapping[str, str], *, kind: str = "exact") -> int:
        """Register pre-computed signature -> character mappings into reference table."""
        target = {
            "exact": self._exact_refs,
            "shape": self._shape_refs,
            "aspect": self._aspect_refs,
        }.get(kind)
        if target is None:
            raise ValueError(f"Unknown signature kind {kind!r}; expected 'exact', 'shape', or 'aspect'")
        count = 0
        for sig, ch in signature_to_char.items():
            if not sig or not ch:
                continue
            target[str(sig)] = str(ch)
            count += 1
        return count

    def save_reference_profile(self, profile_path: Path | str) -> Path:
        out_path = Path(profile_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(SHANGHAI_TZ).isoformat(),
            "exact_refs": dict(sorted(self._exact_refs.items())),
            "shape_refs": dict(sorted(self._shape_refs.items())),
            "aspect_refs": dict(sorted(self._aspect_refs.items())),
        }
        tmp_path = out_path.with_name(f"{out_path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, out_path)
        return out_path

    def load_reference_profile(self, profile_path: Path | str) -> int:
        path = Path(profile_path).resolve()
        if not path.is_file():
            raise FontDecodeError(f"Reference profile does not exist: '{path}'")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FontDecodeError(f"Failed to load reference profile '{path}': {exc}") from exc
        if not isinstance(payload, dict):
            raise FontDecodeError(f"Reference profile root must be a JSON object: '{path}'")
        count = 0
        count += self.register_reference_map(payload.get("exact_refs", {}), kind="exact")
        self.register_reference_map(payload.get("shape_refs", {}), kind="shape")
        self.register_reference_map(payload.get("aspect_refs", {}), kind="aspect")
        return count

    def _cache_file_for_sha(self, font_sha256: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"font_map_{font_sha256}.json"

    def _load_cached_mapping(self, font_sha256: str) -> Optional[Dict[int, str]]:
        if font_sha256 in self._font_map_cache:
            return dict(self._font_map_cache[font_sha256])
        cache_file = self._cache_file_for_sha(font_sha256)
        if cache_file is not None and cache_file.is_file():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                raw_map = data.get("mapping", {})
                loaded = {int(k): str(v) for k, v in raw_map.items()}
                self._font_map_cache[font_sha256] = loaded
                return dict(loaded)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise FontDecodeError(f"Corrupted cached font map '{cache_file}': {exc}") from exc
        return None

    def _save_cached_mapping(self, font_sha256: str, mapping: Mapping[int, str]) -> None:
        clean_map = {int(k): str(v) for k, v in mapping.items()}
        self._font_map_cache[font_sha256] = clean_map
        cache_file = self._cache_file_for_sha(font_sha256)
        if cache_file is not None:
            payload = {
                "font_sha256": font_sha256,
                "cached_at": datetime.now(SHANGHAI_TZ).isoformat(),
                "mapping": {str(k): v for k, v in sorted(clean_map.items())},
            }
            tmp_file = cache_file.with_name(f"{cache_file.name}.tmp")
            tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_file, cache_file)

    def _match_signature(self, sigs: GlyphOutlineSignatures) -> Optional[str]:
        if sigs.exact_hash in self._exact_refs:
            return self._exact_refs[sigs.exact_hash]
        if sigs.shape_hash in self._shape_refs:
            return self._shape_refs[sigs.shape_hash]
        if sigs.aspect_hash in self._aspect_refs:
            return self._aspect_refs[sigs.aspect_hash]
        return None

    def build_font_mapping(
        self,
        font_source: bytes | bytearray | Path | str,
        *,
        use_cache: bool = True,
    ) -> Tuple[str, Dict[int, str], bool]:
        """Parse a .woff2 font and build PUA codepoint -> Unicode char mapping keyed by SHA256."""
        font_bytes = _read_font_bytes(font_source, max_bytes=MAX_OBFUSCATED_FONT_BYTES)
        font_sha256 = hashlib.sha256(font_bytes).hexdigest()

        if use_cache:
            cached = self._load_cached_mapping(font_sha256)
            if cached is not None:
                return font_sha256, cached, True

        try:
            ttfont = TTFont(io.BytesIO(font_bytes))
        except Exception as exc:
            raise FontDecodeError(f"Failed to parse WOFF2 font ({font_sha256[:12]}): {exc}") from exc

        try:
            cmap = ttfont.getBestCmap()
            if not cmap:
                raise FontDecodeError(f"WOFF2 font ({font_sha256[:12]}) has no Unicode cmap table")

            mapping: Dict[int, str] = {}
            for cp, glyph_name in cmap.items():
                if not is_pua_codepoint(cp):
                    continue
                try:
                    sigs = extract_glyph_signatures(ttfont, glyph_name)
                except FontDecodeError:
                    raise
                except Exception as exc:
                    raise FontDecodeError(
                        f"Failed extracting glyph {glyph_name!r} from WOFF2 font ({font_sha256[:12]}): {exc}"
                    ) from exc
                if sigs.contour_ops == 0:
                    continue
                matched = self._match_signature(sigs)
                if matched is not None:
                    mapping[int(cp)] = matched
        except FontDecodeError:
            raise
        except Exception as exc:
            raise FontDecodeError(
                f"Failed to read WOFF2 font tables ({font_sha256[:12]}): {exc}"
            ) from exc
        finally:
            ttfont.close()

        self._save_cached_mapping(font_sha256, mapping)
        return font_sha256, mapping, False

    def decode_text(
        self,
        text: str,
        font_source: bytes | bytearray | Path | str | None = None,
        *,
        font_sha256: Optional[str] = None,
        context_label: str = "decoded_text",
    ) -> str:
        """Decode PUA-obfuscated text using font_source or cached font_sha256, failing on ANY residual PUA."""
        res = self.decode_text_detailed(
            text,
            font_source=font_source,
            font_sha256=font_sha256,
            context_label=context_label,
        )
        return res.decoded_text

    def decode_text_detailed(
        self,
        text: str,
        font_source: bytes | bytearray | Path | str | None = None,
        *,
        font_sha256: Optional[str] = None,
        context_label: str = "decoded_text",
    ) -> FontDecodeResult:
        if not isinstance(text, str):
            raise FontDecodeError(f"Expected str to decode, got {type(text).__name__}")

        if font_source is not None:
            resolved_sha, mapping, cache_hit = self.build_font_mapping(font_source)
        elif font_sha256 is not None:
            cached = self._load_cached_mapping(font_sha256)
            if cached is None:
                raise FontDecodeError(f"No cached font mapping found for sha256 '{font_sha256}'")
            resolved_sha = font_sha256
            mapping = cached
            cache_hit = True
        else:
            raise FontDecodeError("Either font_source or font_sha256 must be provided to decode_text")

        pua_chars = [ch for ch in text if is_pua_codepoint(ord(ch))]
        decoded = text.translate(mapping) if mapping else text
        assert_no_residual_pua(decoded, context_label=context_label)
        return FontDecodeResult(
            font_sha256=resolved_sha,
            decoded_text=decoded,
            mapped_count=len(pua_chars),
            total_pua_chars=len(pua_chars),
            cache_hit=cache_hit,
        )

    def decode_hotlist_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        font_source: bytes | bytearray | Path | str | None = None,
        *,
        font_sha256: Optional[str] = None,
        text_fields: Sequence[str] = ("rank", "title", "author", "heat", "category", "summary"),
    ) -> List[Dict[str, Any]]:
        """Decode all string fields in Tomato hotlist entries; aborts immediately on ANY residual PUA."""
        if font_source is not None:
            resolved_sha, _, _ = self.build_font_mapping(font_source)
        elif font_sha256 is not None:
            resolved_sha = font_sha256
        else:
            raise FontDecodeError("font_source or font_sha256 is required to decode hotlist entries")

        decoded_entries: List[Dict[str, Any]] = []
        for idx, item in enumerate(entries):
            copied = dict(item)
            for fld in text_fields:
                val = copied.get(fld)
                if isinstance(val, str) and val:
                    copied[fld] = self.decode_text(
                        val,
                        font_sha256=resolved_sha,
                        context_label=f"hotlist[{idx}].{fld}",
                    )
            decoded_entries.append(copied)
        return decoded_entries


__all__ = [
    "FontDecodeError",
    "FontDecodeResidualError",
    "FontDecodeResult",
    "GlyphOutlineSignatures",
    "PUA_RANGE_END",
    "PUA_RANGE_START",
    "PUA_RESIDUAL_RE",
    "Woff2FontDecoder",
    "assert_no_residual_pua",
    "extract_glyph_signatures",
    "is_pua_codepoint",
    "scan_residual_pua",
]
