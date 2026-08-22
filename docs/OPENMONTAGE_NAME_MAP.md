# OpenMontage × claude-video — pipeline / style 名称映射表

> **PIN WARNING**: The two tables below are a contract between
> `claude-video` and `OpenMontage_Voicebox`. Adding a value here means
> updating `OpenMontage_Voicebox/docs/claude-video-integration.md` §5
> in lockstep, and the corresponding `recompose` whitelist test on
> both sides. Removing a value is a breaking change.

The source for the claude-video side is `docs/todo.md` §2.6.2. The
source for the OpenMontage side is `OpenMontage_Voicebox/docs/
claude-video-integration.md` §5 (their whitelist audit at
`claude-video-whitelist-audit.md` is the green-light evidence).

## `pipeline` 映射

| claude-video `pipeline` (accepted by `recompose`) | OpenMontage `pipeline_defs/*.yaml` | GPU-free | Notes |
|---|---|---|---|
| `clip-factory` | `clip-factory.yaml` | yes | MVP — supported |
| `documentary-montage` | `documentary-montage.yaml` | yes | MVP — supported |
| `podcast-repurpose` | `podcast-repurpose.yaml` | yes | **Spelling correction** — was `podcast-reproduce` in `todo.md` §2.6.2 until F1 fix |
| `localization-dub` | `localization-dub.yaml` | yes | OM-side; gated on voice clone provider |
| `hybrid` | `hybrid.yaml` | yes | OM-side composition of two pipelines |
| `screen-demo` | `screen-demo.yaml` | yes | Added in F1 fix — was missing from `todo.md` despite existing in OM |

If claude-video wants to introduce a new pipeline value, the order is:

1. Open a PR here updating this table + `docs/todo.md` §2.6.2.
2. Add the corresponding `pipeline_defs/<name>.yaml` in OM.
3. Update `claude-video-whitelist-audit.md` with the GPU-free evidence.
4. Update the `recompose` whitelist test on both sides.

**Reverse direction (claude-video → OM)** is forbidden. claude-video
must never pass a string OM doesn't recognize and rely on OM to
fallback — that produces `ToolError: pipeline_not_in_whitelist` with
no path to recovery.

## `style` → OM playbook 映射

| claude-video `style` | OM playbook (`styles/*.yaml`) | Notes |
|---|---|---|
| `clean-professional` | `clean-professional` | Default |
| `flat-motion-graphics` | `flat-motion-graphics` | |
| `minimalist-diagram` | `minimalist-diagram` | |
| `premium-minimalist` | `premium-minimalist` | |
| `ink-sketch` | `ink-sketch` | |
| `anime-ghibli` | `anime-ghibli` | |
| *(anything else)* | `extra={"playbook_override": "<that value>"}` | **Only safe after the OM owner implements explicit handling in `tools/external/claude_video.py`. Passing an unknown style today returns `ToolError` from the OM adapter.** |

The last row is the escape hatch for cases where the caller knows
about a newer OM playbook that hasn't been mirrored into
claude-video's enum yet. It must NOT be the default — the default
should always pick a value from the pinned table.

## Where the whitelist is enforced

Three places must agree on the `pipeline` whitelist:

1. `docs/todo.md` §2.6.2 — the planning doc (already includes F1 fix).
2. `tests/fixtures/error_envelope_pipeline_not_in_whitelist.json` —
   the message body lists the allowed values; `tests/test_claude_video_shim.py::test_pipeline_not_in_whitelist_lists_allowed_values` pins that the fixture doesn't drift from the table.
3. The actual `recompose` tool implementation (Phase 2.6.2 work) —
   must validate `pipeline` against this table before calling OM.

## Related docs

- `docs/MCP_SERVER_PRD.md` §2.6 — the `recompose` tool signature and error envelope
- `docs/openmontage-integration-inputs.md` §2, §5 — origin of this mapping
- `docs/todo.md` §2.6.2 — where the whitelist is consumed
