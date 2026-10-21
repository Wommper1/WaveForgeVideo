# Changelog

## [1.0.0] - 2026-10-20

First public release.

### Added
- Open video and audio files with FFmpeg; shows resolution, frame rate, codecs, container and duration.
- Timeline with a real audio waveform, a time ruler and a highlighted trim range. Click to set the start, right-click to set the end.
- Trim, rotate (90° either way, 180°), mute and volume.
- Export presets: MP4 H.264, MP4 H.265, MOV H.264, WebM VP9, audio-only MP3 and FLAC; 720p to 4K resizing with optional Lanczos upscale, sharpen and denoise; four quality levels.
- Hardware encoding (NVENC, Quick Sync, AMF). Each encoder is test-encoded at startup so only ones that work are offered, and a failed hardware export is retried with the software encoder.
- Cancel stops FFmpeg completely, including when it is installed through a Chocolatey or Scoop shim.
- Exports are written to a `.part` file and moved into place on success; cancelling or failing never leaves a broken file, and an existing file is only replaced by a finished export.
- FFmpeg discovery: configured path, next to the app, `PATH`, WinGet install folder, `C:\ffmpeg\bin`; file pickers in FFmpeg Settings.
- Settings and export logs kept in `%APPDATA%\WaveForgeVideo` (override with `WAVEFORGE_HOME`); the 50 newest logs are kept.
- Windows build script (`build_exe.bat`) and GitHub Actions workflows for tests and releases.
