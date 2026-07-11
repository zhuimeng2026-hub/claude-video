# Youku Watch Test

Source:

```text
https://v.youku.com/v_show/id_XMTA5MDQyODk2.html?playMode=pugv&frommaciku=1
```

Processing directory:

```text
C:\Users\Admin\claude-video\youku_watch_test
```

## Result

The current app can download and process this public Youku video through `yt-dlp`.

Generated files:

```text
C:\Users\Admin\claude-video\youku_watch_test\download\video.mp4
C:\Users\Admin\claude-video\youku_watch_test\download\video.info.json
C:\Users\Admin\claude-video\youku_watch_test\frames
C:\Users\Admin\claude-video\youku_watch_test\contact_sheet.jpg
C:\Users\Admin\claude-video\youku_watch_test\audio_16k.wav
C:\Users\Admin\claude-video\youku_watch_test\transcript_local_whisper_cpp.txt
C:\Users\Admin\claude-video\youku_watch_test\transcript_local_whisper_cpp.srt
```

Video metadata:

```text
codec: H.264 video + AAC audio
resolution: 320x240
frame rate: 15 fps
duration: 60.866667 seconds
size: 1,734,680 bytes
```

Frame extraction:

```text
41 scene-aware frames
```

## Notes

The first sandboxed attempt failed because network access to Youku-related domains was blocked. Running with network permission succeeded.

The video has no detected subtitles. Local `whisper.cpp` was run in Chinese mode and produced transcript files, but the result appears repetitive and unreliable, likely because the source is low-resolution/low-bitrate with music or noisy audio.

The visual content appears to be a low-resolution car/motorcycle chase or road sequence with a Youku watermark. It is useful to verify Youku support, but it is not an ideal source for product-replacement demos because of the 320x240 resolution and heavy compression.
