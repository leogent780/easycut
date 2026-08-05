"""Per-GCP-project daily YouTube Data API quota tracking.

Default quota is 10,000 units/day *per project*, not per channel — this is why the plan
calls for one GCP project (or a small cluster) per channel rather than sharing one project
across all channels (see plan §유튜브 API 핵심 사항).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from shorts_factory.integrations.youtube_api import SEARCH_LIST_COST, VIDEOS_INSERT_COST, VIDEOS_LIST_COST
from shorts_factory.state import get_quota_used_today, record_quota_usage

DAILY_QUOTA_BUDGET = 10_000


def remaining_quota(session: Session, gcp_project_ref: str) -> int:
    return DAILY_QUOTA_BUDGET - get_quota_used_today(session, gcp_project_ref)


def has_budget_for(session: Session, gcp_project_ref: str, units: int) -> bool:
    return remaining_quota(session, gcp_project_ref) >= units


def spend(session: Session, gcp_project_ref: str, units: int) -> None:
    record_quota_usage(session, gcp_project_ref, units)


# Convenience wrappers so callers don't need to import the raw cost constants directly.


def spend_search_list(session: Session, gcp_project_ref: str) -> None:
    spend(session, gcp_project_ref, SEARCH_LIST_COST)


def spend_videos_list(session: Session, gcp_project_ref: str) -> None:
    spend(session, gcp_project_ref, VIDEOS_LIST_COST)


def spend_videos_insert(session: Session, gcp_project_ref: str) -> None:
    spend(session, gcp_project_ref, VIDEOS_INSERT_COST)


def has_budget_for_upload(session: Session, gcp_project_ref: str) -> bool:
    return has_budget_for(session, gcp_project_ref, VIDEOS_INSERT_COST)
