"""Tests for the app logic that needs neither FFmpeg nor a display.

FFmpeg is played by the Python interpreter where a process has to run; the real
thing is covered in test_ffmpeg_integration.py.
"""

import array
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import waveforge_video as wf
from conftest import Var
from waveforge_video import App, Job


def drain(q):
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def run_with_timeout(fn, seconds=30):
    result = {}
    t = threading.Thread(target=lambda: result.update(value=fn()), daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "worker hung"
    return result.get("value")


def vcodec(cmd):
    return cmd[cmd.index("-c:v") + 1]


# ------------------------------------------------------------------ finding FFmpeg

@pytest.fixture
def no_ffmpeg_anywhere(temp_dir, monkeypatch):
    monkeypatch.setattr(wf, "APP_DIR", temp_dir / "app")
    monkeypatch.setattr(wf.shutil, "which", lambda name: None)
    monkeypatch.setattr(wf, "extra_search_dirs", lambda: [])


def test_find_executable_returns_empty_when_nothing_found(no_ffmpeg_anywhere):
    assert wf.find_executable("ffmpeg.exe", "") == ""


def test_find_executable_ignores_a_folder_without_the_exe(temp_dir, monkeypatch, no_ffmpeg_anywhere):
    monkeypatch.chdir(temp_dir)
    assert wf.find_executable("ffmpeg.exe", ".") == ""
    assert wf.find_executable("ffmpeg.exe", "/nonexistent/path") == ""


def test_find_executable_accepts_file_or_folder(temp_dir, no_ffmpeg_anywhere):
    exe = temp_dir / "ffmpeg.exe"
    exe.write_bytes(b"")
    assert wf.find_executable("ffmpeg.exe", str(exe)) == str(exe)
    assert wf.find_executable("ffmpeg.exe", str(temp_dir)) == str(exe)


def test_find_executable_checks_bin_folder_beside_the_app(temp_dir, no_ffmpeg_anywhere):
    (temp_dir / "app" / "bin").mkdir(parents=True)
    exe = temp_dir / "app" / "bin" / "ffprobe.exe"
    exe.write_bytes(b"")
    assert wf.find_executable("ffprobe.exe") == str(exe)


def test_find_executable_uses_path(temp_dir, monkeypatch, no_ffmpeg_anywhere):
    exe = temp_dir / "ffmpeg.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(wf.shutil, "which", lambda name: str(exe) if name == "ffmpeg" else None)
    assert wf.find_executable("ffmpeg") == str(exe)


def test_find_executable_finds_winget_install(temp_dir, monkeypatch):
    bin_dir = temp_dir / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_abc" / "ffmpeg-9.0-full_build" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(temp_dir))
    monkeypatch.setattr(wf, "APP_DIR", temp_dir / "app")
    monkeypatch.setattr(wf.shutil, "which", lambda name: None)
    assert bin_dir in wf.extra_search_dirs()
    assert wf.find_executable("ffmpeg.exe") == str(bin_dir / "ffmpeg.exe")


# ------------------------------------------------------------------------ encoders

ENCODER_LISTING = """
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 V....D libvpx-vp9           libvpx VP9 (codec vp9)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D h264_qsv             H.264 / AVC / MPEG-4 AVC (Intel Quick Sync Video acceleration) (codec h264)
"""


def test_detect_encoders_parses_listing(monkeypatch):
    monkeypatch.setattr(wf, "run_capture", lambda cmd, timeout=30: ENCODER_LISTING)
    assert wf.detect_encoders("ffmpeg") == {"libx264", "libx265", "libvpx-vp9", "h264_nvenc", "h264_qsv"}


def test_detect_encoders_empty_when_ffmpeg_fails(monkeypatch):
    def boom(cmd, timeout=30):
        raise RuntimeError("no such file")
    monkeypatch.setattr(wf, "run_capture", boom)
    assert wf.detect_encoders("ffmpeg") == set()


@pytest.mark.parametrize("encoders,codec,prefer_hw,expected", [
    ({"libx264", "h264_nvenc", "h264_qsv"}, "h264", True, "h264_nvenc"),
    ({"libx264", "h264_qsv", "h264_amf"}, "h264", True, "h264_qsv"),
    ({"libx264", "h264_amf"}, "h264", True, "h264_amf"),
    ({"libx264", "h264_nvenc"}, "h264", False, "libx264"),
    ({"libx264"}, "h264", True, "libx264"),
    ({"libx265", "hevc_nvenc"}, "h265", True, "hevc_nvenc"),
    ({"libx265", "hevc_nvenc"}, "h265", False, "libx265"),
    (set(), "h265", True, "libx265"),
    ({"h264_nvenc"}, "vp9", True, "libvpx-vp9"),
    (set(), "unknown", True, "libx264"),
])
def test_choose_encoder(encoders, codec, prefer_hw, expected):
    assert wf.choose_encoder(encoders, codec, prefer_hw) == expected


def test_is_hw_encoder():
    for name in ("h264_nvenc", "hevc_qsv", "h264_amf"):
        assert wf.is_hw_encoder(name)
    for name in ("libx264", "libx265", "libvpx-vp9", ""):
        assert not wf.is_hw_encoder(name)


def test_verify_encoder_false_when_test_encode_fails(monkeypatch):
    def fake_run_capture(cmd, timeout=30):
        if "h264_nvenc" in cmd:
            raise RuntimeError("Cannot load nvcuda.dll")
        return ""
    monkeypatch.setattr(wf, "run_capture", fake_run_capture)
    assert wf.verify_encoder("ffmpeg", "h264_nvenc") is False
    assert wf.verify_encoder("ffmpeg", "h264_amf") is True


def test_verify_encoder_false_on_timeout(monkeypatch):
    def hang(cmd, timeout=30):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(wf, "run_capture", hang)
    assert wf.verify_encoder("ffmpeg", "hevc_qsv") is False


# ----------------------------------------------------------------- building commands

@pytest.mark.parametrize("preset,hw_enc,sw_enc", [
    ("MP4 H.264 (Best compatibility)", "h264_nvenc", "libx264"),
    ("MOV H.264", "h264_nvenc", "libx264"),
    ("MP4 H.265 (Smaller file)", "hevc_nvenc", "libx265"),
])
def test_force_software_swaps_hardware_encoder(make_app, preset, hw_enc, sw_enc):
    app = make_app(preset=Var(preset))
    assert vcodec(app.build_command("out.mp4")) == hw_enc
    assert vcodec(app.build_command("out.mp4", force_software=True)) == sw_enc


def test_hardware_checkbox_is_respected(make_app):
    app = make_app(hw=Var(False))
    assert vcodec(app.build_command("out.mp4")) == "libx264"


def test_job_has_software_fallback_only_for_hardware_encoders(make_app):
    assert make_app()._make_job("out.mp4").fallback_cmd is not None
    assert make_app(hw=Var(False))._make_job("out.mp4").fallback_cmd is None
    assert make_app(preset=Var("WebM VP9"))._make_job("out.webm").fallback_cmd is None


def test_job_writes_to_a_partial_file_first(make_app):
    job = make_app()._make_job("C:/videos/out.mp4")
    assert job.output == "C:/videos/out.mp4"
    assert job.work_path.endswith("out.part.mp4")
    assert job.cmd[-1] == job.work_path
    assert job.fallback_cmd[-1] == job.work_path


def test_video_presets_use_widely_playable_pixel_format(make_app):
    for preset in ("MP4 H.264 (Best compatibility)", "MP4 H.265 (Smaller file)", "WebM VP9", "MOV H.264"):
        cmd = make_app(preset=Var(preset), hw=Var(False)).build_command("out.mp4")
        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p", preset


def test_keep_source_resolution_forces_even_dimensions(make_app):
    cmd = make_app().build_command("out.mp4")
    assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in cmd[cmd.index("-vf") + 1]


def test_resolution_preset_scales_and_pads(make_app):
    cmd = make_app(resolution=Var("1080p")).build_command("out.mp4")
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf and "pad=1920:1080" in vf


@pytest.mark.parametrize("rotation,filter_text", [
    ("90° clockwise", "transpose=1"),
    ("90° counterclockwise", "transpose=2"),
    ("180°", "hflip,vflip"),
])
def test_rotation_filters(make_app, rotation, filter_text):
    cmd = make_app(rotate=Var(rotation)).build_command("out.mp4")
    assert cmd[cmd.index("-vf") + 1].startswith(filter_text)


def test_mute_drops_audio(make_app):
    cmd = make_app(mute=Var(True)).build_command("out.mp4")
    assert "-an" in cmd and "-c:a" not in cmd


def test_volume_filter_only_when_changed(make_app):
    assert "-af" not in make_app().build_command("out.mp4")
    cmd = make_app(volume=Var(0.5)).build_command("out.mp4")
    assert cmd[cmd.index("-af") + 1] == "volume=0.500"


def test_audio_only_ignores_video_filters_and_honours_volume(make_app):
    app = make_app(preset=Var("Audio only MP3"), rotate=Var("180°"), resolution=Var("1080p"), volume=Var(1.5))
    cmd = app.build_command("out.mp3")
    assert "-vf" not in cmd and "-vn" in cmd
    assert cmd[cmd.index("-af") + 1] == "volume=1.500"


def test_audio_only_with_mute_is_rejected(make_app):
    app = make_app(preset=Var("Audio only MP3"), mute=Var(True))
    with pytest.raises(ValueError, match="silent"):
        app.build_command("out.mp3")


def test_audio_only_without_audio_track_is_rejected(make_app):
    media = wf.MediaInfo(path="silent.mp4", duration=5.0, video_codec="h264")
    with pytest.raises(ValueError, match="no audio"):
        make_app(preset=Var("Audio only FLAC"), media=media).build_command("out.flac")


def test_audio_file_input_gets_no_video_filters(make_app):
    media = wf.MediaInfo(path="song.wav", duration=5.0, audio_codec="pcm_s16le")
    cmd = make_app(media=media, rotate=Var("180°")).build_command("out.mp4")
    assert "-vf" not in cmd


def test_trim_range_validates_and_clamps(make_app):
    with pytest.raises(ValueError):
        make_app(trim_start=Var("5"), trim_end=Var("3"))._trim_range()
    assert make_app(trim_start=Var("2"), trim_end=Var("999"))._trim_range() == (2.0, 10.0)
    assert make_app(trim_start=Var(""), trim_end=Var(""))._trim_range() == (0.0, 10.0)


def test_trim_is_passed_to_ffmpeg(make_app):
    cmd = make_app(trim_start=Var("1.5"), trim_end=Var("4")).build_command("out.mp4")
    assert cmd[cmd.index("-ss") + 1] == "1.500"
    assert cmd[cmd.index("-t") + 1] == "2.500"


# ----------------------------------------------------------- waveform and file helpers

def pcm(values):
    return array.array("h", values).tobytes()


def test_peaks_from_pcm_levels():
    peaks = wf.peaks_from_pcm(pcm([0] * 100 + [32767] * 100 + [-32768] * 100), 3)
    assert peaks[0] == 0.0
    assert peaks[1] == pytest.approx(32767 / 32768)
    assert peaks[2] == 1.0


def test_peaks_from_pcm_handles_edge_cases():
    assert wf.peaks_from_pcm(b"", 10) == []
    assert wf.peaks_from_pcm(b"\x01", 10) == []                      # half a sample
    assert len(wf.peaks_from_pcm(pcm([5, 6, 7]), 1000)) == 3        # never more buckets than samples
    assert len(wf.peaks_from_pcm(pcm(range(5000)) + b"\x00", 100)) == 100  # trailing odd byte ignored


def test_compute_waveform_returns_empty_when_ffmpeg_fails(monkeypatch):
    monkeypatch.setattr(wf.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"err"))
    assert wf.compute_waveform("ffmpeg", "x.mp4") == []

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(wf.subprocess, "run", missing)
    assert wf.compute_waveform("ffmpeg", "x.mp4") == []


def test_same_file(temp_dir):
    a = temp_dir / "a.mp4"
    a.write_bytes(b"x")
    assert wf.same_file(str(a), str(a))
    assert wf.same_file(str(a), str(temp_dir / "." / "a.mp4"))
    assert not wf.same_file(str(a), str(temp_dir / "b.mp4"))


def test_partial_path_keeps_the_extension():
    assert Path(wf.partial_path("C:/v/out.mp4")) == Path("C:/v/out.part.mp4")
    assert wf.partial_path("out.webm") == "out.part.webm"
    assert wf.partial_path("clip.v2.mov") == "clip.v2.part.mov"


# ----------------------------------------------------------------- frame previews

@pytest.mark.parametrize("t,duration,expected", [
    (3.0, 10.0, 3.0),
    (-1.0, 10.0, 0.0),
    (10.0, 10.0, 9.95),   # the very end of a file has no frame, so stay just inside it
    (99.0, 10.0, 9.95),
    (5.0, 0.0, 5.0),      # unknown duration: leave the time alone
    (0.0, 0.01, 0.0),
])
def test_preview_time(t, duration, expected):
    assert wf.preview_time(t, duration) == pytest.approx(expected)


def fake_run(captured, stdout=b"\x89PNG\r\n\x1a\nimage data", returncode=0):
    def run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout, b"")
    return run


def test_grab_frame_builds_a_single_frame_png_command(monkeypatch):
    captured = []
    monkeypatch.setattr(wf.subprocess, "run", fake_run(captured))
    png = wf.grab_frame("ffmpeg", "in.mp4", 12.3456, "90° clockwise", (300, 169))
    cmd = captured[0][0]
    assert png.startswith(b"\x89PNG")
    assert cmd[cmd.index("-ss") + 1] == "12.346"
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[cmd.index("-i") + 1] == "in.mp4"
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "transpose=1,scale=300:169:force_original_aspect_ratio=decrease"
    assert cmd[-1] == "-" and "image2pipe" in cmd and "-an" in cmd


def test_grab_frame_without_rotation_only_scales(monkeypatch):
    captured = []
    monkeypatch.setattr(wf.subprocess, "run", fake_run(captured))
    wf.grab_frame("ffmpeg", "in.mp4", 0, "None", (160, 90))
    cmd = captured[0][0]
    assert cmd[cmd.index("-vf") + 1] == "scale=160:90:force_original_aspect_ratio=decrease"
    assert cmd[cmd.index("-ss") + 1] == "0.000"


def test_grab_frame_returns_empty_when_nothing_usable(monkeypatch):
    monkeypatch.setattr(wf.subprocess, "run", fake_run([], stdout=b"\x89PNG..", returncode=1))
    assert wf.grab_frame("ffmpeg", "in.mp4", 1) == b""                      # ffmpeg failed
    monkeypatch.setattr(wf.subprocess, "run", fake_run([], stdout=b""))
    assert wf.grab_frame("ffmpeg", "in.mp4", 1) == b""                      # no frame decoded (seek past the end)
    monkeypatch.setattr(wf.subprocess, "run", fake_run([], stdout=b"not a png"))
    assert wf.grab_frame("ffmpeg", "in.mp4", 1) == b""                      # not an image

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(wf.subprocess, "run", hang)
    assert wf.grab_frame("ffmpeg", "in.mp4", 1) == b""

    def missing(cmd, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr(wf.subprocess, "run", missing)
    assert wf.grab_frame("ffmpeg", "in.mp4", 1) == b""


class Recorder:
    """Collects FrameLoader deliveries so a test can wait for them."""

    def __init__(self):
        self.items = []
        self.cond = threading.Condition()

    def __call__(self, slot, ticket, key, data):
        with self.cond:
            self.items.append((slot, ticket, key, data))
            self.cond.notify_all()

    def wait_for(self, count, timeout=10):
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.items) >= count, timeout), f"only {len(self.items)} of {count} deliveries"
        return list(self.items)


def test_frame_loader_delivers_the_requested_frame():
    got = Recorder()
    loader = wf.FrameLoader(lambda *key: b"png:" + repr(key).encode(), got)
    ticket = loader.request("start", ("a.mp4", 1.5))
    (slot, n, key, data), = got.wait_for(1)
    assert (slot, n, key) == ("start", ticket, ("a.mp4", 1.5))
    assert data == b"png:" + repr(("a.mp4", 1.5)).encode()
    assert loader.current("start", ticket)


def test_frame_loader_keeps_only_the_newest_request_per_slot():
    release, started = threading.Event(), threading.Event()
    grabbed = []

    def grab(name, t):
        grabbed.append((name, t))
        started.set()
        release.wait(10)
        return b"x"

    got = Recorder()
    loader = wf.FrameLoader(grab, got)
    loader.request("hover", ("a", 1))
    assert started.wait(10)                    # the first request is now running and blocked
    stale = loader.request("hover", ("a", 2))  # superseded before it ever starts
    newest = loader.request("hover", ("a", 3))
    release.set()
    deliveries = got.wait_for(2)
    assert grabbed == [("a", 1), ("a", 3)]     # the middle request was never fetched
    assert not loader.current("hover", stale) and loader.current("hover", newest)
    assert [d[2] for d in deliveries] == [("a", 1), ("a", 3)]
    assert not loader.current("hover", deliveries[0][1])  # the running one was superseded too, so the UI drops it


def test_frame_loader_slots_do_not_cancel_each_other():
    got = Recorder()
    loader = wf.FrameLoader(lambda *key: b"x", got)
    a = loader.request("start", ("a", 1))
    b = loader.request("end", ("a", 2))
    got.wait_for(2)
    assert loader.current("start", a) and loader.current("end", b)


def test_frame_loader_cancel_makes_results_stale():
    release, started = threading.Event(), threading.Event()

    def grab(*key):
        started.set()
        release.wait(10)
        return b"x"

    got = Recorder()
    loader = wf.FrameLoader(grab, got)
    ticket = loader.request("hover", ("a", 1))
    assert started.wait(10)
    loader.cancel("hover")
    release.set()
    got.wait_for(1)
    assert not loader.current("hover", ticket)


def test_frame_loader_serves_repeats_from_cache():
    calls = []
    got = Recorder()
    loader = wf.FrameLoader(lambda *key: calls.append(key) or b"x", got)
    loader.request("start", ("a", 1))
    got.wait_for(1)
    loader.request("end", ("a", 1))            # same frame, different slot
    got.wait_for(2)
    assert calls == [("a", 1)]
    loader.clear_cache()
    loader.request("start", ("a", 1))
    got.wait_for(3)
    assert calls == [("a", 1), ("a", 1)]


def test_frame_loader_cache_is_bounded_and_forgets_the_oldest():
    calls = []
    got = Recorder()
    loader = wf.FrameLoader(lambda *key: calls.append(key) or b"x", got, cache_size=2)
    for i in range(3):
        loader.request("hover", ("a", i))
        got.wait_for(i + 1)
    loader.request("hover", ("a", 2))          # still cached
    loader.request("hover", ("a", 0))          # evicted, so fetched again
    got.wait_for(5)
    assert calls == [("a", 0), ("a", 1), ("a", 2), ("a", 0)]


def test_frame_loader_does_not_cache_failures_and_survives_errors():
    attempts = []

    def grab(*key):
        attempts.append(key)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return b"" if len(attempts) == 2 else b"good"

    got = Recorder()
    loader = wf.FrameLoader(grab, got)
    for i in range(3):
        loader.request("start", ("a", 1))
        got.wait_for(i + 1)
    assert [d[3] for d in got.items] == [b"", b"", b"good"]   # an exception and an empty result both deliver b""
    assert len(attempts) == 3                                  # and neither was cached


# --------------------------------------------------------------------- export worker

def py(code):
    return [sys.executable, "-c", code]


def test_run_ffmpeg_survives_stderr_flood(make_app):
    # ~3 MB on stderr is far more than an OS pipe holds. If nothing reads stderr while
    # the process runs, the child blocks forever.
    code = ("import sys\n"
            "for _ in range(3000): sys.stderr.write('e' * 1000 + chr(10))\n"
            "sys.stderr.flush()\n"
            "print('out_time_ms=500000', flush=True)\n")
    app = make_app()
    job = Job(py(code), "out.mp4", "flood", total=1.0)
    rc, err = run_with_timeout(lambda: app._run_ffmpeg(job, job.cmd))
    assert rc == 0
    assert err.count("\n") <= 2000  # bounded tail, not the whole 3 MB
    assert [e[1] for e in drain(app.events) if e[0] == "progress"] == [50.0]


def test_worker_retries_with_software_encoder(make_app, temp_dir):
    out = temp_dir / "out.mp4"
    job = Job(py("import sys; sys.stderr.write('No NVENC capable devices'); sys.exit(1)"), str(out), "retry",
              total=1.0, fallback_cmd=py(f"open(r'{out}', 'wb').write(b'x')"))
    app = make_app()
    run_with_timeout(lambda: app._worker(job))
    events = drain(app.events)
    assert any(e[0] == "note" for e in events)
    done = [e for e in events if e[0] == "done"]
    assert len(done) == 1 and done[0][1] is True
    assert out.exists()
    log = next((temp_dir / "logs").glob("job_*.log")).read_text(encoding="utf-8")
    assert "No NVENC capable devices" in log  # both attempts are logged


def test_worker_reports_failure_without_fallback(make_app, temp_dir):
    job = Job(py("import sys; sys.stderr.write('boom'); sys.exit(1)"), str(temp_dir / "out.mp4"), "fail", total=1.0)
    app = make_app()
    run_with_timeout(lambda: app._worker(job))
    done = [e for e in drain(app.events) if e[0] == "done"]
    assert len(done) == 1 and done[0][1] is False and "boom" in done[0][2]


def test_worker_does_not_retry_after_cancel(make_app, temp_dir):
    marker = temp_dir / "fallback_ran"
    job = Job(py("import sys; sys.exit(1)"), str(temp_dir / "out.mp4"), "cancel", total=1.0,
              fallback_cmd=py(f"open(r'{marker}', 'w').write('x')"))
    job.cancelled = True
    app = make_app()
    run_with_timeout(lambda: app._worker(job))
    done = [e for e in drain(app.events) if e[0] == "done"]
    assert done[0][1] is False and done[0][2] == "Cancelled"
    assert not marker.exists()


def test_worker_moves_finished_partial_file_into_place(make_app, temp_dir):
    out, part = temp_dir / "out.mp4", temp_dir / "out.part.mp4"
    out.write_bytes(b"old")
    job = Job(py(f"open(r'{part}', 'wb').write(b'new video')"), str(out), "ok", total=1.0, work_path=str(part))
    app = make_app()
    run_with_timeout(lambda: app._worker(job))
    assert [e[1] for e in drain(app.events) if e[0] == "done"] == [True]
    assert out.read_bytes() == b"new video"
    assert not part.exists()


def test_failed_export_leaves_existing_file_and_no_partial(make_app, temp_dir):
    out, part = temp_dir / "out.mp4", temp_dir / "out.part.mp4"
    out.write_bytes(b"precious")
    job = Job(py(f"open(r'{part}', 'wb').write(b'half'); import sys; sys.exit(1)"), str(out), "bad", total=1.0, work_path=str(part))
    app = make_app()
    run_with_timeout(lambda: app._worker(job))
    assert [e[1] for e in drain(app.events) if e[0] == "done"] == [False]
    assert out.read_bytes() == b"precious"
    assert not part.exists()


def test_cancelled_export_discards_partial_file(make_app, temp_dir):
    out, part = temp_dir / "out.mp4", temp_dir / "out.part.mp4"
    code = f"open(r'{part}', 'wb').write(b'half')\nimport time; time.sleep(60)"
    job = Job(py(code), str(out), "cancel", total=1.0, work_path=str(part))
    app = make_app()
    t = threading.Thread(target=lambda: app._worker(job), daemon=True)
    t.start()
    for _ in range(100):  # wait until the process has started writing
        if part.exists():
            break
        threading.Event().wait(0.1)
    job.cancel()
    t.join(30)
    assert not t.is_alive()
    done = [e for e in drain(app.events) if e[0] == "done"]
    assert done[0][1] is False and done[0][2] == "Cancelled"
    assert not part.exists() and not out.exists()


def test_cancel_also_stops_child_processes(make_app):
    # FFmpeg installed through a Chocolatey/Scoop shim is a launcher with the real encoder as a
    # child. Stopping only the launcher leaves the child holding the pipes, so the export never ends.
    child = "import time; print('started', flush=True); time.sleep(60)"
    launcher = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    app = make_app()
    job = Job(py(launcher), "out.mp4", "shim", total=1.0)
    t = threading.Thread(target=lambda: app._run_ffmpeg(job, job.cmd), daemon=True)
    t.start()
    threading.Event().wait(2)  # let the child start
    job.cancel()
    t.join(20)
    assert not t.is_alive(), "cancel left the child process running"


def test_old_logs_are_pruned(temp_dir):
    log_dir = temp_dir / "logs"
    log_dir.mkdir()
    for i in range(60):
        (log_dir / f"job_{i:04d}.log").write_text("x")
    App._write_log(log_dir / "job_9999.log", [(["ffmpeg"], 0, "")], keep=50)
    remaining = sorted(p.name for p in log_dir.glob("job_*.log"))
    assert len(remaining) == 50 and remaining[-1] == "job_9999.log"
