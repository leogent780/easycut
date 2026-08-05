"""Reference-video discovery: find a proven-viral YouTube source per channel per cycle.

Core principle from the plan: "무조건 조회수가 터진 레퍼런스를 들고온다" — but raw view count
alone favors old evergreen videos over what's *actually* blowing up right now, so candidates
are ranked by view velocity (views / days since publish), not cumulative view count.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from shorts_factory.config import ChannelConfig
from shorts_factory.integrations import youtube_api
from shorts_factory.integrations.claude_client import MODEL_HAIKU, call_structured
from shorts_factory.integrations.youtube_api import VideoCandidate
from shorts_factory.pipeline import quota
from shorts_factory.state import AuditLevel, Channel, is_source_already_processed, log_audit

DISCOVERY_WINDOW_DAYS = 7  # only consider videos published in this window — see plan's velocity rationale
MAX_KEYWORDS_PER_CYCLE = 2  # how many keyword variants to burn search.list quota on per cycle
MAX_SEARCH_RESULTS_PER_KEYWORD = 25
NICHE_FIT_SHORTLIST_SIZE = 8  # how many top-velocity candidates get the Haiku niche-fit check

NICHE_FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "is_good_fit": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["video_id", "is_good_fit", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["fits"],
    "additionalProperties": False,
}


@dataclass
class RankedCandidate:
    video: VideoCandidate
    velocity_score: float
    niche_fit_reason: str = ""


def _pick_keywords_for_cycle(keywords: list[str], cycle_seed: int) -> list[str]:
    """Round-robin rotation so the same top keyword doesn't return an identical result set daily."""
    if len(keywords) <= MAX_KEYWORDS_PER_CYCLE:
        return keywords
    start = cycle_seed % len(keywords)
    rotated = keywords[start:] + keywords[:start]
    return rotated[:MAX_KEYWORDS_PER_CYCLE]


def _video_duration_bucket(min_duration_s: int) -> str | None:
    """YouTube's videoDuration search param only accepts coarse buckets — used as a pre-filter
    hint only; exact min/max filtering happens afterward against real durations."""
    if min_duration_s >= 1200:  # 20 minutes
        return "long"
    return None


def _niche_fit_filter(
    candidates: list[RankedCandidate], niche_description: str
) -> list[RankedCandidate]:
    if not candidates:
        return []

    listing = "\n".join(f"- video_id={c.video.video_id} | title: {c.video.title}" for c in candidates)
    system_prompt = (
        "You check whether candidate YouTube videos actually match a channel's content niche, "
        "based only on their title (and you may reason about likely content). Keyword search can "
        "return off-topic hits that merely happen to contain a matching word — flag those as not "
        "a good fit."
    )
    user_content = (
        f"채널 니치 설명:\n{niche_description}\n\n후보 영상 목록:\n{listing}\n\n"
        "각 영상이 이 채널 니치에 실제로 맞는지 판단해서 fits 배열로 반환하세요."
    )
    result = call_structured(
        system_prompt=system_prompt,
        user_content=user_content,
        schema=NICHE_FIT_SCHEMA,
        tool_name="report_niche_fit",
        model=MODEL_HAIKU,
    )
    fit_by_id = {item["video_id"]: item for item in result.get("fits", [])}

    kept = []
    for c in candidates:
        fit = fit_by_id.get(c.video.video_id)
        if fit is None or fit.get("is_good_fit"):  # fail open: no verdict returned -> keep, don't silently drop
            c.niche_fit_reason = fit.get("reason", "") if fit else "no verdict returned — kept by default"
            kept.append(c)
    return kept


def discover_candidates(
    session: Session,
    channel: Channel,
    channel_config: ChannelConfig,
    job=None,
    cycle_seed: int | None = None,
) -> list[RankedCandidate]:
    """Return a ranked (best-first) list of source-video candidates for this channel's cycle.

    Caller (the strategy pipeline) attempts download/transcribe on candidates in order,
    falling through to the next on failure — see plan §실패 처리 원칙.
    """
    if channel_config.source_strategy.value == "template_card":
        return []  # no source video needed at all for this strategy

    cycle_seed = cycle_seed if cycle_seed is not None else datetime.date.today().toordinal()
    keywords = _pick_keywords_for_cycle(channel_config.search_keywords, cycle_seed)
    published_after = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DISCOVERY_WINDOW_DAYS)
    duration_bucket = _video_duration_bucket(channel_config.min_duration_s)

    service = youtube_api.get_service(channel.name)

    all_video_ids: set[str] = set()
    for keyword in keywords:
        if not quota.has_budget_for(session, channel_config.gcp_project_ref, youtube_api.SEARCH_LIST_COST):
            log_audit(session, job, "discover", f"quota exhausted before searching '{keyword}'", level=AuditLevel.WARNING)
            break
        ids = youtube_api.search_candidates(
            service,
            query=keyword,
            published_after=published_after,
            video_duration=duration_bucket,
            max_results=MAX_SEARCH_RESULTS_PER_KEYWORD,
        )
        quota.spend_search_list(session, channel_config.gcp_project_ref)
        all_video_ids.update(ids)

    if not all_video_ids:
        log_audit(session, job, "discover", "no candidates returned from search", level=AuditLevel.WARNING)
        return []

    if not quota.has_budget_for(session, channel_config.gcp_project_ref, youtube_api.VIDEOS_LIST_COST):
        log_audit(session, job, "discover", "quota exhausted before videos.list", level=AuditLevel.WARNING)
        return []

    details = youtube_api.get_video_details(service, list(all_video_ids))
    quota.spend_videos_list(session, channel_config.gcp_project_ref)

    filtered = [
        v
        for v in details
        if channel_config.min_duration_s <= v.duration_s <= channel_config.max_duration_s
        and not is_source_already_processed(session, v.video_id)
    ]

    ranked = sorted(
        (RankedCandidate(video=v, velocity_score=v.velocity_score) for v in filtered),
        key=lambda c: c.velocity_score,
        reverse=True,
    )
    shortlist = ranked[:NICHE_FIT_SHORTLIST_SIZE]

    final = _niche_fit_filter(shortlist, channel_config.niche_description)
    log_audit(
        session,
        job,
        "discover",
        f"{len(all_video_ids)} raw hits -> {len(filtered)} passed duration/dedupe -> {len(final)} passed niche-fit",
    )
    return final
