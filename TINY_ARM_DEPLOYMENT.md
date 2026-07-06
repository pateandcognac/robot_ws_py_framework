# Deploying the arm-animation model — usage guide

Handoff for a Claude Code instance wiring the fine-tuned arm model into the
robot (`~/robot_ws/`, ROS Noetic catkin workspace). This document is only
about **how to call the model from Python and get valid arm-animation JSON
back**. Deployment nuance inside `robot_ws` (nodes, topics, timing, how the
animation gets rendered) is out of scope here and handled in that workspace.

The model, dataset, training, and evaluation that produced these
recommendations live in this repo (`~/src/ft_gemma_face/`). See "Where to
find more" at the bottom. This is the arm counterpart of `DEPLOYMENT.md`
(face model) in the same repo — same overall approach, different schema.

## ⚠️ Required robot_ws-side change before this can work: joint field rename

The model was trained to emit **`shoulder_roll`** and **`shoulder_pitch`**,
not the `joint1`/`joint2` names used by the current runtime schema at
`robot_ws/tools/arm_animation_schema.py`. This was an intentional rename done
on the training side for legibility (`joint1` rolls the whole arm away from
the torso/abduction, `joint2` is mounted on joint1's output and pitches the
arm within that plane) — but it means **every place in `robot_ws` that reads
`joint1`/`joint2` needs to accept `shoulder_roll`/`shoulder_pitch` instead**
before model output can drive the arms. Specifically, in
`arm_animation_schema.py`:

- `ARM_KEYS = ("joint1", "joint2", "wrist")` → `("shoulder_roll", "shoulder_pitch", "wrist")`
- `DEFAULT_ARM = {"joint1": 10.0, "joint2": -85.0, "wrist": 0.0}` → same values, renamed keys
- Change all legacy references to joint1 and joint2 throughout the code and json. Mostly just occurs in json, and few parts the json touches code. First change the json en masse, then search for lingering references in code.
- `wrist` is unchanged on both sides.
`schemas/arm_animation_schema.json` in this repo (`ft_gemma_face`) documents
the renamed semantic schema, mirroring the face schema.

## TL;DR

- The model runs as a **local Ollama model** on the robot's own CPU. Talk to
  it over Ollama's **HTTP API** at `http://localhost:11434/api/generate`.
- **No deployed-model recommendation yet** — unlike the face model (which has
  a full temperature/quant sweep behind its `q2_K` pick), the arm model has
  only had a small 5-8 prompt smoke test so far (see
  `scripts/export_gguf_notes.md`, "Arms: first real adapter" section). All
  three quants (`q8_0`, `Q4_K_M`, `q2_K`) passed that smoke test cleanly.
  **Start with `q4_K_M`** (105 MB, a reasonable size/quality middle ground)
  for initial hardware testing, temperature 0.3. 
- **Always pass the JSON schema** (`format` field) so decoding is
  grammar-constrained — same lesson as the face model, and already confirmed
  to help here (see smoke test results).
- **Frame count is a soft request, not a guarantee.** The prompt asks for a
  specific frame count (`"Create a JSON arm sequence with {N} frames..."`)
  and the model mostly complies but not exactly — in spot checks it matched
  the requested count on roughly 2 of 9 samples, usually (not always) under-producing by 1 when wrong. **Always read `len(result["frames"])` from the actual
  output** rather than assuming it equals what you asked for. The training
  data itself only covers 1–6 frame sequences (3–6 from the main corpus, 1–2
  from the hand-authored single-frame poses), so don't request frame counts
  outside that range and expect good results. Did some quick tests - let's
  base value of `N` on length of string being synthesized or prompt length..
  So:
  1 to 3 word prompt -> N="1 to 2" (literal string "1 to 2")
  4 to 7 words -> N="2 to 4"
  8 or more words -> "3 to 6"
  (try to count words but fall back to simple char counting if there isn't white space for some reason.)


## Prerequisites

1. `ollama serve` must be running on the robot (it hosts the model on
   `localhost:11434`). Any process on the box can then hit it.
2. The model must be present in Ollama's local registry. Check:
   ```bash
   ollama list | grep smollm2-135m-arm-lora-38k
   ```
   If it's missing, it needs to be (re)created from the GGUF via a
   `Modelfile` — that recipe is in `scripts/export_gguf_notes.md` in this
   repo (same mechanical steps as the face model: merge LoRA → GGUF →
   `llama-quantize` → `ollama create`).
3. Copy the schema file into your workspace so deployment isn't coupled to
   this experiment repo:
   ```bash
   cp ~/src/ft_gemma_face/schemas/arm_ollama_response_format.json ~/robot_ws/<wherever>/
   ```

## The two constants that must match training exactly

```python
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot arm animation. No markdown. No explanation."
USER_PROMPT   = "Create a JSON arm sequence with {n_frames} frames based on this input text: {text}"
```

## Minimal working example

```python
import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "smollm2-135m-arm-lora-38k:q4_K_M"
SYSTEM_PROMPT = "Generate only valid JSON for a Logos robot arm animation. No markdown. No explanation."

# Load once at startup, not per-call.
with open("arm_ollama_response_format.json") as f:
    RESPONSE_SCHEMA = json.load(f)


def generate_arms(text: str, n_frames: int = 4, temperature: float = 0.0, seed: int = 42, timeout_s: float = 45) -> dict:
    """Robot utterance/emoji + desired frame count in -> arm-animation dict out.

    n_frames is a request, not a guarantee -- check len(result["frames"])
    against what you actually got before using it downstream. Stick to 1-6;
    that's the range the training data covers.
    """
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "prompt": f"Create a JSON arm sequence with {n_frames} frames based on this input text: {text}",
            "stream": False,
            "format": RESPONSE_SCHEMA,          # <-- grammar-constrained decoding
            "options": {"temperature": temperature, "seed": seed},
        },
        timeout=timeout_s,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["response"])


if __name__ == "__main__":
    result = generate_arms("wave hello enthusiastically", n_frames=4)
    print(json.dumps(result, indent=2))
    print(f"requested 4 frames, got {len(result['frames'])}")
```

That's the whole interface: **text (+ desired frame count) in, arm-animation
`dict` out.**

## What comes out

A dict shaped like the training data:

```jsonc
{
  "emoji": "🙂",
  "frames": [                          // requested count is a soft target, not exact; dataset covers 1-6
    {
      "beat": "short description of this frame's pose",
      "arms": {                        // sides: "both", or "left"/"right" for asymmetry
        "both": {
          "shoulder_roll": -10.0,      // [-90, 90] degrees -- rolls the arm away from the torso
          "shoulder_pitch": -85.0,     // [-90, 90] degrees -- pitches the arm within that plane
          "wrist": 0.0                 // [-90, 90] degrees
        }
      }
    }
    // ...
  ]
}
```

The authoritative field list and ranges are in
`schemas/arm_animation_schema.json` (plain-JSON constraint spec) and
`schemas/arm_ollama_response_format.json` (the JSON-Schema you pass to
Ollama) — both in this repo, both already using the renamed
`shoulder_roll`/`shoulder_pitch` fields.

## Important: what schema mode does and doesn't guarantee

Grammar-constrained decoding (the `format` field) enforces **structure**:
required keys, types, frame-array length within [2, 12], no stray/extra
keys, no markdown. It does **not** enforce:

- **Numeric min/max** — llama.cpp's schema→grammar conversion has no way to
  bound a number's value, only its lexical shape. Manual spot checks so far
  found zero range violations, but that's a small sample (see
  `export_gguf_notes.md`) — don't assume this never happens on faces'-model
  precedent (faces do occasionally overshoot by 5-25%). A defensive clamp to
  [-90, 90] before use is cheap insurance.
- **Exact frame count** — see the TL;DR above. This is the one gap that
  showed up consistently across every quant level tested, including q8_0, so
  it reads as a training-signal limitation rather than something
  quantization-related.

## Gotchas (learned the hard way in the face pipeline — apply here too)

- **Use the HTTP API, never the `ollama run` CLI** for anything parsed
  programmatically. The CLI injects cursor-control escape codes into piped
  stdout that look exactly like corrupted JSON but aren't.
- **Pin `seed`** in `options` if you want reproducible output at a given
  temperature. (At temp 0 the seed is inert — greedy decoding is
  deterministic anyway, modulo tiny CPU floating-point non-associativity.)
- **A generation that hangs well past normal latency (>45 s when typical is
  ~3-4 s) is a degenerate repetition loop**, not slow progress — time it out
  and treat it as a failure rather than waiting it out.
- **Keep the system prompt byte-identical across calls.** Ollama/llama.cpp
  KV-caches a constant prefix, so a fixed system prompt is prefilled once and
  reused. Varying it per call throws that cache away.
- **Load the schema file once at startup**, not per request.
- **The face model supports streaming interpretation for low latency-to-first-frame**
  (~1-2s) — the same approach should work here since it's the same base
  architecture and Ollama streaming API, but it hasn't been specifically
  verified for the arm schema/output shape yet. Worth confirming before
  relying on it.

## Where to find more

Everything lives in `~/src/ft_gemma_face/`:

- `README.md` — full pipeline, dataset design, project status (arm section
  describes the dataset build: 1487-emoji corpus + numeric perturbation +
  single-frame pose pairs, 38,554 total rows).
- `scripts/export_gguf_notes.md` — the live results log, including the
  "Arms: first real adapter" section with the current (limited) smoke-test
  numbers and explicit caveats about what hasn't been tested yet.
- `scripts/eval_ollama_arm_candidates.py` — reusable deterministic comparison
  harness for arm candidates (text-mode vs schema-mode, multiple models) —
  arm counterpart of `eval_ollama_candidates.py`.
- `scripts/chat_face_model.py --domain arm` — interactive REPL for manual
  zero-shot testing (`/frames <n>` slash command to change the requested
  frame count on the fly).
- `schemas/arm_ollama_response_format.json` — the schema to pass to Ollama.
- `schemas/arm_animation_schema.json` — the authoritative field/range spec
  (renamed fields already applied).
- `scripts/arm_common.py` — the canonical `ARM_SYSTEM_PROMPT` /
  `ARM_USER_PROMPT_TEMPLATE` constants (source of truth if this doc ever
  drifts) and the `joint1/joint2 -> shoulder_roll/shoulder_pitch` rename
  mapping used when the training dataset was built.
