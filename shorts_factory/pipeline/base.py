"""Strategy dispatch: routes a channel's cycle to its configured source_strategy pipeline.

Phase 1 implements only `longform_highlight_cut` (the riskiest pipeline — proves the shared
infra: OAuth, quota, state, render). `viral_translate_dub` and `template_card` are Phase 2
additions that plug into the same shared render/upload/state modules (see plan §단계별 빌드 순서).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from shorts_factory.config import ChannelConfig, SourceStrategy
from shorts_factory.state import Channel


@dataclass
class CycleResult:
    status: str  # "completed" | "failed" | "no_output"
    clips_rendered: int = 0
    clips_uploaded: int = 0
    message: str = ""


def run_cycle(session: Session, channel: Channel, channel_config: ChannelConfig) -> CycleResult:
    if channel_config.source_strategy == SourceStrategy.LONGFORM_HIGHLIGHT_CUT:
        from shorts_factory.pipeline import longform_highlight_cut

        result = longform_highlight_cut.run(session, channel, channel_config)
        channel.last_run_at = datetime.datetime.now(datetime.timezone.utc)
        session.commit()
        return result

    raise NotImplementedError(
        f"source_strategy={channel_config.source_strategy.value} is not implemented yet "
        f"(planned for Phase 2 — see plan §단계별 빌드 순서)"
    )
