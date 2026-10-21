# User guide

## Before you start

WaveForge Video needs FFmpeg. If the ENGINE panel says it was not found, run `winget install Gyan.FFmpeg` and restart the app, or point to `ffmpeg.exe` and `ffprobe.exe` under **FFmpeg Settings**.

## The window

| Area | What it does |
|------|--------------|
| **SOURCE** (top left) | Name, size, frame rate, codecs, container and duration of the loaded file. |
| **EDIT** (left) | Trim start/end, mute, volume and rotation. |
| **FRAMES AT THE TRIM POINTS** (middle, top) | The video frame at the start and at the end of the part you are keeping. |
| **TIMELINE** (middle) | Audio waveform with a time ruler. The bright box is the part that will be kept. Hover to preview any moment. |
| **EXPORT / CONVERT** (right) | Preset, resolution, 4K enhancement, quality and the hardware-encoder switch. |
| **QUICK CONVERT** | One-click shortcuts: MP4 1080p, upscale to 4K, extract MP3. |
| **ENGINE** | Which FFmpeg is in use and which GPU encoders passed their startup test. |
| **RECENT JOBS** | Exports from this session and whether they finished. |

## Trimming

- Type seconds into **Trim start** and **Trim end** (decimals are fine, for example `12.5`). Leave **Trim end** blank to keep everything to the end.
- Or use the timeline: **click** sets the start, **right-click** sets the end.
- The time under the timeline shows `start – end / total`.

### Seeing what you are cutting

- The **START** and **END** panels show the picture at the two trim points. They update a moment after you change a trim value or the rotation (so they follow *Rotate* too). END shows the last frame that will be kept.
- **Move the mouse over the timeline** to see a small thumbnail of that moment with its time. Click to make that moment the start, or right-click to make it the end.
- The panels grow and shrink with the window. Frames you have already looked at are remembered, so moving back and forth is instant.
- These are still pictures, not playback. For a real look at the clip use **Play in default player**.
- Audio files and videos without a picture show "No video track" instead.

Trimming re-encodes the video, so the cut is frame-accurate but takes as long as a normal export.

## Editing options

- **Mute audio** removes the audio track. **Volume** goes from 0 (silent) to 2 (double); 1 leaves it unchanged.
- **Rotate** turns the picture 90° clockwise, 90° counterclockwise or 180°.

## Exporting

1. Pick a **Preset**:
   - *MP4 H.264 (Best compatibility)* plays almost everywhere.
   - *MP4 H.265 (Smaller file)* is smaller for the same quality but some older devices cannot play it.
   - *WebM VP9* is for the web.
   - *MOV H.264* is for editors and Apple software that prefer MOV.
   - *Audio only MP3 / FLAC* extracts the sound (the source needs an audio track and *Mute audio* must be off).
2. Pick a **Resolution**. A size different from the source is scaled to fit and padded with black bars so the picture is never stretched. *Keep source* leaves the size alone (odd sizes are rounded down to even numbers, which the encoders require).
3. **4K enhancement** works together with the resolution: *Lanczos upscale* is plain high-quality scaling, *light sharpen* adds a mild sharpen, and *Denoise + Lanczos + sharpen* cleans noisy footage first. It makes low-resolution video look crisper but cannot add real detail. With *Keep source* selected, any option other than *Off* just applies the light sharpen.
4. **Quality**: Fast, Balanced, High or Maximum. Higher means a bigger file and a slower export.
5. Click **Choose Output + Start** and pick where to save.

Video presets always produce standard 8-bit 4:2:0 video, so the result plays in ordinary players even if the source was 10-bit.

### Hardware encoding

With *Use hardware encoder when available* on, exports use your GPU (NVIDIA NVENC, Intel Quick Sync or AMD AMF) when one passed the startup test shown in the ENGINE panel; otherwise the software encoder is used. If a GPU export fails anyway, it is retried automatically in software. Software encoding is slower but usually gives slightly better quality for the file size.

### Cancelling and closing

**Cancel Job** stops the export and deletes the unfinished file. If you close the window during an export it asks whether to cancel it. Your original file and any existing file at the output location are only replaced by a successful export.

## Where things are stored

`%APPDATA%\WaveForgeVideo\config.json` holds your settings and `%APPDATA%\WaveForgeVideo\logs` holds one log per export (the 50 newest are kept). Each log has the exact FFmpeg command and its messages, which is useful when reporting a problem.
