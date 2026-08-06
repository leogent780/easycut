import yaml

from shorts_factory import config as config_module
from shorts_factory.dashboard.app import app
from fastapi.testclient import TestClient


def test_channel_detail_lists_all_format_options():
    client = TestClient(app)
    r = client.get("/channels/example_gaming_clips")
    assert r.status_code == 200
    for key in config_module.list_format_template_names():
        assert key in r.text
    assert "현재 적용중" in r.text  # the currently-configured template is marked


def test_set_format_updates_yaml_and_db(tmp_path):
    # copy the example channel config into an isolated config root so this test never
    # mutates the real repo file
    config_root = tmp_path / "config"
    (config_root / "channels").mkdir(parents=True)
    (config_root / "formats").mkdir(parents=True)
    for src in (config_module.DEFAULT_CONFIG_ROOT / "formats").glob("*.yaml"):
        (config_root / "formats" / src.name).write_text(src.read_text())

    channel_yaml = config_root / "channels" / "test_format_channel.yaml"
    channel_yaml.write_text(
        "source_strategy: longform_highlight_cut\n"
        "format_template: simple_hook_top\n"
        "niche_description: test\n"
        "search_keywords: [x]\n"
        "gcp_project_ref: proj\n"
    )

    config_module.update_channel_format_template("test_format_channel", "gossip_comment_overlay", root=config_root)

    updated = yaml.safe_load(channel_yaml.read_text())
    assert updated["format_template"] == "gossip_comment_overlay"
    # other fields must survive the rewrite untouched
    assert updated["search_keywords"] == ["x"]
    assert updated["gcp_project_ref"] == "proj"


def test_set_format_rejects_unknown_template():
    client = TestClient(app)
    r = client.post(
        "/channels/example_gaming_clips/format",
        data={"format_template": "not_a_real_template"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # unknown template must not have been written anywhere
    cfg = config_module.load_channel_config("example_gaming_clips")
    assert cfg.format_template != "not_a_real_template"
