# Product Replacement Model Sources

## Core Pipeline

For product replacement, the realistic open-source pipeline is:

```text
detection / segmentation -> mask tracking / propagation -> video inpainting -> product compositing
```

## Main Model Sources

### SAM 2 / SAM 2.1

Use for promptable image/video segmentation.

Source:

```text
https://github.com/facebookresearch/sam2
```

Checkpoints:

```text
https://github.com/facebookresearch/sam2/tree/main/checkpoints
https://huggingface.co/facebook/sam2-hiera-large
```

Notes:

- Official Meta repository.
- Apache-2.0 license in the repository.
- Requires Python 3.10+, PyTorch 2.5.1+, TorchVision 0.20.1+.
- Windows users are strongly recommended by the project to use WSL Ubuntu.

### XMem

Use for long-video object mask propagation/tracking.

Source:

```text
https://github.com/hkchengrex/XMem
```

Notes:

- MIT license.
- Older but still useful for video object segmentation and mask propagation.
- Newer alternatives from the same author include Cutie and DEVA.

### Cutie

Use as a newer XMem-style video object segmentation alternative.

Source:

```text
https://github.com/hkchengrex/Cutie
```

Notes:

- MIT license.
- Tested on Ubuntu.
- Python 3.8+, PyTorch 1.12+.
- Has an interactive video segmentation GUI and scripting demos.

### DEVA

Use for tracking anything / automatic video segmentation with external detectors.

Source:

```text
https://github.com/hkchengrex/Tracking-Anything-with-DEVA
```

Notes:

- Tested on Ubuntu.
- Python 3.9+, PyTorch 1.12+.
- Can combine with Grounded Segment Anything for text-prompted object discovery.

### ProPainter

Use for video inpainting / object removal after masks are generated.

Source:

```text
https://github.com/sczhou/ProPainter
```

Weights:

```text
https://github.com/sczhou/ProPainter/releases
```

Notes:

- Strong video inpainting choice.
- Non-commercial use only under NTU S-Lab License 1.0.
- CUDA >= 9.2, PyTorch >= 1.7.1.
- Pretrained models can be downloaded manually or automatically during first inference.

### E2FGVI / E2FGVI-HQ

Use as another video inpainting option.

Source:

```text
https://github.com/MCG-NKU/E2FGVI
```

Weights:

```text
Google Drive / Baidu links from the official README
```

Notes:

- CVPR 2022 video inpainting model.
- E2FGVI-HQ supports arbitrary resolution better than the original E2FGVI.
- Python >= 3.7, PyTorch >= 1.5, CUDA >= 9.2, mmcv-full.

## Open-Source Alternatives

### Segmentation / Tracking

- SAM 2 / SAM 2.1: best first choice for promptable video masks.
- Cutie: newer video object segmentation alternative to XMem, MIT licensed.
- XMem: mature mask propagation baseline, MIT licensed.
- DEVA: good when automatic or text-prompted tracking is needed.

### Inpainting / Object Removal

- ProPainter: strong video inpainting, but non-commercial license.
- E2FGVI-HQ: practical video inpainting baseline.
- Inpaint Anything: integrates SAM with image/video inpainting tools; Apache-2.0 repository.
- LaMa: strong image inpainting; useful frame-by-frame or for static/short stabilized patches, but not a full video-consistency solution by itself.

## Recommended Practical Route

For the BYD product-video demo, do not start with full vehicle replacement.

Start with:

```text
SAM2 or Cutie -> mask logo / screen / badge
ProPainter or E2FGVI-HQ -> remove original content
ffmpeg / Remotion / compositor -> place new logo, UI, product art
```

Use Ubuntu + NVIDIA GPU if possible. On the current Windows machine, WSL Ubuntu is the better route for SAM2/Cutie/ProPainter than native Windows.

## If There Is No Local GPU

NVIDIA GPU is recommended but not required if the heavy model stage is moved to a hosted API.

There are two practical cloud/API routes:

### Route A: Use a Commercial Video Editing API

Use this when the goal is product demo output rather than owning every mask/inpainting step.

Options:

- Runway API: supports video/image generation endpoints and has product/ad workflow recipes, including product-related video workflows.
- Other generative video platforms with API access can be used similarly if they expose video-to-video, object removal, inpainting, or product-swap workflows.

Pros:

- Fastest way to get visually polished output.
- Less engineering work.
- No local GPU.

Cons:

- Less control over masks, tracking, and exact product placement.
- Pricing and model behavior may change.
- Commercial/video rights and privacy must be checked before uploading client assets.

### Route B: Hosted Open-Source Models As APIs

Use this when control matters and the pipeline should stay close to SAM2/XMem/ProPainter-style components.

Options:

- Replicate hosted model APIs. For example, `meta/sam-2-video` provides SAM 2 video segmentation as an API and runs on hosted NVIDIA GPU hardware.
- Replicate custom model deployment. Package SAM2/Cutie/E2FGVI/ProPainter with Cog and push it as a private or public cloud API.
- fal.ai hosted endpoints. fal has SAM2 image segmentation and many image/video model APIs; useful for frame-level segmentation and custom/serverless workflows depending on the exact endpoint available.

Pros:

- No local GPU required.
- More controllable than one-shot product-swap tools.
- Can keep the same pipeline shape as local open-source deployment.

Cons:

- Large video files need upload/download handling.
- Multi-step workflows can be slow and more expensive than local GPU once volume increases.
- Some open-source models have non-commercial licenses even when hosted; check model license before client/commercial use.

### Suggested No-GPU Architecture

For the current app:

```text
local /watch app
  -> download video
  -> extract frames and timestamps
  -> choose target shots
  -> send selected clip/frame + prompt points to cloud SAM2 API
  -> receive masks
  -> send clip + masks to hosted inpainting API or custom Replicate deployment
  -> receive repaired clip
  -> local ffmpeg / Remotion compositing
  -> final video
```

This keeps the current machine useful as the orchestrator while outsourcing only the GPU-heavy mask/inpainting steps.

### Practical Recommendation

For a first paid/API test:

1. Use Replicate `meta/sam-2-video` for mask generation on a short BYD clip.
2. Use a hosted/custom inpainting model for object/logo removal.
3. Keep final compositing local with ffmpeg/Remotion.

If the desired result is just a polished product ad and exact mask control is less important, test Runway-style product/video workflows first.
