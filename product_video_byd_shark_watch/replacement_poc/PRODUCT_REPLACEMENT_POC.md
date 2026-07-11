# BYD SHARK Product Replacement POC

## Output

Final demo video:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\byd_shark_product_replacement_poc_v3.mp4
```

Source video:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\download\video.mp4
```

Check sheet:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\check_v3_sheet.jpg
```

## What Was Changed

The proof of concept changes visible product/brand surfaces into a fictional product brand, `AURORA X`.

Replacement windows:

- 00:06.7-00:09.6: street front vehicle shot.
- 00:18.0-00:20.5: cockpit/product UI area.
- 00:26.5-00:28.2: front hero grille shot.
- 00:38.4-00:41.5: rear tailgate badge.
- 01:10.0-01:14.5: night moving front shot.
- 01:30.8-01:33.9: end card.

## Pipeline Mapping

This local POC stands in for the heavier product-replacement architecture:

- Target detection / segmentation: hand-authored product/logo boxes on known time windows.
- Tracking: fixed time-window overlays for stable product shots.
- Inpainting / repair: local-looking opaque or semi-opaque replacement patches cover the original visible branding.
- Compositing: ffmpeg overlays render a new H.264/AAC video while preserving source audio.

## Generated Assets

Overlay generator:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\build_product_replacement_poc.py
```

Transparent overlay images:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\overlays
```

ffmpeg filter graph:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\filter_complex.txt
```

## Verification

Final video metadata:

```text
codec: H.264 video + AAC audio
resolution: 1280x720
frame rate: 30000/1001
duration: 93.8938 seconds
size: 25,595,881 bytes
```

## Limitations

This is a runnable local proof of concept, not a production-grade video inpainting result.

The current masks are manually authored rectangles, so they do not deform with perspective or occlusions. Some original branding may remain visible outside the masked region. A production pipeline should replace the hand-authored masks with SAM2-style segmentation, add XMem-style mask propagation/tracking, and use ProPainter/E2FGVI-style video inpainting before compositing the new product surface.

## Real Model vs Real Product

In this context, "real model" means the actual AI vision/video models used for product replacement, not necessarily a physically filmed replacement product.

The model side includes:

- Target detection and segmentation: locate the car, logo, screen, package, device, or other product region.
- Mask propagation and tracking: keep the target region stable across motion, occlusion, perspective changes, and shot cuts.
- Video inpainting: remove the original product branding or surface content and reconstruct plausible background/material.
- Product compositing: place the new product visual back into the repaired region with matching scale, perspective, blur, grain, and lighting.

The product source can be one of three kinds:

- Real filmed product footage or photos. This gives the strongest realism when the angle, lens, lighting, and motion match the source video.
- Product renderings, UI screenshots, logo files, packaging artwork, or 3D model renders. This is usually the most practical route for commercial product demos.
- AI-generated product images. This can work for concept demos, but consistency across shots is weaker unless the output is controlled carefully.

For realistic new video output, the replacement product assets matter as much as the AI model. A good production-grade input package should include:

- Clean logo files with transparent background.
- Product images or renders from front, side, rear, and close-up angles.
- UI screenshots for any phone, dashboard, HUD, or screen replacement.
- Optional 3D model or turntable render if the product must follow camera motion.
- Color/material references so the compositor can match reflections, shadows, and surface texture.

For the BYD sample, the most realistic near-term targets are not full-car replacement. Better targets are logo/badge replacement, screen/UI replacement, end-card replacement, and stable close-up product-surface replacement. Full vehicle-body replacement would need stronger segmentation, object tracking, 3D-aware alignment, and likely manual compositing review.
