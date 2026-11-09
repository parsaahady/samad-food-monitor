"""تست‌های زمان‌بند تطبیقی تهران و دستورات خط فرمان (status / doctor)."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from samad_monitor.main import main
from samad_monitor.monitor import SamadFoodMonitor
from samad_monitor.notifications.manager import NotificationManager
from samad_monitor.scheduler import AdaptiveScheduler
from tests.test_monitor_scenarios import RecordingChannel, _make_test_config

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def test_scheduler_normal_vs_peak_intervals(tmp_path: Path) -> None:
    config = _make_test_config(tmp_path)
    config.check_jitter_seconds = 0
    monitor = SamadFoodMonitor(
        config=config,
        notification_manager=NotificationManager([RecordingChannel()]),
    )
    sched = AdaptiveScheduler(config=config, monitor=monitor, rng=random.Random(42))

    # دوشنبه (روز عادی) -> دقیقاً 60 دقیقه (3600 ثانیه)
    monday = datetime(2026, 11, 9, 10, 0, tzinfo=TEHRAN_TZ)
    assert sched.compute_delay_seconds(monday) == 3600.0

    # پنجشنبه و جمعه (روزهای اوج انتشار منو) -> بین 10 تا 15 دقیقه (600 تا 900 ثانیه)
    thursday = datetime(2026, 11, 12, 10, 0, tzinfo=TEHRAN_TZ)
    friday = datetime(2026, 11, 13, 18, 0, tzinfo=TEHRAN_TZ)
    thu_delay = sched.compute_delay_seconds(thursday)
    fri_delay = sched.compute_delay_seconds(friday)
    assert 600.0 <= thu_delay <= 900.0
    assert 600.0 <= fri_delay <= 900.0


def test_cli_status_and_doctor_commands(tmp_path: Path, capsys: object) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "\n".join(
            [
                f"DATA_DIR={tmp_path / 'data'}",
                f"LOGS_DIR={tmp_path / 'logs'}",
                f"SQLITE_DB_PATH={tmp_path / 'data' / 'cli.db'}",
                f"STORAGE_STATE_PATH={tmp_path / 'data' / 'state.json'}",
                f"BROWSER_PROFILE_DIR={tmp_path / 'data' / 'profile'}",
                "NOTIFICATION_CHANNELS=telegram",
                "TELEGRAM_BOT_TOKEN=111111:TEST_CLI_BOT_TOKEN",
                "TELEGRAM_CHAT_ID=123456",
            ]
        ),
        encoding="utf-8",
    )

    rc_status = main(["--env-file", str(env_file), "status"])
    assert rc_status == 0
    out_status = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "وضعیت سامانه پایش غذای سماد" in out_status
    assert "دانشگاه شهید بهشتی" in out_status
    assert "وضعیت انتشار هفته آینده" in out_status

    rc_doc = main(["--env-file", str(env_file), "doctor"])
    assert rc_doc == 0
    out_doc = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Playwright Chromium" in out_doc
    assert "SQLite" in out_doc
