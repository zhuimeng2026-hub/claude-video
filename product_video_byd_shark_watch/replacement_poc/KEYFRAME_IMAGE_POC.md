# Keyframe Image Edit POC

## Goal

This POC tests the lightweight route discussed in the product-replacement workflow:

```text
source video
  -> extract a small set of keyframes
  -> edit those keyframe images
  -> synthesize short clips from the edited images
  -> concatenate the clips into a new demo video
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
```

The artifact directory is ignored by Git.

The generated video from the current run is:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\keyframe_image_poc\byd_shark_keyframe_image_poc.mp4
```

Current output properties:

```text
1280x720
30 fps
12 seconds
H.264 video + AAC silent audio
about 2.4 MB
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

## What It Does Not Solve Yet

This POC does not preserve the full original video motion. It creates new short clips from edited still frames. That makes it useful for proving a low-cost product-demo direction, but it is not yet a full product replacement pipeline.

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
