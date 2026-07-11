# Product Replacement Realism Notes

This note records what is required to make a product-replacement video look real.

Related POC:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\byd_shark_product_replacement_poc_v3.mp4
```

Detailed POC doc:

```text
C:\Users\Admin\claude-video\product_video_byd_shark_watch\replacement_poc\PRODUCT_REPLACEMENT_POC.md
```

## Key Clarification

"Real model" means real AI vision/video models, not necessarily a real filmed product.

The model side should eventually include:

- Detection / segmentation: find the product, logo, screen, package, vehicle part, or device surface.
- Tracking / mask propagation: keep the mask aligned through movement, occlusion, and perspective change.
- Video inpainting: remove the original product/branding and repair the background or surface.
- Compositing: place the new product asset back with matching perspective, blur, grain, light, and shadow.

## Product Source Options

The replacement product can come from:

- Real filmed product photos or video clips.
- Product renderings, UI screenshots, logo files, packaging artwork, or 3D renders.
- AI-generated product images for early concept demos.

For realism, real filmed or rendered assets are usually better than pure AI-generated images.

## Minimum Asset Package For Realistic Demo

- Transparent logo files.
- Product images/renders from multiple angles.
- UI screenshots for phone, dashboard, HUD, or display replacement.
- Color/material references.
- Optional 3D model or turntable render if the product must move with the camera.

## Best Targets In The BYD Sample

Good near-term targets:

- Vehicle badge/logo replacement.
- Phone app UI replacement.
- Dashboard/HUD/screen UI replacement.
- End-card replacement.
- Stable close-up surface replacement.

Hard targets:

- Full vehicle body replacement.
- Moving product replacement under occlusion.
- Long shots with changing perspective and reflections.

Those hard targets need SAM2-style segmentation, XMem-style tracking, ProPainter/E2FGVI-style video inpainting, and manual compositing review.
