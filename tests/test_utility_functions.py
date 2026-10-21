"""Unit tests for the small helpers in waveforge_video.py: frame rates, time formatting, settings."""

import json

import pytest

import waveforge_video as wf


@pytest.mark.parametrize("text,expected", [
    ("30", 30.0),
    ("29.97", 29.97),
    ("25/1", 25.0),
    ("30000/1001", 30000 / 1001),
    ("23.976/1", 23.976),
    ("0", 0.0),
    ("0/0", 0.0),       # ffprobe reports this for streams without a frame rate
    ("1/0", 0.0),
    ("", 0.0),
    ("invalid", 0.0),
    ("a/b", 0.0),
])
def test_parse_fraction(text, expected):
    assert wf.parse_fraction(text) == pytest.approx(expected)


def test_parse_fraction_rejects_non_strings():
    assert wf.parse_fraction(None) == 0.0


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00"),
    (5.9, "00:05"),
    (65, "01:05"),
    (3599, "59:59"),
    (3600, "1:00:00"),
    (3725, "1:02:05"),
    (-3, "00:00"),
])
def test_fmt_time(seconds, expected):
    assert wf.fmt_time(seconds) == expected


def test_load_config_defaults_when_missing():
    cfg = wf.load_config()
    assert cfg["ffmpeg_path"] == "" and cfg["ffprobe_path"] == ""
    assert cfg["prefer_hardware"] is True
    assert cfg["last_output_dir"]


def test_load_config_reads_saved_values():
    wf.CONFIG.parent.mkdir(parents=True)
    wf.CONFIG.write_text(json.dumps({"ffmpeg_path": "C:\\tools\\ffmpeg.exe", "prefer_hardware": False}), encoding="utf-8")
    cfg = wf.load_config()
    assert cfg["ffmpeg_path"] == "C:\\tools\\ffmpeg.exe"
    assert cfg["prefer_hardware"] is False
    assert cfg["ffprobe_path"] == ""  # keys missing from the file keep their defaults


def test_load_config_survives_corrupt_file():
    wf.CONFIG.parent.mkdir(parents=True)
    wf.CONFIG.write_text("{ not json", encoding="utf-8")
    assert wf.load_config()["prefer_hardware"] is True


def test_save_config_round_trip_and_indent():
    wf.save_config({"ffmpeg_path": "x", "prefer_hardware": True})
    assert wf.CONFIG.exists()
    assert "\n  " in wf.CONFIG.read_text(encoding="utf-8")
    assert wf.load_config()["ffmpeg_path"] == "x"


def test_save_config_ignores_unwritable_location(temp_dir, monkeypatch):
    blocker = temp_dir / "not_a_folder"
    blocker.write_text("x")
    monkeypatch.setattr(wf, "CONFIG", blocker / "config.json")
    wf.save_config({"a": 1})  # must not raise


def test_settings_live_outside_the_app_folder():
    # The app folder may be read-only (Program Files) and must stay free of runtime files.
    assert wf.DATA_DIR != wf.APP_DIR
