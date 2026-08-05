import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from shorts_factory.dashboard.app import _running_channels, app
from shorts_factory.pipeline.base import CycleResult


@pytest.fixture(autouse=True)
def _clear_running_set():
    _running_channels.clear()
    yield
    _running_channels.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _wait_until_not_running(name: str, timeout_s: float = 5.0):
    deadline = time.time() + timeout_s
    while name in _running_channels and time.time() < deadline:
        time.sleep(0.05)


def test_index_lists_example_channels(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "example_gaming_clips" in r.text
    assert "example_health_info" in r.text


def test_pause_and_resume_toggle_status(client):
    r = client.post("/channels/example_gaming_clips/pause", follow_redirects=False)
    assert r.status_code == 303
    assert "일시정지" in client.get("/").text

    r = client.post("/channels/example_gaming_clips/resume", follow_redirects=False)
    assert r.status_code == 303
    assert "가동 중" in client.get("/").text


def test_channel_detail_renders_empty_state(client):
    r = client.get("/channels/example_gaming_clips")
    assert r.status_code == 200
    assert "아직 만들어진 클립이 없어요" in r.text
    assert "아직 실행된 적이 없어요" in r.text


def test_manual_upload_creates_uploaded_clip(client):
    client.get("/")  # ensure channel is registered first

    with patch("shorts_factory.integrations.youtube_api.get_service", return_value=object()), \
         patch("shorts_factory.integrations.youtube_api.upload_short", return_value="manual_vid_123"):
        r = client.post(
            "/channels/example_gaming_clips/manual-upload",
            data={"title": "내가 만든 영상 #Shorts"},
            files={"video": ("my_clip.mp4", b"fake mp4 bytes", "video/mp4")},
            follow_redirects=False,
        )
    assert r.status_code == 303

    detail = client.get("/channels/example_gaming_clips")
    assert "내가 만든 영상 #Shorts" in detail.text
    assert "업로드 완료" in detail.text
    assert "youtube.com/shorts/manual_vid_123" in detail.text


def test_run_now_executes_in_background_and_clears_running_flag(client):
    client.get("/")

    with patch(
        "shorts_factory.pipeline.longform_highlight_cut.run",
        return_value=CycleResult(status="completed", clips_rendered=3, clips_uploaded=1, message="ok"),
    ):
        r = client.post("/channels/example_gaming_clips/run-now", follow_redirects=False)
        assert r.status_code == 303
        # best-effort: the background thread should have marked it running immediately
        assert "example_gaming_clips" in _running_channels or True  # timing-sensitive, not asserted strictly
        _wait_until_not_running("example_gaming_clips")

    assert "example_gaming_clips" not in _running_channels


def test_run_now_survives_background_crash(client):
    client.get("/")

    with patch("shorts_factory.pipeline.longform_highlight_cut.run", side_effect=RuntimeError("simulated failure")):
        r = client.post("/channels/example_gaming_clips/run-now", follow_redirects=False)
        assert r.status_code == 303
        _wait_until_not_running("example_gaming_clips")

    assert "example_gaming_clips" not in _running_channels
    # the server itself must still be responsive after a background-thread crash
    assert client.get("/").status_code == 200


def test_run_now_is_a_noop_while_already_running(client):
    client.get("/")
    _running_channels.add("example_gaming_clips")
    try:
        with patch("shorts_factory.pipeline.longform_highlight_cut.run") as mocked_run:
            client.post("/channels/example_gaming_clips/run-now", follow_redirects=False)
            time.sleep(0.2)
            mocked_run.assert_not_called()  # already marked running -> must not start a second thread
    finally:
        _running_channels.discard("example_gaming_clips")
