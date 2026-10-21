# Troubleshooting

## "FFmpeg/ffprobe not found"

Install FFmpeg with `winget install Gyan.FFmpeg`, then restart the app. Or open **FFmpeg Settings** and browse to `ffmpeg.exe` and `ffprobe.exe` (you can also enter the folder that contains them). The app also checks next to itself, in a `bin` folder beside it, your `PATH`, the WinGet install folder and `C:\ffmpeg\bin`.

To see what the app is using, look at the ENGINE panel.

## "Could not read file"

ffprobe could not understand the file: it may be damaged, still downloading, or an unusual format. Try opening it in a player first. The message shown is ffprobe's own error text.

## The export failed

The error box shows the end of FFmpeg's messages. The full log, including the exact command, is in `%APPDATA%\WaveForgeVideo\logs` (newest file last). Common causes:

- **Audio-only export: "no audio track" or "would be silent".** The source has no sound, or *Mute audio* is ticked.
- **"Could not save ..."** The output file is open in another program (a player, for instance) or the folder is read-only. Close the other program or choose another location.
- **"The output would overwrite the source video."** Choose a different file name.
- **Out of disk space.** 4K and *Maximum* quality files get large.

## "Hardware encoder failed - retrying with software encoder"

This is informational. The GPU encoder could not run (for example outdated graphics drivers), so the app switched to software encoding and carried on. To stop seeing it, untick *Use hardware encoder when available* or update your graphics driver.

## ENGINE says "Hardware encoders: none available"

Your FFmpeg build or GPU does not provide a working hardware encoder. Exports use software encoding, which works fine, only slower.

## No waveform on the timeline

"No audio track" means the file has no sound. "Waveform unavailable" means FFmpeg could not decode the audio; exporting video may still work.

## The exported file is large, small, or looks soft

Use *Quality* to trade size against detail. Upscaling cannot create detail that was not in the source.

## Reporting a bug

Open an issue with: what you did, the exact error text, your Windows and FFmpeg versions (`ffmpeg -version`), and the log file from the failed export. Remove anything private from the log first; it contains the file paths you used.
