"""End-to-end tests against a real FFmpeg. Skipped automatically when FFmpeg is not installed."""

import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

import waveforge_video as wf
from conftest import Var
from waveforge_video import Job

pytestmark = pytest.mark.requires_ffmpeg

UNICODE_NAME = "clip ünï 日本語.mp4"


def make_sample(ffmpeg, out, *inputs_and_codecs):
    cmd = [ffmpeg, "-v", "error", "-y", *inputs_and_codecs, str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


@pytest.fixture(scope="module")
def samples(ffmpeg_tools):
    ffmpeg, _ = ffmpeg_tools
    root = Path(tempfile.mkdtemp())
    video = ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=4"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=4"]
    s = {"root": root}
    s["main"] = make_sample(ffmpeg, root / UNICODE_NAME, *video, *tone, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-metadata", "title=日本語 タイトル 😀 – “quotes”")
    s["odd"] = make_sample(ffmpeg, root / "odd.mp4", "-f", "lavfi", "-i", "testsrc=size=321x241:rate=25:duration=2",
                           "-c:v", "libx264", "-pix_fmt", "yuv444p")
    s["silent"] = make_sample(ffmpeg, root / "silent.mp4", *video, "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    s["tone"] = make_sample(ffmpeg, root / "tone.wav", *tone, "-t", "2")
    s["long"] = make_sample(ffmpeg, root / "long.mp4", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=40",
                            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")
    try:
        s["tenbit"] = make_sample(ffmpeg, root / "tenbit.mp4", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                                  "-c:v", "libx265", "-pix_fmt", "yuv420p10le")
    except subprocess.CalledProcessError:
        s["tenbit"] = None  # this FFmpeg build has no libx265
    yield s
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def tools(ffmpeg_tools):
    return ffmpeg_tools


def stream_info(ffprobe, path):
    """Streams of a file as dicts with codec_type, codec_name, width, height, pix_fmt."""
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt",
                          "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8").stdout
    return json.loads(out)["streams"]


def software_app(make_app, tools, source, **overrides):
    """An app stand-in with `source` loaded, software encoding and the fast quality preset (override as needed)."""
    ffmpeg, ffprobe = tools
    settings = {"hw": Var(False), "encoders": {"libx264", "libx265", "libvpx-vp9"}, "quality": Var("Fast")}
    settings.update(overrides)
    return make_app(ffmpeg=ffmpeg, media=wf.probe_media(ffprobe, str(source)), **settings)


def export(app, output, timeout=120):
    """Run the app's real export path (job building, worker, partial-file handling). Returns (ok, message)."""
    job = app._make_job(str(output))
    t = threading.Thread(target=lambda: app._worker(job), daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "export hung"
    events = []
    while not app.events.empty():
        events.append(app.events.get_nowait())
    done = [e for e in events if e[0] == "done"]
    return done[0][1], done[0][2], events


# ------------------------------------------------------------------ probing and waveform

def test_probe_handles_unicode_file_name_and_metadata(tools, samples):
    info = wf.probe_media(tools[1], str(samples["main"]))
    assert (info.width, info.height) == (640, 360)
    assert info.video_codec == "h264" and info.audio_codec == "aac"
    assert info.duration == pytest.approx(4.0, abs=0.2)
    assert info.fps == pytest.approx(25.0)


def test_probe_reports_audio_only_file(tools, samples):
    info = wf.probe_media(tools[1], str(samples["tone"]))
    assert info.video_codec == "" and info.audio_codec.startswith("pcm")


def test_probe_rejects_non_media_file(tools, samples):
    junk = samples["root"] / "junk.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        wf.probe_media(tools[1], str(junk))


def test_waveform_reflects_the_audio(tools, samples):
    peaks = wf.compute_waveform(tools[0], str(samples["main"]))
    assert 0 < len(peaks) <= 1000
    assert max(peaks) > 0.05  # a 440 Hz tone, not silence


def test_waveform_is_empty_without_audio(tools, samples):
    assert wf.compute_waveform(tools[0], str(samples["silent"])) == []


def test_encoder_listing_includes_software_encoders(tools):
    assert {"libx264", "libvpx-vp9"} <= wf.detect_encoders(tools[0])


# ---------------------------------------------------------------------------- presets

@pytest.mark.parametrize("preset,ext,video,audio", [
    ("MP4 H.264 (Best compatibility)", ".mp4", "h264", "aac"),
    ("MP4 H.265 (Smaller file)", ".mp4", "hevc", "aac"),
    ("WebM VP9", ".webm", "vp9", "opus"),
    ("MOV H.264", ".mov", "h264", "aac"),
    ("Audio only MP3", ".mp3", None, "mp3"),
    ("Audio only FLAC", ".flac", None, "flac"),
])
def test_every_preset_exports_a_playable_file(make_app, tools, samples, temp_dir, preset, ext, video, audio):
    app = software_app(make_app, tools, samples["main"], preset=Var(preset))
    out = temp_dir / f"result{ext}"
    ok, msg, _ = export(app, out)
    assert ok, msg
    kinds = {st["codec_type"]: st["codec_name"] for st in stream_info(tools[1], out)}
    assert kinds.get("video") == video
    assert kinds.get("audio") == audio
    assert not list(temp_dir.glob("*.part*"))


def test_trim_cuts_the_requested_range(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["main"], trim_start=Var("1"), trim_end=Var("3"))
    out = temp_dir / "trimmed.mp4"
    assert export(app, out)[0]
    assert wf.probe_media(tools[1], str(out)).duration == pytest.approx(2.0, abs=0.25)


def test_progress_reaches_the_end(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["main"])
    ok, _, events = export(app, temp_dir / "p.mp4")
    progress = [e[1] for e in events if e[0] == "progress"]
    assert ok and progress and max(progress) > 80


def test_rotate_swaps_dimensions(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["main"], rotate=Var("90° clockwise"))
    out = temp_dir / "rot.mp4"
    assert export(app, out)[0]
    info = wf.probe_media(tools[1], str(out))
    assert (info.width, info.height) == (360, 640)


def test_resolution_preset_gives_exact_frame_size(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["main"], resolution=Var("1080p"), enhance=Var("Lanczos + light sharpen"))
    out = temp_dir / "hd.mp4"
    assert export(app, out)[0]
    info = wf.probe_media(tools[1], str(out))
    assert (info.width, info.height) == (1920, 1080)


def test_odd_sized_source_exports_with_even_dimensions(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["odd"])
    out = temp_dir / "even.mp4"
    ok, msg, _ = export(app, out)
    assert ok, msg
    info = wf.probe_media(tools[1], str(out))
    assert (info.width, info.height) == (320, 240)


def test_ten_bit_source_becomes_standard_yuv420p(make_app, tools, samples, temp_dir):
    if samples["tenbit"] is None:
        pytest.skip("FFmpeg build has no libx265 to create the 10-bit sample")
    app = software_app(make_app, tools, samples["tenbit"])
    out = temp_dir / "eight.mp4"
    ok, msg, _ = export(app, out)
    assert ok, msg
    assert stream_info(tools[1], out)[0]["pix_fmt"] == "yuv420p"


def test_mute_removes_the_audio_track(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["main"], mute=Var(True))
    out = temp_dir / "mute.mp4"
    assert export(app, out)[0]
    assert [st["codec_type"] for st in stream_info(tools[1], out)] == ["video"]


def test_volume_zero_silences_the_audio(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["main"], volume=Var(0.0))
    out = temp_dir / "quiet.mp4"
    assert export(app, out)[0]
    assert max(wf.compute_waveform(tools[0], str(out))) < 0.01


def test_extract_audio_from_audio_only_input(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["tone"], preset=Var("Audio only MP3"))
    out = temp_dir / "tone.mp3"
    ok, msg, _ = export(app, out)
    assert ok, msg
    assert stream_info(tools[1], out)[0]["codec_name"] == "mp3"


def test_video_preset_on_audio_only_input(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["tone"], rotate=Var("90° clockwise"), resolution=Var("720p"))
    ok, msg, _ = export(app, temp_dir / "tone_as_mp4.mp4")
    assert ok, msg


def test_source_without_audio_exports_fine(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["silent"], volume=Var(0.5))
    ok, msg, _ = export(app, temp_dir / "novoice.mp4")
    assert ok, msg


# ------------------------------------------------------------ hardware encoders and safety

def test_unusable_hardware_encoder_falls_back_to_software(make_app, tools, samples, temp_dir):
    """The reported bug: FFmpeg lists GPU encoders that this PC cannot actually use."""
    listed = [e for e in wf.detect_encoders(tools[0]) if wf.is_hw_encoder(e) and e.startswith("h264")]
    broken = [e for e in listed if not wf.verify_encoder(tools[0], e)]
    if not broken:
        pytest.skip("every listed GPU encoder works on this machine, so there is nothing to fall back from")
    app = software_app(make_app, tools, samples["main"], hw=Var(True), encoders={broken[0], "libx264"})
    assert wf.is_hw_encoder(app.build_command("x.mp4")[app.build_command("x.mp4").index("-c:v") + 1])
    out = temp_dir / "fallback.mp4"
    ok, msg, events = export(app, out)
    assert ok, msg
    assert any(e[0] == "note" for e in events)
    assert stream_info(tools[1], out)[0]["codec_name"] == "h264"


def test_working_hardware_encoder_produces_a_file(make_app, tools, samples, temp_dir):
    working = [e for e in wf.detect_encoders(tools[0]) if wf.is_hw_encoder(e) and e.startswith("h264")
               and wf.verify_encoder(tools[0], e)]
    if not working:
        pytest.skip("no working GPU H.264 encoder on this machine")
    app = software_app(make_app, tools, samples["main"], hw=Var(True), encoders={working[0], "libx264"})
    out = temp_dir / "gpu.mp4"
    ok, msg, events = export(app, out)
    assert ok, msg
    assert not any(e[0] == "note" for e in events)  # no fallback needed
    assert stream_info(tools[1], out)[0]["codec_name"] == "h264"


def test_failed_export_keeps_the_existing_output(make_app, tools, samples, temp_dir):
    out = temp_dir / "keep.mp4"
    out.write_bytes(b"precious")
    app = software_app(make_app, tools, samples["main"])
    app.media.path = str(temp_dir / "does_not_exist.mp4")
    ok, _, _ = export(app, out)
    assert not ok
    assert out.read_bytes() == b"precious"
    assert not list(temp_dir.glob("*.part*"))


def test_successful_export_replaces_existing_output(make_app, tools, samples, temp_dir):
    out = temp_dir / "replace.mp4"
    out.write_bytes(b"old")
    app = software_app(make_app, tools, samples["main"])
    assert export(app, out)[0]
    assert wf.probe_media(tools[1], str(out)).width == 640


def test_cancel_stops_ffmpeg_and_removes_partial_file(make_app, tools, samples, temp_dir):
    app = software_app(make_app, tools, samples["long"], quality=Var("Maximum"))
    out = temp_dir / "cancelled.mp4"
    job = app._make_job(str(out))
    t = threading.Thread(target=lambda: app._worker(job), daemon=True)
    t.start()
    for _ in range(100):
        if job.proc is not None:
            break
        threading.Event().wait(0.1)
    threading.Event().wait(1.5)
    job.cancel()
    t.join(30)
    assert not t.is_alive()
    done = []
    while not app.events.empty():
        e = app.events.get_nowait()
        if e[0] == "done":
            done.append(e)
    assert done and done[0][1] is False and done[0][2] == "Cancelled"
    assert not out.exists() and not Path(job.work_path).exists()
