# Product Replacement POC Assets

Generated transparent replacement overlays and an ffmpeg filter graph.

## Overlays

- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_01_intro.png`: 0.0s to 0.2s
- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_02_street_front.png`: 6.7s to 9.6s
- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_03_side_close.png`: 18.0s to 20.5s
- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_04_hero_front.png`: 26.5s to 28.2s
- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_05_rear_badge.png`: 38.4s to 41.5s
- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_06_night_front.png`: 70.0s to 74.5s
- `C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays\overlay_07_end_card.png`: 90.8s to 93.9s

## Filter Graph

`C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\filter_complex.txt`

This is a lightweight stand-in for detection/segmentation + inpainting + tracking:

- Detection/segmentation: hand-authored product/logo boxes on selected shots.
- Inpainting: masked boxes use local-looking dark or body-color fills.
- Tracking: time-windowed overlays follow stable product shots.
- Composition: ffmpeg overlays render a new video while preserving the source audio.
