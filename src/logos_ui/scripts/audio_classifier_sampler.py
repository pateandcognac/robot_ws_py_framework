#!/home/robot/robot_ws/.venv/bin/python3
"""
AudioClassifierSampler — MediaPipe YAMNet wrapper with temporal aggregation.

No ROS imports. Designed to be loaded lazily inside stt_node.py when the
/stt/audio_classifier/enable topic goes True.

YAMNet produces 521 raw AudioSet labels. We publish them as-is so downstream
consumers (e.g. the LLM context builder) can interpret them freely.

# TODO: Future option — label grouping / ontology collapse
#   YAMNet's 521 labels are hierarchical (AudioSet ontology). Many are very
#   specific ("Bathtub (filling or washing)", "Male speech, man speaking").
#   A LABEL_GROUPS dict could collapse related labels into ~20 buckets
#   (e.g. any label containing "speech"/"conversation"/"narration" → "Speech",
#   "Music"/"Singing"/"Instrument" → "Music", etc.) for a cleaner payload.
#   This would also make temporal boosting more meaningful — "Speech" would
#   accumulate counts instead of being split across 10 sub-labels.
#   Reference: https://research.google.com/audioset/ontology/index.html
"""

import time
import math
import collections
import numpy as np


class AudioClassifierSampler:
    """
    Wraps MediaPipe AudioClassifier (YAMNet) and maintains a temporal
    aggregation history for publication.

    Temporal aggregation:
      - _recent_samples: deque of the last N raw classification results
      - _minute_buckets: deque of per-wall-clock-minute accumulator dicts,
        covering the last 10 minutes

    Boosted score formula:
      boosted_score = avg_score * (1 + log1p(count) * boost_factor)

    This naturally elevates categories seen repeatedly across samples —
    even at individually modest confidence — above one-off detections.
    """

    def __init__(
        self,
        model_path: str,
        boost_factor: float = 0.5,
        top_k: int = 10,
        score_threshold: float = 0.05,
        recent_maxlen: int = 10,
        history_minutes: int = 10,
    ):
        self._model_path = model_path
        self._boost_factor = boost_factor
        self._top_k = top_k
        self._score_threshold = score_threshold

        self._recent_samples = collections.deque(maxlen=recent_maxlen)
        self._minute_buckets = collections.deque(maxlen=history_minutes)

        # Lazy mediapipe import — node starts cleanly even if not installed.
        from mediapipe.tasks import python as _mp_python
        from mediapipe.tasks.python import audio as _mp_audio
        from mediapipe.tasks.python.components.containers import AudioData as _AudioData

        self._AudioData = _AudioData

        base_opts = _mp_python.BaseOptions(model_asset_path=model_path)
        opts = _mp_audio.AudioClassifierOptions(
            base_options=base_opts,
            running_mode=_mp_audio.RunningMode.AUDIO_CLIPS,
            max_results=top_k,
            score_threshold=score_threshold,
        )
        self._classifier = _mp_audio.AudioClassifier.create_from_options(opts)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, audio_float32: np.ndarray, sample_rate: int = 16000) -> dict:
        """
        Run MediaPipe on a float32 audio clip, update internal history,
        and return the raw result dict for this sample.

        audio_float32 must be normalized to [-1.0, 1.0] and at least
        ~0.975 s long (15,600 samples at 16 kHz) for YAMNet to produce output.
        """
        audio_data = self._AudioData.create_from_array(
            audio_float32.astype(np.float32), sample_rate
        )
        # AUDIO_CLIPS mode returns a list of results (one per YAMNet window).
        # We aggregate across all windows and keep the highest score per label.
        result_list = self._classifier.classify(audio_data)

        epoch = time.time()
        categories = []
        if result_list:
            # Merge all windows: keep the max score seen for each label
            best: dict[str, float] = {}
            for result in result_list:
                if not result.classifications:
                    continue
                for cat in result.classifications[0].categories:
                    if cat.score >= self._score_threshold:
                        name = cat.category_name
                        if name not in best or cat.score > best[name]:
                            best[name] = cat.score
            categories = [
                {'name': name, 'score': round(score, 4)}
                for name, score in sorted(best.items(), key=lambda x: -x[1])
            ][:self._top_k]
        raw = {'epoch': epoch, 'categories': categories}
        self._recent_samples.append(raw)
        self._update_minute_buckets(epoch, categories)
        return raw

    def get_publication_payload(self) -> dict:
        """
        Build and return the full JSON-serializable payload for publication.

        Schema:
          {
            "per_minute": [
              {
                "start_epoch": float,
                "end_epoch":   float,
                "categories": [
                  {"name": str, "avg_score": float, "count": int, "boosted_score": float},
                  ...
                ]
              },
              ...           # oldest-first, up to history_minutes entries
            ],
            "recent": [
              {"epoch": float, "categories": [{"name": str, "score": float}, ...]},
              ...           # oldest-first, up to recent_maxlen entries
            ]
          }
        """
        self._prune_old_buckets()
        per_minute = []
        for b in self._minute_buckets:
            cats = self._compute_boosted_categories(b)
            per_minute.append({
                'start_epoch': b['start_epoch'],
                'end_epoch':   b['end_epoch'],
                'categories':  cats,
            })
        return {
            'per_minute': per_minute,
            'recent':     list(self._recent_samples),
        }

    def reset(self):
        """Clear all accumulated history (called when classifier is disabled)."""
        self._recent_samples.clear()
        self._minute_buckets.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_minute_buckets(self, epoch: float, categories: list):
        self._prune_old_buckets()

        # Floor to the current 60-second wall-clock boundary
        minute_start = epoch - (epoch % 60)

        bucket = None
        for b in self._minute_buckets:
            if b['start_epoch'] == minute_start:
                bucket = b
                break

        if bucket is None:
            bucket = {
                'start_epoch': minute_start,
                'end_epoch':   epoch,
                '_accum':      {},
            }
            self._minute_buckets.append(bucket)
        else:
            bucket['end_epoch'] = max(bucket['end_epoch'], epoch)

        for cat in categories:
            name = cat['name']
            if name not in bucket['_accum']:
                bucket['_accum'][name] = {'sum': 0.0, 'count': 0}
            bucket['_accum'][name]['sum']   += cat['score']
            bucket['_accum'][name]['count'] += 1

    def _compute_boosted_categories(self, bucket: dict) -> list:
        cats = []
        for name, acc in bucket['_accum'].items():
            avg     = acc['sum'] / acc['count']
            count   = acc['count']
            boosted = avg * (1.0 + math.log1p(count) * self._boost_factor)
            cats.append({
                'name':          name,
                'avg_score':     round(avg, 4),
                'count':         count,
                'boosted_score': round(boosted, 4),
            })
        # Sort by boosted score descending; drop anything below threshold
        cats.sort(key=lambda x: x['boosted_score'], reverse=True)
        return [c for c in cats if c['boosted_score'] >= 0.05]

    def _prune_old_buckets(self):
        cutoff = time.time() - 600  # 10 minutes
        while self._minute_buckets and self._minute_buckets[0]['start_epoch'] < cutoff:
            self._minute_buckets.popleft()
