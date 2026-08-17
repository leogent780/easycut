"""Playwright-rendered PNGs for the viral_translate_dub strategy's two overlay layers, both
using the SAME typeface (Pretendard Black) per explicit user request — captions previously used
a separate font (Do Hyeon) but the user asked to just match the top hook-banner font instead:

1. Hook banner — fixed 1080x353px, #000000 background, Pretendard Black, white default text
   with one emphasized word/phrase in #FEF501, real typed text (not AI-generated letterforms
   in an image). Font size is auto-fit per render (not a fixed constant) so the banner text
   always fills close to the full banner width regardless of how long the hook title happens to
   be — matching a real reference screenshot the user provided where the copy visually fills
   almost the entire frame.
2. Short caption chunks — same Pretendard Black typeface, white fill with a thin black outline,
   sized/positioned per a style spec the user gave (see module-level constants below).

Both reuse the same Playwright-screenshot-a-transparent-page technique as
pipeline/overlay_render.py rather than sharing its renderer directly, because these two overlays
have fixed, hand-tuned specs (exact px box, exact stroke ratio) rather than the configurable
per-format-template slots overlay_render.py serves.
"""

from __future__ import annotations

from pathlib import Path

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
BANNER_HEIGHT = 353

# Hook banner auto-fit: render once at a trial size, measure actual rendered width, then scale
# to hit this fraction of the banner width — matches a reference screenshot where the copy (two
# lines, one in white / one in yellow) spans ~85-87% of the frame edge-to-edge.
BANNER_TARGET_WIDTH_FRACTION = 0.86
BANNER_TRIAL_FONT_SIZE = 80
BANNER_MIN_FONT_SIZE = 40
BANNER_MAX_FONT_SIZE = 120

# Caption chunk style: white fill / thin black outline, tight letter-spacing, sized so the
# outline reads as a deliberate thin stroke rather than a thick UI-style border.
CAPTION_FONT_SIZE = 76
CAPTION_STROKE_RATIO = 0.065
CAPTION_LETTER_SPACING_PX = -2

_FONT_CSS = """
@font-face {{
  font-family: "Pretendard Black";
  src: url("file://{pretendard_path}") format("opentype");
  font-weight: 900;
}}
"""

_BANNER_HTML_TEMPLATE = """<!doctype html><html><head><style>
{font_css}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {width}px; height: {height}px; background: #000000; }}
.titlebox {{
  width: {width}px; height: {height}px; background: #000000;
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px;
}}
.titlebox .line {{
  font-family: "Pretendard Black", sans-serif; font-weight: 900; font-size: {font_size}px; line-height: 1;
  color: #ffffff; white-space: nowrap; text-align: center;
}}
.titlebox .yellow {{ color: #fef501; }}
</style></head><body>
<div class="titlebox">
  <div class="line" id="line1">{line1}</div>
  <div class="line" id="line2">{line2_html}</div>
</div>
</body></html>"""

_CAPTION_HTML_TEMPLATE = """<!doctype html><html><head><style>
{font_css}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {width}px; height: {height}px; background: transparent; }}
.capbox {{
  position: absolute; left: 0; top: {caption_top}px; width: {width}px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px; padding: 0 60px;
}}
.capbox .line {{
  font-family: "Pretendard Black", sans-serif; font-weight: 900; font-size: {font_size}px; line-height: 1.3;
  letter-spacing: {letter_spacing}px;
  color: #ffffff; text-align: center; white-space: nowrap;
  -webkit-text-stroke: {stroke_width}px #000000; paint-order: stroke fill;
}}
</style></head><body>
<div class="capbox"><div class="line">{text}</div></div>
</body></html>"""

DEFAULT_CAPTION_TOP = 1480

PRETENDARD_BLACK_URL = (
    "https://raw.githubusercontent.com/orioncactus/pretendard/main/packages/pretendard/"
    "dist/public/static/Pretendard-Black.otf"
)


def ensure_pretendard_font(cache_dir: str | Path) -> Path:
    """Download+cache the exact webfont file the banner (and now caption) spec requires (real
    typed text, not an AI-generated image of letterforms). `raw.githubusercontent.com` is
    reachable even from network-restricted environments where e.g. jsdelivr's CDN is blocked.
    """
    return _ensure_font(cache_dir, "Pretendard-Black.otf", PRETENDARD_BLACK_URL)


def _ensure_font(cache_dir: str | Path, filename: str, url: str) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    font_path = cache_dir / filename
    if not font_path.exists():
        import httpx

        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        font_path.write_bytes(resp.content)
    return font_path


def _wrap_emphasis(line: str, emphasis_word: str | None) -> str:
    if emphasis_word and emphasis_word in line:
        return line.replace(emphasis_word, f'<span class="yellow">{emphasis_word}</span>')
    return line


def render_hook_banner(
    line1: str,
    line2: str,
    emphasis_word: str | None,
    pretendard_font_path: str | Path,
    output_path: str | Path,
    chromium_executable_path: str | None = None,
) -> Path:
    """Render the fixed 1080x353 hook banner. `emphasis_word` (if it appears verbatim in
    line1 or line2) is colored #FEF501; everything else stays white.

    Font size is auto-fit: render once at a trial size, measure the longer line's actual
    rendered width, then scale so it hits BANNER_TARGET_WIDTH_FRACTION of the banner width
    (clamped to [BANNER_MIN_FONT_SIZE, BANNER_MAX_FONT_SIZE]) and re-render at that size.
    """
    from playwright.sync_api import sync_playwright

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    line1_html = _wrap_emphasis(line1, emphasis_word)
    line2_html = _wrap_emphasis(line2, emphasis_word)
    font_css = _FONT_CSS.format(pretendard_path=Path(pretendard_font_path).resolve())

    with sync_playwright() as p:
        launch_kwargs = {"executable_path": chromium_executable_path} if chromium_executable_path else {}
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": CANVAS_WIDTH, "height": BANNER_HEIGHT})

        trial_html = _BANNER_HTML_TEMPLATE.format(
            font_css=font_css, width=CANVAS_WIDTH, height=BANNER_HEIGHT,
            font_size=BANNER_TRIAL_FONT_SIZE, line1=line1_html, line2_html=line2_html,
        )
        page.set_content(trial_html)
        page.wait_for_timeout(100)
        widths = page.eval_on_selector_all(
            "#line1, #line2", "els => els.map(e => e.getBoundingClientRect().width)"
        )
        measured_width = max(widths)

        target_width = CANVAS_WIDTH * BANNER_TARGET_WIDTH_FRACTION
        scale = target_width / measured_width if measured_width > 0 else 1.0
        final_font_size = max(
            BANNER_MIN_FONT_SIZE, min(BANNER_MAX_FONT_SIZE, round(BANNER_TRIAL_FONT_SIZE * scale))
        )

        final_html = _BANNER_HTML_TEMPLATE.format(
            font_css=font_css, width=CANVAS_WIDTH, height=BANNER_HEIGHT,
            font_size=final_font_size, line1=line1_html, line2_html=line2_html,
        )
        page.set_content(final_html)
        page.wait_for_timeout(100)
        page.screenshot(path=str(output_path), omit_background=False)
        browser.close()
    return output_path


def render_caption_chunk(
    text: str,
    pretendard_font_path: str | Path,
    output_path: str | Path,
    caption_top: int = DEFAULT_CAPTION_TOP,
    chromium_executable_path: str | None = None,
) -> Path:
    """Render one short caption chunk as a full-canvas transparent PNG (Pretendard Black, white
    fill / thin black outline — same typeface as the hook banner), positioned at `caption_top`.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_css = _FONT_CSS.format(pretendard_path=Path(pretendard_font_path).resolve())
    stroke_width = round(CAPTION_FONT_SIZE * CAPTION_STROKE_RATIO, 1)
    html = _CAPTION_HTML_TEMPLATE.format(
        font_css=font_css,
        width=CANVAS_WIDTH,
        height=CANVAS_HEIGHT,
        caption_top=caption_top,
        text=text,
        font_size=CAPTION_FONT_SIZE,
        stroke_width=stroke_width,
        letter_spacing=CAPTION_LETTER_SPACING_PX,
    )
    _screenshot(html, CANVAS_WIDTH, CANVAS_HEIGHT, output_path, chromium_executable_path, transparent=True)
    return output_path


def _screenshot(
    html: str, width: int, height: int, output_path: Path, chromium_executable_path: str | None, transparent: bool = False
) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        launch_kwargs = {"executable_path": chromium_executable_path} if chromium_executable_path else {}
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.set_content(html)
        page.wait_for_timeout(100)
        page.screenshot(path=str(output_path), omit_background=transparent)
        browser.close()
