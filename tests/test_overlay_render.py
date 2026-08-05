import pytest

from shorts_factory.config import load_format_template
from shorts_factory.pipeline.overlay_render import OverlayContent, build_html, render_overlay_png


def test_build_html_includes_enabled_slots_only():
    fmt = load_format_template("gossip_comment_overlay")  # hook_title + comment_overlay enabled, rest off
    content = OverlayContent(
        hook_title_lines=["제목1", "제목2"],
        comment_author="익명1",
        comment_text="댓글 내용",
        secondary_caption_text="이건 안 보여야 함",  # secondary_caption is disabled in this template
    )
    html = build_html(fmt, content)

    assert "제목1" in html and "제목2" in html
    assert "익명1" in html and "댓글 내용" in html
    assert "이건 안 보여야 함" not in html  # disabled slot must not leak into the output


def test_build_html_omits_hook_title_when_no_lines_given():
    fmt = load_format_template("simple_hook_top")
    html = build_html(fmt, OverlayContent())
    # the .hook-title CSS rule is always present (shared static stylesheet) — what matters is
    # that no <div> for it gets emitted into the body when there's no content to show.
    assert 'class="hook-title"' not in html


def test_render_overlay_png_produces_transparent_image(tmp_path):
    """Real Playwright render — skipped if no browser is installed (this sandbox ships a
    nonstandard chromium build; on the user's real Mac/PC, `playwright install chromium`
    makes this pass for real, which is part of Phase 1 setup verification)."""
    fmt = load_format_template("simple_hook_top")
    content = OverlayContent(hook_title_lines=["테스트 타이틀"])
    output_path = tmp_path / "overlay.png"

    try:
        render_overlay_png(fmt, content, output_path)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright browser not installed in this environment — run `playwright install chromium`")
        raise

    assert output_path.exists()
    from PIL import Image

    img = Image.open(output_path)
    assert img.mode == "RGBA"
    assert img.getpixel((10, 10))[3] == 0  # corner pixel must be transparent (alpha=0)
