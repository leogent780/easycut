"""Format-template overlay rendering: hook title / secondary caption / source attribution /
shopping tag / comment overlay slots -> HTML/CSS -> Playwright screenshot -> transparent PNG.

This PNG is composited over the cropped/scaled video via ffmpeg's `overlay` filter in
render.py. Chosen over hand-built ffmpeg drawtext/box chains because these layouts (badges,
banners, speech-bubble comment overlays) are exactly what CSS is good at and drawtext is not.
Reused as-is for the card-rendering strategy (template_card) — see card_renderer.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shorts_factory.config import FormatTemplateConfig

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920


@dataclass
class OverlayContent:
    hook_title_lines: list[str] = field(default_factory=list)
    secondary_caption_text: str | None = None
    source_attribution_text: str | None = None
    comment_author: str | None = None
    comment_text: str | None = None
    shopping_tag_label: str | None = None


def _hook_title_html(cfg, content: OverlayContent) -> str:
    if not content.hook_title_lines:
        return ""
    lines_html = "".join(f"<div>{line}</div>" for line in content.hook_title_lines[: cfg.lines])
    position_css = "top: 80px;" if cfg.position in ("top", "top_bottom") else "bottom: 80px;"
    return f"""
    <div class="hook-title" style="{position_css} background:{cfg.banner_color}; color:{cfg.text_color};">
      {lines_html}
    </div>
    """


def _secondary_caption_html(cfg, content: OverlayContent) -> str:
    if not cfg.enabled or not content.secondary_caption_text:
        return ""
    position_css = "bottom: 260px;" if cfg.position == "bottom" else "top: 260px;"
    return f"""
    <div class="secondary-caption" style="{position_css}">{content.secondary_caption_text}</div>
    """


def _source_attribution_html(cfg, content: OverlayContent) -> str:
    if not cfg.enabled or not content.source_attribution_text:
        return ""
    position_map = {
        "bottom_center": "bottom: 40px; left: 50%; transform: translateX(-50%);",
        "bottom_right": "bottom: 40px; right: 40px;",
        "bottom_left": "bottom: 40px; left: 40px;",
    }
    position_css = position_map.get(cfg.position, position_map["bottom_center"])
    return f"""
    <div class="source-attribution" style="{position_css}">{content.source_attribution_text}</div>
    """


def _comment_overlay_html(cfg, content: OverlayContent) -> str:
    if not cfg.enabled or not content.comment_text:
        return ""
    author = content.comment_author or "익명"
    return f"""
    <div class="comment-overlay">
      <div class="comment-author">{author}</div>
      <div class="comment-text">{content.comment_text}</div>
    </div>
    """


def _shopping_tag_html(cfg, content: OverlayContent) -> str:
    if not cfg.enabled or not content.shopping_tag_label:
        return ""
    return f"""
    <div class="shopping-tag">{content.shopping_tag_label} <span class="arrow">↗</span></div>
    """


_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
html, body { width: 1080px; height: 1920px; background: transparent; }
.hook-title {
  position: absolute; left: 50%; transform: translateX(-50%);
  width: 940px; padding: 24px 32px; border-radius: 16px;
  font-size: 56px; font-weight: 800; line-height: 1.3; text-align: center;
}
.secondary-caption {
  position: absolute; left: 50%; transform: translateX(-50%);
  width: 900px; padding: 16px 28px; border-radius: 12px;
  background: rgba(0,0,0,0.75); color: #fff; font-size: 36px; text-align: center;
}
.source-attribution {
  position: absolute; color: rgba(255,255,255,0.85); font-size: 28px; font-weight: 600;
  text-shadow: 0 1px 4px rgba(0,0,0,0.8);
}
.comment-overlay {
  position: absolute; bottom: 420px; left: 60px; width: 700px;
  background: #1c1c1e; color: #fff; border-radius: 20px; padding: 24px 28px;
}
.comment-overlay .comment-author { font-size: 26px; color: #8e8e93; margin-bottom: 8px; }
.comment-overlay .comment-text { font-size: 32px; line-height: 1.4; }
.shopping-tag {
  position: absolute; bottom: 300px; right: 50px;
  background: #fff; color: #000; border-radius: 40px; padding: 16px 28px;
  font-size: 30px; font-weight: 700; box-shadow: 0 4px 16px rgba(0,0,0,0.4);
}
"""


def build_html(format_template: FormatTemplateConfig, content: OverlayContent) -> str:
    body = (
        _hook_title_html(format_template.hook_title, content)
        + _secondary_caption_html(format_template.secondary_caption, content)
        + _source_attribution_html(format_template.source_attribution, content)
        + _comment_overlay_html(format_template.comment_overlay, content)
        + _shopping_tag_html(format_template.shopping_tag, content)
    )
    return f"<!doctype html><html><head><style>{_CSS}</style></head><body>{body}</body></html>"


def render_overlay_png(
    format_template: FormatTemplateConfig,
    content: OverlayContent,
    output_path: str | Path,
    chromium_executable_path: str | None = None,
) -> Path:
    """Render the format-template overlay to a transparent PNG at canvas resolution.

    `chromium_executable_path` is normally left None — a plain `playwright install chromium`
    on the user's own machine (per plan: local Mac/PC) resolves the browser automatically.
    The override exists only for environments with a pre-installed browser at a nonstandard
    path (e.g. this dev sandbox), so tests here don't require a fresh Playwright download.
    """
    from playwright.sync_api import sync_playwright

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = build_html(format_template, content)

    with sync_playwright() as p:
        launch_kwargs = {"executable_path": chromium_executable_path} if chromium_executable_path else {}
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT})
        page.set_content(html)
        page.screenshot(path=str(output_path), omit_background=True)
        browser.close()
    return output_path
