from shorts_factory.integrations.ytdlp_client import _parse_vtt


def test_parse_vtt_basic_cues():
    sample = """WEBVTT
Kind: captions
Language: ko

00:00:00.000 --> 00:00:02.500
안녕하세요 여러분

00:00:02.500 --> 00:00:05.000
오늘은 <c>재밌는</c> 이야기를 해볼게요
"""
    cues = _parse_vtt(sample)
    assert len(cues) == 2
    assert cues[0].start_s == 0.0
    assert cues[0].end_s == 2.5
    assert cues[0].text == "안녕하세요 여러분"
    assert "<c>" not in cues[1].text  # inline styling tags stripped
    assert cues[1].text == "오늘은 재밌는 이야기를 해볼게요"


def test_parse_vtt_empty_content():
    assert _parse_vtt("WEBVTT\n") == []


def test_parse_vtt_dedupes_growing_autosub_repeats():
    # YouTube auto-subs commonly emit the same growing line across consecutive cues.
    sample = """WEBVTT

00:00:00.000 --> 00:00:01.000
안녕

00:00:01.000 --> 00:00:02.000
안녕
"""
    cues = _parse_vtt(sample)
    assert len(cues) == 1
