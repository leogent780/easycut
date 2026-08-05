"""CLI entrypoint. `dashboard` launches the Phase 3 web UI (shorts_factory/dashboard/app.py)
for users who'd rather click buttons in a browser than type commands — both read/write the
same SQLite state DB, so the CLI and the dashboard stay interchangeable and decoupled.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from sqlalchemy.orm import Session

from shorts_factory import config as config_module
from shorts_factory.pipeline.base import run_cycle
from shorts_factory.state import Channel, ChannelStatus, get_channel_by_name, get_engine

app = typer.Typer(help="쇼츠 팩토리 — local automation CLI")


def _db_path() -> Path:
    return Path(os.environ.get("SHORTS_FACTORY_DATA_DIR", "./data")) / "shorts_factory.db"


def _get_or_create_channel(session: Session, name: str, channel_config) -> Channel:
    channel = get_channel_by_name(session, name)
    if channel is None:
        channel = Channel(
            name=name,
            source_strategy=channel_config.source_strategy.value,
            format_template=channel_config.format_template,
            niche_keywords=channel_config.search_keywords,
            layout_config_ref=channel_config.layout_config_ref,
            gcp_project_ref=channel_config.gcp_project_ref,
        )
        session.add(channel)
        session.flush()
    return channel


@app.command("run-once")
def run_once(channel: str = typer.Option(..., "--channel", help="Channel name (config/channels/<channel>.yaml)")):
    """Run one full discover -> render -> upload cycle for a single channel, right now."""
    channel_config = config_module.load_channel_config(channel)
    engine = get_engine(_db_path())
    with Session(engine) as session:
        channel_row = _get_or_create_channel(session, channel, channel_config)
        session.commit()

        if channel_row.status == ChannelStatus.PAUSED.value:
            typer.echo(f"채널 '{channel}'은(는) 일시정지 상태입니다. 먼저 `resume`으로 재개하세요.")
            raise typer.Exit(code=1)
        if channel_row.status == ChannelStatus.NEEDS_REAUTH.value:
            typer.echo(f"채널 '{channel}'은(는) 재인증이 필요합니다 (needs_reauth). OAuth 플로우를 다시 실행하세요.")
            raise typer.Exit(code=1)

        result = run_cycle(session, channel_row, channel_config)
        session.commit()

        typer.echo(f"[{result.status}] {result.message}")
        if result.status != "completed":
            raise typer.Exit(code=1)


@app.command("pause")
def pause(channel: str = typer.Option(..., "--channel")):
    engine = get_engine(_db_path())
    with Session(engine) as session:
        channel_row = get_channel_by_name(session, channel)
        if channel_row is None:
            typer.echo(f"채널 '{channel}'을(를) 찾을 수 없습니다 (먼저 run-once를 한 번 실행해서 등록하세요).")
            raise typer.Exit(code=1)
        channel_row.status = ChannelStatus.PAUSED.value
        session.commit()
        typer.echo(f"채널 '{channel}' 일시정지됨.")


@app.command("resume")
def resume(channel: str = typer.Option(..., "--channel")):
    engine = get_engine(_db_path())
    with Session(engine) as session:
        channel_row = get_channel_by_name(session, channel)
        if channel_row is None:
            typer.echo(f"채널 '{channel}'을(를) 찾을 수 없습니다 (먼저 run-once를 한 번 실행해서 등록하세요).")
            raise typer.Exit(code=1)
        channel_row.status = ChannelStatus.ACTIVE.value
        session.commit()
        typer.echo(f"채널 '{channel}' 재개됨.")


@app.command("status")
def status(channel: str | None = typer.Option(None, "--channel", help="생략하면 전체 채널 표시")):
    engine = get_engine(_db_path())
    with Session(engine) as session:
        if channel:
            row = get_channel_by_name(session, channel)
            rows = [row] if row else []
        else:
            from sqlalchemy import select

            rows = list(session.scalars(select(Channel)))

        if not rows:
            typer.echo("등록된 채널이 없습니다.")
            return

        for row in rows:
            typer.echo(
                f"{row.name:30s} status={row.status:14s} strategy={row.source_strategy:24s} "
                f"last_run={row.last_run_at or '-'}"
            )


@app.command("dashboard")
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """Launch the local web dashboard at http://127.0.0.1:8000 — channel list, pause/resume,
    run-now, job/clip history, and a manual-upload form, all click-driven (no commands to type)."""
    from shorts_factory.dashboard.app import run_dashboard

    typer.echo(f"대시보드 실행 중: http://{host}:{port}  (끄려면 Ctrl+C)")
    run_dashboard(host=host, port=port)


if __name__ == "__main__":
    app()
