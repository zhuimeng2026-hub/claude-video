# Keyframe Image Edit POC

## Goal

This POC tests the lightweight route discussed in the product-replacement workflow and turns it into a repeatable script:

```text
source video
  -> extract a small set of keyframes
  -> edit those keyframe images
  -> synthesize short clips from the edited images
  -> concatenate the clips into a short demo montage
  -> replace matching spans in the source video timeline
  -> export a full-length video with the edited content embedded
```

It intentionally does not use a video generation model, SAM2, Cutie, XMem, ProPainter, or E2FGVI. The purpose is to validate whether a product-demo workflow can start from image-level edits before adding heavier video consistency models.

## Script

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\build_keyframe_image_poc.py
```

Run from the repository root:

```powershell
python product_video_byd_shark_watch\replacement_poc\build_keyframe_image_poc.py
```

## Input

The script uses the previously downloaded BYD product video:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\download\video.mp4
```

The input video is ignored by Git.

## Output

Generated artifacts are written under:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\keyframe_image_poc
```

Important files:

```text
source_frames\*.jpg
edited_keyframes\*.jpg
keyframe_image_poc_sheet.jpg
byd_shark_keyframe_image_poc.mp4
byd_shark_keyframe_image_full_timeline.mp4
full_timeline_filter_complex.txt
```

The artifact directory is ignored by Git.

The generated video from the current run is:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\keyframe_image_poc\byd_shark_keyframe_image_poc.mp4
```

The generated full-timeline video from the current run is:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\keyframe_image_poc\byd_shark_keyframe_image_full_timeline.mp4
```

Current output properties:

```text
Short montage:
  1280x720
  30 fps
  12 seconds
  H.264 video + AAC silent audio
  about 2.4 MB

Full timeline:
  1280x720
  30 fps
  93.83 seconds
  H.264 video + AAC audio from the source timeline
  about 23 MB

Source video:
  93.87 seconds
```

## What It Demonstrates

The POC extracts five representative product frames:

```text
00:00:07.2 street front
00:00:18.8 side / interior feature
00:00:27.0 front hero
00:00:39.2 rear badge
00:01:11.4 night product shot
```

Each frame is edited as an image:

- A rough product identity patch is drawn over the visible vehicle badge or product region.
- A product-demo callout panel is added.
- The replacement brand is represented as `AURORA X`.

The edited images are then converted into short motion clips with ffmpeg `zoompan`, then concatenated into a 12-second video.

The same short motion clips are also inserted back into the original video timeline. Each edited clip replaces a 2.4-second source segment starting at the matching timestamp:

```text
00:00:07.2 -> replace 00:00:07.2 to 00:00:09.6
00:00:18.8 -> replace 00:00:18.8 to 00:00:21.2
00:00:27.0 -> replace 00:00:27.0 to 00:00:29.4
00:00:39.2 -> replace 00:00:39.2 to 00:00:41.6
00:01:11.4 -> replace 00:01:11.4 to 00:01:13.8
```

The generated ffmpeg concat graph is saved at:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\keyframe_image_poc\full_timeline_filter_complex.txt
```

## Repeatable Workflow

This script is the reusable workflow for the current lightweight route.

To run again:

```powershell
python product_video_byd_shark_watch\replacement_poc\build_keyframe_image_poc.py
```

To adapt it to another product video:

1. Put or download the new source video.
2. Update `VIDEO` in `build_keyframe_image_poc.py`.
3. Update the `SHOTS` list with new timestamps, labels, badge rectangles, and panel rectangles.
4. Run the script again.

The workflow regenerates:

```text
source frames
edited keyframes
comparison sheet
short edited montage
full-length edited timeline
ffmpeg filter graph
```

## What It Does Not Solve Yet

This POC preserves the full source-video timeline length, but it does not preserve full original motion inside the replaced spans. Those spans become short image-driven clips from edited still frames. That makes it useful for proving a low-cost product-demo direction, but it is not yet a full product replacement pipeline.

Missing pieces for production realism:

- Automatic product detection.
- SAM2 / Cutie / XMem mask tracking across all frames.
- ProPainter / E2FGVI video inpainting to remove the original product.
- Real replacement product assets, such as multi-angle photos, transparent renders, UI screenshots, or a 3D model.
- Motion-consistent product compositing across the original video.

## Practical Meaning

This route can be used when the desired deliverable is a short product-demonstration montage:

```text
extract keyframes
  -> edit keyframe images
  -> create short moving clips
  -> assemble final demo video
```

If the desired deliverable is “the original video with the original product replaced in-place,” the next step is to add:

```text
SAM2 / Cutie / XMem
  -> mask tracking
  -> ProPainter / E2FGVI
  -> repaired original video
  -> product compositing
```
