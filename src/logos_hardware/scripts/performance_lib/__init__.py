"""
performance_lib: shared runtime library for the Logos Text-to-Performance
(TTP v2) pipeline.

Modules:
- face_schema: canonical semantic face-animation schema utilities
  (tools/face_animation_schema.py re-exports from here).
- luts: on-disk animation LUT loaders (semantic face, legacy arms).
- face_gen_client: Ollama client for the tiny face-animation model,
  including streaming frame parsing and the saved-generation store.
"""
