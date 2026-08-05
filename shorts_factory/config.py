"""Load and validate channel + format-template YAML configs."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_ROOT = Path("config")


class SourceStrategy(str, Enum):
    LONGFORM_HIGHLIGHT_CUT = "longform_highlight_cut"
    VIRAL_TRANSLATE_DUB = "viral_translate_dub"
    TEMPLATE_CARD = "template_card"


class ChannelConfig(BaseModel):
    name: str
    source_strategy: SourceStrategy
    format_template: str
    niche_description: str = Field(
        description="Short prose description used as context for the LLM niche-fit filter and prompts"
    )
    search_keywords: list[str] = Field(
        default_factory=list,
        description="Keyword variants rotated across discovery cycles (longform_highlight_cut / viral_translate_dub only)",
    )
    layout_config_ref: str | None = Field(
        default=None, description="config/layouts/<ref>.yaml — required for longform_highlight_cut on gaming niches"
    )
    gcp_project_ref: str = Field(description="Identifies which GCP project's OAuth creds + quota pool this channel uses")
    min_duration_s: int = 180  # 3 minutes, matches EasyCut's stated source constraint
    max_duration_s: int = 3600  # 60 minutes
    target_clip_count: int = 10
    daily_publish_count: int = 1
    needs_translation: bool = False
    target_language: str = "ko"

    @field_validator("search_keywords")
    @classmethod
    def _require_keywords_for_video_strategies(cls, v: list[str], info) -> list[str]:
        strategy = info.data.get("source_strategy")
        if strategy in (SourceStrategy.LONGFORM_HIGHLIGHT_CUT, SourceStrategy.VIRAL_TRANSLATE_DUB) and not v:
            raise ValueError(f"search_keywords is required for source_strategy={strategy}")
        return v


class HookTitleConfig(BaseModel):
    position: str = "top"  # "top" | "top_bottom"
    banner_color: str = "#FFE600"
    text_color: str = "#000000"
    lines: int = 2


class SecondaryCaptionConfig(BaseModel):
    enabled: bool = False
    source: str = "description"  # "translation" | "description" | "original_subcaption"
    position: str = "bottom"


class SourceAttributionConfig(BaseModel):
    enabled: bool = False
    text_template: str = "@{handle}"  # or "출처: {name}"
    position: str = "bottom_center"


class ShoppingTagConfig(BaseModel):
    enabled: bool = False
    # Actual product/link populated later once the user has something to promote — slot only for now.


class CommentOverlayConfig(BaseModel):
    enabled: bool = False
    # Text is always LLM-generated (comment_generator.py) — never scraped from a real account.


class FormatTemplateConfig(BaseModel):
    name: str
    hook_title: HookTitleConfig = Field(default_factory=HookTitleConfig)
    secondary_caption: SecondaryCaptionConfig = Field(default_factory=SecondaryCaptionConfig)
    source_attribution: SourceAttributionConfig = Field(default_factory=SourceAttributionConfig)
    shopping_tag: ShoppingTagConfig = Field(default_factory=ShoppingTagConfig)
    comment_overlay: CommentOverlayConfig = Field(default_factory=CommentOverlayConfig)
    karaoke_caption_style: str = "default"


def load_channel_config(name: str, root: Path = DEFAULT_CONFIG_ROOT) -> ChannelConfig:
    path = root / "channels" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No channel config at {path}")
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("name", name)
    return ChannelConfig(**data)


def load_format_template(name: str, root: Path = DEFAULT_CONFIG_ROOT) -> FormatTemplateConfig:
    path = root / "formats" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No format template at {path}")
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("name", name)
    return FormatTemplateConfig(**data)


def load_layout_config(ref: str, root: Path = DEFAULT_CONFIG_ROOT) -> dict:
    path = root / "layouts" / f"{ref}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No layout config at {path}")
    return yaml.safe_load(path.read_text()) or {}


def list_channel_names(root: Path = DEFAULT_CONFIG_ROOT) -> list[str]:
    channels_dir = root / "channels"
    if not channels_dir.exists():
        return []
    return sorted(p.stem for p in channels_dir.glob("*.yaml"))
