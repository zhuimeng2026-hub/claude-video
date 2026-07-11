# Original Video Local Transcript

Source video:

```text
C:\Users\Admin\claude-video\watch_chinese_face_video\original_downloaded_video.mp4
```

Local Whisper backend:

```text
C:\Users\Admin\claude-video\local_tools\whisper_cpp\bin\Release\whisper-cli.exe
```

Model:

```text
C:\Users\Admin\claude-video\local_tools\whisper_cpp\models\ggml-base.bin
```

Generated audio:

```text
C:\Users\Admin\claude-video\watch_chinese_face_video\original_audio_16k.wav
```

Generated transcript files:

```text
C:\Users\Admin\claude-video\watch_chinese_face_video\original_transcript_local_whisper_cpp.txt
C:\Users\Admin\claude-video\watch_chinese_face_video\original_transcript_local_whisper_cpp.srt
```

## Result

This video contains continuous vocal music. Local `whisper.cpp` successfully produced timestamped transcript output.

Measured source duration: about 3 minutes 33 seconds.

Local CPU transcription time reported by `whisper-cli`: about 72 seconds.

The transcript is useful for validating that local speech transcription works without a Groq/OpenAI Whisper API key. Since the content is song lyrics, this note intentionally does not duplicate the full text.

## Commands

```powershell
ffmpeg -y -i watch_chinese_face_video\original_downloaded_video.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le watch_chinese_face_video\original_audio_16k.wav
```

```powershell
.\local_tools\whisper_cpp\bin\Release\whisper-cli.exe -m local_tools\whisper_cpp\models\ggml-base.bin -f watch_chinese_face_video\original_audio_16k.wav -otxt -osrt -of watch_chinese_face_video\original_transcript_local_whisper_cpp
```
