# Watch Analysis Run: YouTube Rickroll

Date: 2026-07-11

Source URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ

## Purpose

Verify the current `watch` skill flow against a real YouTube URL. According to
the planned feature behavior, this flow should download the video, extract
frames, pull captions when available, and produce enough evidence for analysis.

## Commands Run

Preflight:

```powershell
python skills\watch\scripts\setup.py --json
```

Result:

```json
{
  "status": "needs_key",
  "can_proceed": false,
  "first_run": true,
  "setup_complete": false,
  "missing_binaries": [],
  "whisper_backend": null,
  "has_api_key": false,
  "config_file": "C:\\Users\\Admin\\.config\\watch\\.env",
  "watch_detail": "balanced",
  "platform": "Windows"
}
```

The required binaries were available. Whisper API configuration was missing,
but that is optional when native captions exist.

Attempted setup scaffold:

```powershell
python skills\watch\scripts\setup.py
```

This failed because `C:\Users\Admin\.config\watch` could not be created or
accessed:

```text
FileExistsError: [WinError 183] Cannot create a file when that file already exists:
'C:\\Users\\Admin\\.config\\watch'
```

Main watch run:

```powershell
python skills\watch\scripts\watch.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --detail balanced --no-whisper
```

The first sandboxed attempt failed because network access to YouTube was
blocked:

```text
Failed to establish a new connection: [WinError 10013]
```

After rerunning with approved network access, the flow completed successfully.

## Output

Working directory:

```text
C:\Users\Admin\AppData\Local\Temp\watch-w0ee4dq0
```

Downloaded video:

```text
C:\Users\Admin\AppData\Local\Temp\watch-w0ee4dq0\download\video.mp4
```

Extracted frames directory:

```text
C:\Users\Admin\AppData\Local\Temp\watch-w0ee4dq0\frames
```

Summary from the generated report:

```text
Title: Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)
Uploader: Rick Astley
Duration: 03:33 (213.1s)
Resolution: 1280x720 (av1)
Detail: balanced
Frames: 100 selected from 108 candidates
Transcript: 43 segments via captions
```

## Visual Review

The extracted frames cover the main music-video scenes:

- Rick Astley singing into a microphone in an interior set.
- Blue arched hallway / stage shots.
- Outdoor wall and fence shots with sunglasses.
- Bar interior shots with a dancing bartender / waiter.
- Dance shots with female performers and silhouette-style framing.

The captions align with the song lyrics and chorus progression. No Whisper
fallback was needed because YouTube captions were available.

## Conclusion

The planned feature path worked for this URL:

1. `yt-dlp` accessed YouTube metadata and captions.
2. The video and audio streams downloaded.
3. The streams merged into `video.mp4`.
4. `ffmpeg` extracted 100 scene-aware frames.
5. The script produced a usable report containing title, duration, frame paths,
   and transcript.

The remaining issue is not the main download/analyze path. It is the first-run
setup scaffold on this Windows machine: the hard-coded config path
`C:\Users\Admin\.config\watch\.env` could not be created because
`C:\Users\Admin\.config\watch` is currently in an abnormal inaccessible or
conflicting state.
