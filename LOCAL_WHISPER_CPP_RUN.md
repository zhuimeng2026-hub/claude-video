# Local whisper.cpp Run

## Result

Local Whisper transcription has been verified with `whisper.cpp` on this machine.

Test video:

```text
https://www.youtube.com/watch?v=mseiD47Zc9w
```

Downloaded video:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\download\video.mp4
```

## Installed Files

Tool directory:

```text
C:\Users\Admin\claude-video\local_tools\whisper_cpp
```

Executable:

```text
C:\Users\Admin\claude-video\local_tools\whisper_cpp\bin\Release\whisper-cli.exe
```

Model:

```text
C:\Users\Admin\claude-video\local_tools\whisper_cpp\models\ggml-base.bin
```

Model size: about 148 MB.

Combined local tool + current product-video processing files measured about 195 MB.

## Commands Used

Download prebuilt Windows x64 package:

```powershell
curl.exe -L -o local_tools\whisper_cpp\whisper-bin-x64.zip https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.1/whisper-bin-x64.zip
Expand-Archive -Path local_tools\whisper_cpp\whisper-bin-x64.zip -DestinationPath local_tools\whisper_cpp\bin -Force
```

Download base model:

```powershell
curl.exe -L -o local_tools\whisper_cpp\models\ggml-base.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
```

Extract 16 kHz mono WAV:

```powershell
ffmpeg -y -i product_video_byd_shark_watch\download\video.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le product_video_byd_shark_watch\audio_16k.wav
```

Run local transcription:

```powershell
.\local_tools\whisper_cpp\bin\Release\whisper-cli.exe -m local_tools\whisper_cpp\models\ggml-base.bin -f product_video_byd_shark_watch\audio_16k.wav -otxt -osrt -of product_video_byd_shark_watch\transcript_local_whisper_cpp
```

## Output

Transcript files:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\transcript_local_whisper_cpp.txt
C:\Users\Admin\claude-video\product_video_byd_shark_watch\transcript_local_whisper_cpp.srt
```

This particular BYD video has almost no speech. The local transcript only detected music and blank audio:

```text
00:00:00.000 --> 00:00:02.580  (upbeat music)
00:00:30.000 --> 00:00:32.580  (upbeat music)
00:01:00.000 --> 00:01:02.580  (upbeat music)
00:01:30.000 --> 00:01:40.000  [BLANK_AUDIO]
```

Runtime reported by `whisper-cli`: about 20 seconds for a 93.9 second video on CPU.

## Next Integration Step

The current `/watch` app is not yet wired to call `whisper.cpp` automatically. The next code step is to add a local backend in `skills/watch/scripts/whisper.py`, then let `watch.py` use it when no Groq/OpenAI key is configured.
