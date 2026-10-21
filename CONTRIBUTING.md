# Contributing

Bug reports and pull requests are welcome.

## Setup

```bash
python -m pip install -r requirements-dev.txt
python waveforge_video.py        # run the app
python -m pytest                 # run the tests
```

The app is a single file, [waveforge_video.py](waveforge_video.py), and uses only the standard library, so please do not add runtime dependencies.

## Tests

- `tests/test_core.py` and `tests/test_utility_functions.py` need neither FFmpeg nor a display.
- `tests/test_ffmpeg_integration.py` runs real exports and is skipped when FFmpeg is not installed. Install it with `winget install Gyan.FFmpeg` to run it locally.
- Tests must not touch your real settings; `conftest.py` redirects them to a temporary folder automatically.

When you fix a bug, add a test that fails without the fix.

## Pull requests

Keep changes focused, run `python -m pytest` before opening the PR, and add a line to [CHANGELOG.md](CHANGELOG.md) for anything users will notice.

## Releases

Bump `__version__` in `waveforge_video.py`, update `CHANGELOG.md`, then push a tag such as `v1.0.1`. The release workflow runs the tests, builds the Windows zip and publishes the GitHub release; it refuses to run if the tag does not match `__version__`.
