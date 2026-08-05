"""YouTube Data API v3 wrapper: OAuth (per-channel installed-app flow), search/candidate
lookup, and resumable Shorts upload.

Known per-call quota costs (default project quota is 10,000 units/day — see plan
§유튜브 API 핵심 사항 for why this is a near-term hard constraint, not a rounding error):
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shorts_factory import secrets

SEARCH_LIST_COST = 100
VIDEOS_LIST_COST = 1  # per call, batched up to 50 ids — always batch, never call per-video
VIDEOS_INSERT_COST = 1600

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

_ISO8601_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_iso8601_duration(duration: str) -> int:
    """'PT15M33S' -> 933 (seconds). Returns 0 for an unparseable/live-stream duration string."""
    match = _ISO8601_DURATION_RE.match(duration)
    if not match:
        return 0
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


@dataclass
class VideoCandidate:
    video_id: str
    title: str
    description: str
    channel_title: str
    published_at: datetime
    duration_s: int = 0
    view_count: int = 0
    embeddable: bool = True

    @property
    def days_since_published(self) -> float:
        delta = datetime.now(timezone.utc) - self.published_at
        return max(delta.total_seconds() / 86400.0, 0.01)  # floor to avoid divide-by-zero on same-day uploads

    @property
    def velocity_score(self) -> float:
        """'터짐 스코어' = views / days since publish — approximates view velocity, not cumulative rank."""
        return self.view_count / self.days_since_published


def run_oauth_flow_for_channel(channel_name: str, client_secrets_file: str) -> None:
    """One-time interactive OAuth flow — run once per channel, stores the refresh token in secrets.

    Must be run with the consent screen in 'In production' publishing status (see plan) or
    the issued refresh token will silently expire after 7 days.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)
    secrets.set_secret(secrets.youtube_refresh_token_key(channel_name), creds.refresh_token)
    secrets.set_secret(f"youtube_client_secrets_file:{channel_name}", client_secrets_file)


def _load_client_config(client_secrets_file: str) -> dict:
    import json

    with open(client_secrets_file) as f:
        return json.load(f)


def get_credentials(channel_name: str):
    """Build refreshable Credentials from the stored refresh token. Raises if the channel
    has never completed run_oauth_flow_for_channel."""
    from google.oauth2.credentials import Credentials

    refresh_token = secrets.get_secret(secrets.youtube_refresh_token_key(channel_name))
    if not refresh_token:
        raise RuntimeError(
            f"No stored YouTube refresh token for channel '{channel_name}' — run "
            f"run_oauth_flow_for_channel() once for this channel first."
        )
    client_secrets_file = secrets.get_secret(f"youtube_client_secrets_file:{channel_name}") or os.environ.get(
        "YOUTUBE_OAUTH_CLIENT_SECRETS_FILE"
    )
    client_config = _load_client_config(client_secrets_file)["installed"]

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=SCOPES,
    )


def get_service(channel_name: str):
    """Authorized googleapiclient service, with access-token refresh handled automatically."""
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = get_credentials(channel_name)
    creds.refresh(Request())  # surfaces invalid_grant immediately if the refresh token was revoked
    return build("youtube", "v3", credentials=creds)


def search_candidates(
    service,
    query: str,
    published_after: datetime,
    video_duration: str | None = None,  # "long" (>20min) | "short" (<4min) | None (any)
    max_results: int = 25,
    region_code: str = "KR",
) -> list[str]:
    """search.list — returns candidate video ids only (caller must batch through get_video_details
    for real stats; search.list's own view counts are not reliable/present). Costs SEARCH_LIST_COST."""
    request = service.search().list(
        part="id",
        q=query,
        type="video",
        order="viewCount",
        publishedAfter=published_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
        maxResults=max_results,
        regionCode=region_code,
        **({"videoDuration": video_duration} if video_duration else {}),
    )
    response = request.execute()
    return [item["id"]["videoId"] for item in response.get("items", [])]


def get_video_details(service, video_ids: list[str]) -> list[VideoCandidate]:
    """videos.list, batched up to 50 ids per call. Costs VIDEOS_LIST_COST per call regardless of batch size."""
    candidates: list[VideoCandidate] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        response = service.videos().list(part="snippet,contentDetails,statistics,status", id=",".join(batch)).execute()
        for item in response.get("items", []):
            snippet = item["snippet"]
            status = item.get("status", {})
            if status.get("privacyStatus") != "public" or not status.get("embeddable", True):
                continue
            candidates.append(
                VideoCandidate(
                    video_id=item["id"],
                    title=snippet.get("title", ""),
                    description=snippet.get("description", ""),
                    channel_title=snippet.get("channelTitle", ""),
                    published_at=datetime.strptime(snippet["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    ),
                    duration_s=parse_iso8601_duration(item.get("contentDetails", {}).get("duration", "PT0S")),
                    view_count=int(item.get("statistics", {}).get("viewCount", 0)),
                    embeddable=status.get("embeddable", True),
                )
            )
    return candidates


def upload_short(
    service,
    file_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    privacy_status: str = "public",
    category_id: str = "24",  # "Entertainment" — reasonable default, override per-niche if needed
) -> str:
    """videos.insert with resumable upload. Returns the uploaded video id. Costs VIDEOS_INSERT_COST.

    No explicit "make this a Short" flag exists — YouTube infers Shorts-shelf eligibility from
    the file's own vertical aspect ratio + duration <= 3min (see plan). Ensure `#Shorts` is
    present in title/description as a defensive discovery signal.
    """
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description if "#Shorts" in description else f"{description}\n\n#Shorts",
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy_status},
    }
    media = MediaFileUpload(file_path, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _status, response = request.next_chunk()
    return response["id"]
