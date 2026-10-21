"""Shared pytest configuration and fixtures for WaveForge Video."""

import os
import queue
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

# Make waveforge_video importable when pytest runs from anywhere
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", default=False, help="Run slow tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: long-running test, needs --run-slow")
    config.addinivalue_line("markers", "requires_ffmpeg: needs ffmpeg and ffprobe (skipped when they are not installed)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="need --run-slow option to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture
def temp_dir():
    """A throw-away directory (plain mkdtemp, so it does not depend on pytest's own temp root)."""
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_settings(temp_dir, monkeypatch):
    """Keep tests from reading or writing the real settings and log folder."""
    import waveforge_video
    monkeypatch.setattr(waveforge_video, "CONFIG", temp_dir / "settings" / "config.json")
    monkeypatch.setattr(waveforge_video, "LOG_DIR", temp_dir / "logs")


@pytest.fixture(scope="session")
def ffmpeg_tools():
    """(ffmpeg, ffprobe) paths, or skip the test when FFmpeg is not installed."""
    import waveforge_video as wf
    ffmpeg = wf.find_executable("ffmpeg.exe") or wf.find_executable("ffmpeg")
    ffprobe = wf.find_executable("ffprobe.exe") or wf.find_executable("ffprobe")
    if not (ffmpeg and ffprobe):
        pytest.skip("FFmpeg is not installed")
    return ffmpeg, ffprobe


class Var:
    """Minimal stand-in for a tkinter variable."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@pytest.fixture
def make_app():
    """Factory for a display-less stand-in for App that can build commands and run export jobs."""
    import waveforge_video as wf

    def factory(**overrides):
        app = types.SimpleNamespace(
            ffmpeg="ffmpeg",
            encoders={"h264_nvenc", "hevc_nvenc", "libx264", "libx265", "libvpx-vp9"},
            media=wf.MediaInfo(path="in.mp4", duration=10.0, width=640, height=360,
                               video_codec="h264", audio_codec="aac"),
            trim_start=Var("0"), trim_end=Var(""),
            preset=Var("MP4 H.264 (Best compatibility)"),
            resolution=Var("Keep source"), enhance=Var("Off"), quality=Var("High"),
            rotate=Var("None"), mute=Var(False), volume=Var(1.0), hw=Var(True),
            events=queue.Queue(),
        )
        app.__dict__.update(overrides)
        for name in ("_trim_range", "build_command", "_make_job", "_run_ffmpeg", "_worker"):
            setattr(app, name, types.MethodType(getattr(wf.App, name), app))
        app._write_log = wf.App._write_log
        return app

    return factory
