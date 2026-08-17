"""Playwright-rendered PNGs for the viral_translate_dub strategy's two overlay layers:

1. Hook banner — fixed 1080x353px, #000000 background, Pretendard Black, white default text
   with one emphasized word/phrase in #FEF501, real typed text (not AI-generated letterforms
   in an image). Exact spec the user gave when this was first validated by hand.
2. Short caption chunks — black text with a thick white outline, sized/positioned to match a
   real high-performing reference short the user provided (see
   samples/xhs_dish_brush_ko_dub_v2/README.md for the pixel-measurement this was derived from).

Both reuse the same Playwright-screenshot-a-transparent-page technique as
pipeline/overlay_render.py rather than sharing its renderer directly, because these two overlays
have fixed, hand-tuned specs (exact px box, exact stroke width) rather than the configurable
per-format-template slots overlay_render.py serves.
"""

from __future__ import annotations

from pathlib import Path

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
BANNER_HEIGHT = 353

_FONT_CSS = """
@font-face {{
  font-family: "Pretendard Black";
  src: url("file://{font_path}") format("opentype");
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
  font-family: "Pretendard Black", sans-serif; font-weight: 900; font-size: 62px; line-height: 1;
  color: #ffffff; white-space: nowrap; text-align: center;
}}
.titlebox .yellow {{ color: #fef501; }}
</style></head><body>
<div class="titlebox">
  <div class="line">{line1}</div>
  <div class="line">{line2_html}</div>
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
  font-family: "Pretendard Black", sans-serif; font-weight: 900; font-size: 62px; line-height: 1.3;
  color: #000000; text-align: center; white-space: nowrap;
  -webkit-text-stroke: 13px #ffffff; paint-order: stroke fill;
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
    """Download+cache the exact webfont file the banner/caption spec requires (real typed
    text, not an AI-generated image of letterforms). `raw.githubusercontent.com` is reachable
    even from network-restricted environments where e.g. jsdelivr's CDN is blocked.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    font_path = cache_dir / "Pretendard-Black.otf"
    if not font_path.exists():
        import httpx

        resp = httpx.get(PRETENDARD_BLACK_URL, timeout=30.0, follow_redirects=True)
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
    font_path: str | Path,
    output_path: str | Path,
    chromium_executable_path: str | None = None,
) -> Path:
    """Render the fixed 1080x353 hook banner. `emphasis_word` (if it appears verbatim in
    line1 or line2) is colored #FEF501; everything else stays white.
    """
    from playwright.sync_api import sync_playwright

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = _BANNER_HTML_TEMPLATE.format(
        font_css=_FONT_CSS.format(font_path=Path(font_path).resolve()),
        width=CANVAS_WIDTH,
        height=BANNER_HEIGHT,
        line1=_wrap_emphasis(line1, emphasis_word),
        line2_html=_wrap_emphasis(line2, emphasis_word),
    )
    _screenshot(html, CANVAS_WIDTH, BANNER_HEIGHT, output_path, chromium_executable_path)
    return output_path


def render_caption_chunk(
    text: str,
    font_path: str | Path,
    output_path: str | Path,
    caption_top: int = DEFAULT_CAPTION_TOP,
    chromium_executable_path: str | None = None,
) -> Path:
    """Render one short caption chunk as a full-canvas transparent PNG (black text / white
    outline), positioned at `caption_top`.
    """
    from playwright.sync_api import sync_playwright

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = _CAPTION_HTML_TEMPLATE.format(
        font_css=_FONT_CSS.format(font_path=Path(font_path).resolve()),
        width=CANVAS_WIDTH,
        height=CANVAS_HEIGHT,
        caption_top=caption_top,
        text=text,
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
