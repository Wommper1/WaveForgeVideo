"""Drives the real window (kept invisible) with a real FFmpeg.

Skipped when FFmpeg is missing or no display is available.
"""

import shutil
import subprocess
import tempfile
import time
import tkinter as tk
from pathlib import Path

import pytest

import waveforge_video as wf

pytestmark = pytest.mark.requires_ffmpeg


@pytest.fixture(scope="module")
def clips(ffmpeg_tools):
    ffmpeg, _ = ffmpeg_tools
    root = Path(tempfile.mkdtemp())
    blocks = []
    for colour in ("red", "lime", "blue"):  # one second of each: the frame colour tells which second it came from
        blocks += ["-f", "lavfi", "-i", f"color=c={colour}:s=640x360:r=25:d=1"]
    subprocess.run([ffmpeg, "-v", "error", "-y", *blocks, "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "rgb.mp4")], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    str(root / "tone.wav")], check=True)
    yield {"video": root / "rgb.mp4", "audio": root / "tone.wav"}
    shutil.rmtree(root, ignore_errors=True)


def pump(window, until=None, timeout=15.0):
    """Run the Tk event loop until `until()` is true (or for `timeout` seconds if it is None)."""
    end = time.time() + timeout
    while time.time() < end:
        window.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until is None


@pytest.fixture
def app(ffmpeg_tools, monkeypatch):
    try:
        window = wf.App()
    except tk.TclError as e:
        pytest.skip(f"no display available: {e}")
    try:
        window.attributes("-alpha", 0.0)  # fully transparent, so nothing flashes on screen while testing
    except tk.TclError:
        pass
    window.geometry("1220x780+0+0")
    pump(window, timeout=0.6)
    yield window
    window.destroy()


def open_clip(window, monkeypatch, path):
    monkeypatch.setattr(wf.filedialog, "askopenfilename", lambda **kw: str(path))
    window.open_file()


def colour(img):
    rgb = img.get(img.width() // 2, img.height() // 2)
    return tuple(int(v) for v in (rgb.split() if isinstance(rgb, str) else rgb))


def dominant(img):
    return max(range(3), key=colour(img).__getitem__)  # 0 red, 1 green, 2 blue


def item_types(canvas, tag="all"):
    return [canvas.type(i) for i in canvas.find_withtag(tag)]


def test_panels_start_with_a_prompt(app):
    assert app._frame_content["start"] == ("text", "Open a video")
    assert app.frame_cap["start"].cget("text") == "START"
    assert app.frame_cap["end"].cget("text") == "END"


def test_opening_a_video_shows_the_first_and_last_frames(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    assert "image" in item_types(app.frame_cv["start"]) and "image" in item_types(app.frame_cv["end"])
    assert dominant(app._frame_content["start"][1]) == 0    # first second is red
    assert dominant(app._frame_content["end"][1]) == 2      # last frame is blue, not past the end
    assert app.frame_cap["start"].cget("text") == "START  0.00s"
    assert app.frame_cap["end"].cget("text") == "END  3.00s"


def test_changing_the_trim_points_updates_the_frames(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    app.trim_start.set("1.2")
    app.trim_end.set("2.5")
    assert pump(app, lambda: app.shown.get("start", ("", 0))[1] == 1.2 and app.shown.get("end", ("", 0))[1] == 2.46)
    assert dominant(app._frame_content["start"][1]) == 1    # 1.2 s is green
    assert dominant(app._frame_content["end"][1]) == 2      # just before 2.5 s is blue
    assert app.frame_cap["start"].cget("text") == "START  1.20s"
    assert app.frame_cap["end"].cget("text") == "END  2.50s"


def test_half_typed_trim_values_do_not_break_the_previews(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    app.trim_start.set("1.")
    app.trim_end.set("abc")
    pump(app, timeout=0.8)
    assert pump(app, lambda: app.shown["start"][1] == 1.0)
    assert "image" in item_types(app.frame_cv["start"])


def test_rotation_turns_the_preview_frames(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    img = app._frame_content["start"][1]
    assert img.width() > img.height()
    app.rotate.set("90° clockwise")
    assert pump(app, lambda: app.shown["start"][2] == "90° clockwise" and app.shown["end"][2] == "90° clockwise")
    img = app._frame_content["start"][1]
    assert img.width() < img.height()


def test_hovering_the_timeline_shows_the_frame_under_the_cursor(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    w, h = app.canvas.winfo_width(), app.canvas.winfo_height()
    app.canvas.event_generate("<Motion>", x=w // 2, y=h // 2)
    assert pump(app, lambda: "hover" in app.shown)
    assert app.shown["hover"][1] == 1.5                     # halfway through the 3 s clip
    assert pump(app, lambda: app._hover_img is not None)
    assert dominant(app._hover_img) == 1                    # 1.5 s is green
    assert {"image", "line", "text"} <= set(item_types(app.canvas, "hover"))
    app.canvas.event_generate("<Leave>")
    pump(app, timeout=0.3)
    assert not app.canvas.find_withtag("hover")
    assert "hover" not in app.shown and app._hover_img is None


def test_hover_at_the_far_right_asks_for_a_frame_that_exists(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    app.canvas.event_generate("<Motion>", x=app.canvas.winfo_width() - 1, y=100)
    assert pump(app, lambda: "hover" in app.shown)
    assert app.shown["hover"][1] <= 2.95
    assert pump(app, lambda: app._hover_img is not None)
    assert dominant(app._hover_img) == 2


def test_audio_only_file_shows_a_message_and_never_asks_for_frames(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["audio"])
    pump(app, timeout=0.5)
    requests = []
    monkeypatch.setattr(app.frames, "request", lambda *a: requests.append(a))
    app.canvas.event_generate("<Motion>", x=100, y=100)
    app.trim_start.set("0.5")
    pump(app, timeout=0.8)
    assert app._frame_content["start"] == ("text", "No video track")
    assert app._frame_content["end"] == ("text", "No video track")
    assert requests == [] and not app.shown
    assert not app.canvas.find_withtag("hover")


def test_opening_another_file_replaces_the_frames(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    open_clip(app, monkeypatch, clips["audio"])
    assert pump(app, lambda: app._frame_content["start"] == ("text", "No video track"))
    assert not app.shown
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    assert "image" in item_types(app.frame_cv["start"])


def test_frames_are_fetched_at_a_size_that_fits_the_panels(app, monkeypatch, clips):
    open_clip(app, monkeypatch, clips["video"])
    assert pump(app, lambda: {"start", "end"} <= set(app.shown))
    normal = app.preview_box
    app.geometry("1600x900")
    assert pump(app, lambda: app.preview_box[0] > normal[0])
    assert app.preview_box == (400, 225)                    # capped
    assert pump(app, lambda: app.shown["start"][3:] == (400, 225))
    app.geometry("1050x700")
    assert pump(app, lambda: app.preview_box[0] < normal[0])
    width, height = app.preview_box
    assert abs(width / height - 16 / 9) < 0.05
    assert app.frame_cv["start"].winfo_width() >= width     # the panel really is wide enough for what was requested
