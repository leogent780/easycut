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


# Plain-language display metadata for the dashboard's format picker — kept separate from
# FormatTemplateConfig because that YAML is pipeline-facing (technical slot config), while
# this is presentation-facing (for a non-technical user choosing by look, not by key name).
# Each entry's example mirrors an actual reference the user provided when we designed these.
FORMAT_TEMPLATE_INFO = {
    "simple_hook_top": {
        "label": "기본형",
        "description": "상단 후킹 타이틀만. 게임클립처럼 소스 자체가 재밌는 채널에 적합.",
    },
    "dual_caption_translate": {
        "label": "번역/더빙형",
        "description": "상단 후킹 타이틀 + 하단 번역 자막 + 원 계정 출처 표기. 해외 바이럴 영상용.",
    },
    "dual_caption_shopping_tag": {
        "label": "라이프스타일/쇼핑태그형",
        "description": "상단 후킹 타이틀 + 하단 설명 캡션 + 쇼핑 태그. 육아·라이프스타일 채널용.",
    },
    "gossip_comment_overlay": {
        "label": "예능/가십형",
        "description": "상단 2줄 배너 + AI 생성 댓글 오버레이. 연예/이슈 채널용.",
    },
    "domestic_reclip_attribution": {
        "label": "국내 재클립형",
        "description": "상단 후킹 타이틀 + 하단 중앙 출처 표기. 국내 소스 재구성 채널용.",
    },
}


def list_format_template_names(root: Path = DEFAULT_CONFIG_ROOT) -> list[str]:
    formats_dir = root / "formats"
    if not formats_dir.exists():
        return []
    return sorted(p.stem for p in formats_dir.glob("*.yaml"))


def update_channel_format_template(name: str, new_format_template: str, root: Path = DEFAULT_CONFIG_ROOT) -> None:
    """Persist a format-template choice made in the dashboard back to the channel's YAML file
    — the YAML stays the single source of truth the pipeline actually reads every cycle
    (see pipeline/longform_highlight_cut.py), so a DB-only update would silently have no effect.

    Rewrites only the `format_template:` line in place (not a full YAML re-serialize) so the
    file's comments and formatting survive — these configs are meant to be hand-edited too.
    """
    path = root / "channels" / f"{name}.yaml"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("format_template:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}format_template: {new_format_template}\n"
            break
    else:
        lines.append(f"format_template: {new_format_template}\n")
    path.write_text("".join(lines), encoding="utf-8")


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
