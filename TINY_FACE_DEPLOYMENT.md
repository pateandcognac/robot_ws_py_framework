# Deploying the face-animation model — usage guide

Handoff for a Claude Code instance wiring the fine-tuned face model into the
robot (`~/robot_ws/`, ROS Noetic catkin workspace). This document is only
about **how to call the model from Python and get valid face-animation JSON
back**. Deployment nuance inside `robot_ws` (nodes, topics, timing, how the
animation gets rendered) is out of scope here and handled in that workspace.

The model, dataset, training, and all the evaluation that produced these
recommendations live in this repo (`~/src/ft_gemma_face/`). See "Where to
find more" at the bottom.

## TL;DR

- The model runs as a **local Ollama model** on the robot's own CPU. Talk to
  it over Ollama's **HTTP API** at `http://localhost:11434/api/generate`.
- **Deployed model: `smollm2-135m-face-lora-34k:q2_K`** (88 MB, ~4 s/gen on
  the robot's CPU). This is the winner after a full model/quant/decoding
  sweep — smallest and fastest, and just as reliable as far larger configs
  once schema mode is on.
- **Always pass the JSON schema** (`format` field) so decoding is
  grammar-constrained. This is what makes the tiny/heavily-quantized model
  reliable — without it, q2_K is unusable; with it, it's ~24/25 valid.
- **Temperature is your knob**: `0` for maximum reliability / repeatable
  faces, `0.5` for per-utterance variety at a tiny validity cost. Do not
  exceed `0.8`; `1.0` produces occasional garbage.

## Prerequisites

1. `ollama serve` must be running on the robot (it hosts the model on
   `localhost:11434`). Any process on the box can then hit it.
2. The model must be present in Ollama's local registry. Check:
   ```bash
   ollama list | grep smollm2-135m-face-lora-34k
   ```
   You want the `:q2_K` tag. If it's missing, it needs to be
   (re)created from the GGUF via a `Modelfile` — that recipe is in
   `scripts/export_gguf_notes.md` in this repo. (The `:q8_0` and `:Q4_K_M`
   tags also exist and are interchangeable in the code below if you ever want
   to trade size for a small quality bump — q8_0 is 144 MB.)
3. Copy the schema file into your workspace so deployment isn't coupled to
   this experiment repo:
   ```bash
   cp ~/src/ft_gemma_face/schemas/ollama_response_format.json ~/robot_ws/<wherever>/
   ```

## The two constants that must match training exactly

The model was fine-tuned with a specific system prompt and user-prompt
template. **Use them verbatim** — the model expects this framing and drifts
if you change it.

```python
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot face animation. No markdown. No explanation."
USER_PROMPT   = "Generate JSON face animation for text: {text}"
```

## Minimal working example

```python
import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "smollm2-135m-face-lora-34k:q2_K"
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot face animation. No markdown. No explanation."

# Load once at startup, not per-call.
with open("ollama_response_format.json") as f:
    RESPONSE_SCHEMA = json.load(f)


def generate_face(text: str, temperature: float = 0.0, seed: int = 42, timeout_s: float = 45) -> dict:
    """Robot utterance/emoji in -> face-animation dict out. Raises on failure."""
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": f"Generate JSON face animation for text: {text}",
            "stream": False,
            "format": RESPONSE_SCHEMA,          # <-- grammar-constrained decoding
            "options": {"temperature": temperature, "seed": seed},
        },
        timeout=timeout_s,
    )
    resp.raise_for_status()
    # With schema mode + this model, `.response` is a JSON string that parses
    # cleanly. json.loads can still be defended against as belt-and-suspenders.
    return json.loads(resp.json()["response"])


if __name__ == "__main__":
    print(json.dumps(generate_face("🥱 I could use a recharge"), indent=2))
    print(json.dumps(generate_face("victory!", temperature=0.5), indent=2))  # a little variety
```

That's the whole interface: **text in, face-animation `dict` out.**

## What comes out

A dict shaped like the training data / production LUT:

```jsonc
{
  "emoji": "🥱",
  "frames": [                    // 4–9 frames
    {
      "beat": "short description of this frame's expression",
      "eyes": {                  // sides: "both", or "left"/"right" for asymmetry
        "both": {
          "gaze_x": 0.0, "gaze_y": -0.1,     // [-1, 1]
          "scale_x": 0.5, "scale_y": 0.4,    // [0, 1]
          "lid_height": 0.3,                 // [-1, 1]
          "lid_angle": 5,                    // [-45, 45] degrees
          "color": "#00FFC8"                 // hex
        }
      },
      "mouth": {
        "frequency": 1.2, "amplitude": 0.5,  // freq [0.01,20], amp [0,1]
        "phase": 0.1, "phase_increment": 0.05,  // [-3.15,3.15] / [-3.14,3.14]
        "color": "#FFDF00"
      }
    }
    // ...
  ]
}
```

The authoritative field list and ranges are in
`schemas/face_animation_schema.json` (plain-JSON constraint spec) and
`schemas/ollama_response_format.json` (the JSON-Schema you pass to Ollama).

## Important: what schema mode does and doesn't guarantee

Grammar-constrained decoding (the `format` field) enforces **structure**:
required keys, types, hex-color pattern, frame count (4–9), no stray/extra
keys, no markdown. It does **not** enforce numeric **min/max** — llama.cpp's
schema→grammar conversion has no way to bound a float's value, only its
lexical shape.

So the one residual failure mode is a numeric value slightly out of range
(e.g. `gaze_x=-1.05`, `amplitude=1.3`). In practice this is benign:

- It's rare and small at low temperature (at temp 0, ~24/25 outputs are fully
  in-range, and the rare overshoot is typically <10% past the bound).
- These overshoots are the model reaching for *more* expressiveness than the
  range allows (a wider mouth, a further glance) — the right instinct, just
  past the fence.
- **The `robot_ws` animation pipeline already clamps values internally**, so
  these are handled downstream and render as the intended (maxed-out)
  expression. No extra guarding needed on the generation side, though a
  defensive clamp before use is cheap if you want belt-and-suspenders.

## Temperature guidance (measured, not guessed)

Validity decays gracefully and linearly as temperature rises — there's no
cliff, so temperature is a safe tunable knob:

| temperature | schema-valid (of 25) | use when |
|---|---|---|
| **0.0** | **~24/25** | you want maximum reliability / the same face for the same input |
| **0.5** | ~23/25 | you want per-utterance variety; still essentially all overshoots are trivial/benign |
| 0.8 | ~20/25 | pushing it; occasional larger overshoots start appearing |
| 1.0 | ~19/25 | **avoid** — rare but catastrophic single-token garbage (e.g. a `phase_increment` of 1e7) |

**Recommendation:** deploy at **temp 0** for reliability, or **0.5** if you
want the face to vary a bit between repeats of the same utterance. Stay ≤ 0.8.

## Gotchas (learned the hard way in this repo — don't rediscover them)

- **Use the HTTP API, never the `ollama run` CLI** for anything parsed
  programmatically. The CLI injects cursor-control escape codes into piped
  stdout that look exactly like corrupted JSON but aren't.
- **Pin `seed`** in `options` if you want reproducible output at a given
  temperature. (At temp 0 the seed is inert — greedy decoding is
  deterministic anyway, modulo tiny CPU floating-point non-associativity.)
- **A generation that hangs well past normal latency (>45 s when typical is
  ~4 s) is a degenerate repetition loop**, not slow progress — time it out
  and treat it as a failure rather than waiting it out. These are very rare at
  low temperature.
- **Keep the system prompt byte-identical across calls.** Ollama/llama.cpp
  KV-caches a constant prefix, so a fixed system prompt is prefilled once and
  reused — the schema/instruction overhead is effectively free after the first
  request. Varying it per call throws that cache away.
- **Load the schema file once at startup**, not per request.

## Where to find more

Everything lives in `~/src/ft_gemma_face/`:

- `README.md` — full pipeline, dataset design, project status.
- `scripts/export_gguf_notes.md` — the live results log: quantization ×
  tuning-method findings, the schema-mode rescue results, the Modelfile /
  GGUF / quantize recipe to recreate any model tag.
- `scripts/eval_ollama_candidates.py` — reusable deterministic comparison
  harness (text-mode vs schema-mode, multiple models).
- `scripts/eval_temp_sweep.py` — temperature sweep + range-overshoot analysis
  (how the temperature table above was produced; rerun it against any model
  tag).
- `schemas/ollama_response_format.json` — the schema to pass to Ollama.
- `schemas/face_animation_schema.json` — the authoritative field/range spec.
- `scripts/common.py` — the canonical `SYSTEM_PROMPT` / user-prompt template
  constants (source of truth if this doc ever drifts).
