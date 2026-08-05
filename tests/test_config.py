import pytest

from shorts_factory.config import (
    ChannelConfig,
    SourceStrategy,
    list_channel_names,
    load_channel_config,
    load_format_template,
)


def test_list_channel_names_finds_examples():
    names = list_channel_names()
    assert "example_gaming_clips" in names
    assert "example_health_info" in names


def test_load_channel_config_gaming():
    cfg = load_channel_config("example_gaming_clips")
    assert cfg.source_strategy == SourceStrategy.LONGFORM_HIGHLIGHT_CUT
    assert cfg.layout_config_ref == "example_streamer_facecam"
    assert len(cfg.search_keywords) >= 1


def test_load_format_template_gossip():
    tmpl = load_format_template("gossip_comment_overlay")
    assert tmpl.comment_overlay.enabled is True
    assert tmpl.secondary_caption.enabled is False


def test_channel_config_requires_keywords_for_video_strategies():
    with pytest.raises(ValueError):
        ChannelConfig(
            name="x",
            source_strategy=SourceStrategy.LONGFORM_HIGHLIGHT_CUT,
            format_template="simple_hook_top",
            niche_description="test",
            search_keywords=[],  # invalid: required for this strategy
            gcp_project_ref="proj",
        )


def test_channel_config_allows_no_keywords_for_template_card():
    cfg = ChannelConfig(
        name="x",
        source_strategy=SourceStrategy.TEMPLATE_CARD,
        format_template="simple_hook_top",
        niche_description="test",
        search_keywords=[],
        gcp_project_ref="proj",
    )
    assert cfg.search_keywords == []
