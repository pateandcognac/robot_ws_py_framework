#!/home/robot/robot_ws/.venv/bin/python3
"""Logos ears using Nemotron 3.5 ONNX streaming ASR."""

import json
import os
import queue
import re
import readline
import time
from datetime import datetime

import numpy as np
import rospy
import sounddevice as sd
import torch
from colorama import Fore, Style
from kobuki_msgs.msg import Sound
from std_msgs.msg import String

from logos_framework.msg import CognitionInput
from stt_node import (
    AMBIENT_HISTORY_MAX_AGE,
    AMBIENT_HISTORY_MAX_CHARS,
    AMBIENT_VAD_THRESHOLD,
    CHANNELS,
    CLASSIFIER_SAMPLE_FRAMES,
    CLASSIFIER_SAMPLE_INTERVAL,
    FRAME_LENGTH,
    LedState,
    LogosEarsNode,
    RECORDING_TIMEOUT,
    SAMPLE_RATE,
    WAKEWORD_VAD_THRESHOLD,
    WAKE_TRIGGER_NOTE,
)
from nemotron_asr import (
    NemotronModel,
    load_vocabulary,
    normalize_vocabulary,
    read_model_config,
)


DEFAULT_MODEL_PATH = os.path.expanduser(
    "~/robot_ws/models/nemotron-3.5-asr-streaming-0.6b-onnx-int4"
)
DEFAULT_VOCAB_PATH = os.path.expanduser(
    "~/robot_ws/config/nemotron_custom_vocabulary.json"
)
CAPTURE_BLOCK_SAMPLES = 2048
AMBIENT_PUBLISH_INTERVAL = 60.0
AMBIENT_EVENT_TOPIC = "/stt/ambient_listener/events"
AMBIENT_EVENT_MAX_CHARS = 50
AMBIENT_EVENT_SILENCE_TIMEOUT = 2.0
AMBIENT_EVENT_PUNCTUATION_RE = re.compile(r"[,\.;:!\?\n]+")


class LogosNemotronEarsNode(LogosEarsNode):
    def _init_ros(self):
        super()._init_ros()
        self.pub_ambient_events = rospy.Publisher(
            AMBIENT_EVENT_TOPIC,
            String,
            queue_size=10,
        )

    def _init_models(self):
        print(Fore.CYAN + "Loading OpenWakeWord, VAD, and Nemotron...")

        core_wakewords, core_thresholds = self._configured_core_wakewords()
        self.core_wakeword_models = self._discover_role_models(
            core_wakewords,
            core_thresholds,
        )
        frameworks = {
            model["framework"]
            for model in self.core_wakeword_models.values()
        }
        self._validate_feature_models(frameworks)
        self.core_wakewords = self._load_wakeword_models(
            self.core_wakeword_models
        )
        self.passive_hotword_directories = ()
        self.passive_wakeword_models = {}
        self.passive_wakewords = {}
        self._last_hotword_time = {}

        self.vad_model, utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            onnx=True,
        )
        (
            self.get_speech_timestamps,
            self.save_audio,
            self.read_audio,
            self.VADIterator,
            self.collect_chunks,
        ) = utils

        model_path = os.path.expanduser(
            rospy.get_param("~model_path", DEFAULT_MODEL_PATH)
        )
        vocab_path = os.path.expanduser(
            rospy.get_param("~vocab_path", DEFAULT_VOCAB_PATH)
        )
        threads = int(rospy.get_param("~threads", 1))
        if not os.path.isfile(os.path.join(model_path, "genai_config.json")):
            raise FileNotFoundError(
                f"Nemotron model not found at {model_path}. See "
                "docs/nemotron_asr_poc_notes.md."
            )

        model_config = read_model_config(model_path)
        if model_config["sample_rate"] != SAMPLE_RATE:
            raise ValueError(
                "Nemotron model sample rate must be "
                f"{SAMPLE_RATE}, got {model_config['sample_rate']}"
            )
        self.nemotron_chunk_samples = model_config["chunk_samples"]
        self.nemotron = NemotronModel(
            model_path,
            threads=threads,
            use_vad=False,
        )
        self.nemotron_vocabulary = load_vocabulary(vocab_path)
        print(
            Fore.CYAN
            + f"Nemotron loaded from {model_path} with {threads} ONNX thread(s) "
            f"and {self.nemotron_chunk_samples}-sample chunks."
        )

    def _audio_capture_loop(self):
        """Use stable 128 ms PortAudio reads, then restore 32 ms analysis frames."""
        while self.running and not rospy.is_shutdown():
            for device_name in self.audio_device_candidates:
                opened_stream = False
                if not self.running or rospy.is_shutdown():
                    return
                try:
                    with sd.InputStream(
                        samplerate=SAMPLE_RATE,
                        blocksize=CAPTURE_BLOCK_SAMPLES,
                        channels=CHANNELS,
                        dtype="int16",
                        device=device_name,
                        latency="high",
                    ) as stream:
                        opened_stream = True
                        self.active_audio_device = device_name
                        rospy.loginfo(
                            f"Nemotron STT capture using device: {device_name}"
                        )

                        while self.running and not rospy.is_shutdown():
                            pcm, overflow = stream.read(CAPTURE_BLOCK_SAMPLES)
                            if overflow:
                                rospy.logwarn("Nemotron STT audio overflow")
                            pcm_int16 = pcm[:, 0].copy()
                            self.current_volume = np.sqrt(
                                np.mean(pcm_int16.astype(float) ** 2)
                            )

                            with self.state_lock:
                                if self.is_speaking:
                                    continue

                            for start in range(
                                0, len(pcm_int16), FRAME_LENGTH
                            ):
                                frame = pcm_int16[start:start + FRAME_LENGTH]
                                if len(frame) == FRAME_LENGTH:
                                    self.audio_queue.put(frame)
                except Exception as exc:
                    if opened_stream:
                        rospy.logerr(
                            f"Nemotron audio capture error on {device_name}: {exc}"
                        )
                    else:
                        rospy.logwarn(
                            f"STT audio device unavailable ({device_name}): {exc}"
                        )
                    self.active_audio_device = None

            if self.running and not rospy.is_shutdown():
                rospy.logerr(
                    "No configured STT audio devices are available; retrying "
                    f"in 2 seconds: {', '.join(self.audio_device_candidates)}"
                )
                time.sleep(2.0)

    def _cb_ambient_enable(self, msg):
        was_enabled = self.ambient_enabled
        super()._cb_ambient_enable(msg)
        if was_enabled and not msg.data:
            self.job_queue.put({"type": "ambient_reset"})
        elif msg.data and not was_enabled:
            self.job_queue.put({"type": "ambient_start"})

    def _brain_loop(self):
        last_classifier_sample = time.time()
        next_ambient_publish = time.monotonic() + AMBIENT_PUBLISH_INTERVAL
        last_ambient_speech_time = 0.0
        ambient_event_timeout_sent = False

        while self.running and not rospy.is_shutdown():
            try:
                pcm_int16 = self.audio_queue.get(timeout=0.25)
            except queue.Empty:
                pcm_int16 = None

            now = time.time()
            monotonic_now = time.monotonic()
            with self.state_lock:
                ambient_enabled = self.ambient_enabled

            if ambient_enabled and monotonic_now >= next_ambient_publish:
                self.job_queue.put({"type": "ambient_flush"})
                next_ambient_publish = (
                    monotonic_now + AMBIENT_PUBLISH_INTERVAL
                )
            elif not ambient_enabled:
                next_ambient_publish = (
                    monotonic_now + AMBIENT_PUBLISH_INTERVAL
                )
                last_ambient_speech_time = 0.0
                ambient_event_timeout_sent = False

            if pcm_int16 is None:
                continue

            with self.state_lock:
                reset_wakewords_pending = self.reset_wakewords_pending
                self.reset_wakewords_pending = False
            if reset_wakewords_pending:
                self._reset_wakeword_models()

            pcm_float32 = pcm_int16.astype(np.float32) / 32768.0
            vad_prob = self.vad_model(
                torch.from_numpy(pcm_float32), SAMPLE_RATE
            ).item()
            self.wakeword_vad_history.append(vad_prob)
            is_ambient_speech = vad_prob > AMBIENT_VAD_THRESHOLD
            is_wakeword_speech = (
                max(self.wakeword_vad_history, default=0.0)
                > WAKEWORD_VAD_THRESHOLD
            )
            self.is_speech_detected = is_ambient_speech
            core_detections, passive_detections = self._predict_wakewords(
                pcm_int16,
                allow_detections=is_wakeword_speech,
            )

            with self.state_lock:
                state = self.current_state
                ambient_enabled = self.ambient_enabled

            if state == LedState.RECORDING:
                self.job_queue.put(
                    {
                        "type": "asr_audio",
                        "mode": "human_stt",
                        "audio": pcm_float32,
                    }
                )
                if now - self.recording_start_time > RECORDING_TIMEOUT:
                    print(Fore.RED + "Recording timeout reached.")
                    self._finish_recording(reason="timeout")
                elif self._recording_vad_timeout_elapsed(is_ambient_speech, now):
                    print(Fore.GREEN + "VAD silence timeout reached; finishing input.")
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="normal")
                elif not self.recording_vad_only and "end" in core_detections:
                    label = self.core_wakeword_models["end"]["label"]
                    self._publish_hotword(label)
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="normal")
                elif not self.recording_vad_only and "cancel" in core_detections:
                    label = self.core_wakeword_models["cancel"]["label"]
                    self._publish_hotword(label)
                    self._play_sound(Sound.OFF)
                    self._cancel_active_wake_context_annotation()
                    self.job_queue.put({"type": "human_cancel"})
                    self._send_feedback(
                        header="Canceled!",
                        body=" - Stopped listening - ",
                        body_color="white",
                        header_color="bright_yellow",
                        font="doom",
                    )
                    self.recording_start_time = 0
                    self._reset_recording_vad_timeout()
                    self._reset_state()
                    self._reset_wakeword_models()
                elif "edit" in core_detections:
                    label = self.core_wakeword_models["edit"]["label"]
                    self._publish_hotword(label)
                    self._play_sound(Sound.OFF)
                    self._finish_recording(reason="edit")
                continue

            if "wake" in core_detections:
                label = self.core_wakeword_models["wake"]["label"]
                print(Fore.MAGENTA + f"Wake word: {label} detected!")
                self._publish_hotword(label)
                self._publish_python_interrupt()
                self._play_sound(Sound.ON)

                wake_context_id = None
                if ambient_enabled:
                    wake_context_id = self._next_wake_context_id()
                    self.job_queue.put(
                        {
                            "type": "ambient_flush",
                            "wake_trigger": True,
                            "wake_context_id": wake_context_id,
                            "prefetch_after": True,
                        }
                    )
                else:
                    self._publish_cognition_prefetch()

                self.job_queue.put({"type": "human_start"})
                with self.state_lock:
                    self.current_state = LedState.RECORDING
                    self.classifier_sample_buffer = []
                self._send_feedback(
                    header="Listening...",
                    body=self._recording_prompt_text(),
                    header_color="bright_green",
                    body_color="bright_white",
                    font="slant",
                )
                with self.ambient_history_lock:
                    self._active_wake_context_id = wake_context_id
                self.recording_start_time = now
                self._reset_recording_vad_timeout()
                continue

            for role in passive_detections:
                detected = self._wakeword_label(role)
                if detected:
                    self._publish_hotword(detected)

            if ambient_enabled and is_ambient_speech:
                last_ambient_speech_time = monotonic_now
                ambient_event_timeout_sent = False
                self.job_queue.put(
                    {
                        "type": "asr_audio",
                        "mode": "ambient",
                        "audio": pcm_float32,
                    }
                )
            elif (
                ambient_enabled
                and last_ambient_speech_time
                and not ambient_event_timeout_sent
                and monotonic_now - last_ambient_speech_time
                >= AMBIENT_EVENT_SILENCE_TIMEOUT
            ):
                self.job_queue.put({"type": "ambient_event_timeout"})
                ambient_event_timeout_sent = True

            with self.state_lock:
                classifier_enabled = self.classifier_enabled
                is_speaking = self.is_speaking
            if classifier_enabled and not is_speaking:
                self.classifier_sample_buffer.append(pcm_float32)
                if len(self.classifier_sample_buffer) > CLASSIFIER_SAMPLE_FRAMES:
                    self.classifier_sample_buffer = self.classifier_sample_buffer[
                        -CLASSIFIER_SAMPLE_FRAMES:
                    ]
                if (
                    not self._classifier_job_pending
                    and len(self.classifier_sample_buffer)
                    >= CLASSIFIER_SAMPLE_FRAMES
                    and now - last_classifier_sample
                    >= CLASSIFIER_SAMPLE_INTERVAL
                ):
                    sample = np.concatenate(
                        self.classifier_sample_buffer[-CLASSIFIER_SAMPLE_FRAMES:]
                    )
                    self.job_queue.put(
                        {
                            "type": "audio_classify",
                            "audio": sample,
                            "epoch": now,
                        }
                    )
                    self._classifier_job_pending = True
                    last_classifier_sample = now
            elif self.classifier_sample_buffer:
                self.classifier_sample_buffer = []

    def _finish_recording(self, reason="normal"):
        print(Fore.GREEN + "Finalizing streaming input...")
        self._reset_recording_vad_timeout()
        with self.state_lock:
            self.current_state = LedState.TRANSCRIBING
        self.job_queue.put(
            {
                "type": "human_finish",
                "edit_mode": reason == "edit",
                "stop_reason": reason,
            }
        )
        self.recording_start_time = 0

    @staticmethod
    def _new_stream_state(model):
        return {
            "recognizer": model.create_stream(),
            "pending": np.empty(0, dtype=np.float32),
            "event_buffer": "",
        }

    def _feed_stream(self, stream_state, audio):
        stream_state["pending"] = np.concatenate(
            (stream_state["pending"], audio)
        )
        emitted = ""
        while len(stream_state["pending"]) >= self.nemotron_chunk_samples:
            chunk = stream_state["pending"][:self.nemotron_chunk_samples]
            stream_state["pending"] = stream_state["pending"][
                self.nemotron_chunk_samples:
            ]
            emitted += stream_state["recognizer"].process(chunk)
        return emitted

    def _finish_stream(self, stream_state):
        if len(stream_state["pending"]):
            emitted = stream_state["recognizer"].process(
                stream_state["pending"]
            )
        else:
            emitted = ""
        emitted += stream_state["recognizer"].flush()
        return emitted

    def _drain_stream_pending(self, stream_state):
        if not len(stream_state["pending"]):
            return ""
        emitted = stream_state["recognizer"].process(
            stream_state["pending"]
        )
        stream_state["pending"] = np.empty(0, dtype=np.float32)
        return emitted

    @staticmethod
    def _squash_spaces(text):
        return re.sub(r"\s+", " ", text).strip()

    def _publish_ambient_event_text(self, text):
        text = normalize_vocabulary(text, self.nemotron_vocabulary)
        text = self._strip_control_phrases(text)
        text = self._squash_spaces(text)
        if not text:
            return
        self.pub_ambient_events.publish(String(data=text))
        print(Fore.CYAN + f"Ambient event: {text[:80]}")

    def _split_ambient_event_buffer(self, text, flush=False):
        fragments = []
        buffer = text

        while buffer:
            punctuation = AMBIENT_EVENT_PUNCTUATION_RE.search(buffer)
            if punctuation:
                chunk = buffer[:punctuation.end()]
                buffer = buffer[punctuation.end():].lstrip()
                chunk = self._squash_spaces(chunk)
                if chunk:
                    fragments.append(chunk)
                continue

            if len(buffer) <= AMBIENT_EVENT_MAX_CHARS:
                break

            split_at = buffer.rfind(" ", 0, AMBIENT_EVENT_MAX_CHARS + 1)
            if split_at <= 0:
                split_at = buffer.find(" ", AMBIENT_EVENT_MAX_CHARS)
            if split_at <= 0:
                break

            chunk = self._squash_spaces(buffer[:split_at])
            buffer = buffer[split_at:].lstrip()
            if chunk:
                fragments.append(chunk)

        if flush:
            tail = self._squash_spaces(buffer)
            if tail:
                fragments.append(tail)
            buffer = ""

        return fragments, buffer

    def _publish_ambient_event_fragments(
        self,
        stream_state,
        emitted,
        flush=False,
    ):
        if emitted:
            stream_state["event_buffer"] += emitted
        fragments, stream_state["event_buffer"] = (
            self._split_ambient_event_buffer(
                stream_state["event_buffer"],
                flush=flush,
            )
        )
        for fragment in fragments:
            self._publish_ambient_event_text(fragment)

    def _publish_ambient_text(
        self,
        text,
        wake_trigger=False,
        wake_context_id=None,
    ):
        text = normalize_vocabulary(text, self.nemotron_vocabulary)
        text = self._strip_control_phrases(text)
        if not text:
            return False
        if wake_trigger:
            text += WAKE_TRIGGER_NOTE

        new_entry = {
            "time": datetime.now().strftime("%I:%M %p"),
            "epoch": time.time(),
            "transcription": text,
        }
        if wake_context_id is not None:
            new_entry["_wake_context_id"] = wake_context_id

        with self.ambient_history_lock:
            if wake_context_id in self._canceled_wake_context_ids:
                new_entry["transcription"] = self._without_wake_trigger_note(
                    new_entry["transcription"]
                )
                self._canceled_wake_context_ids.discard(wake_context_id)
            self.ambient_history.append(new_entry)
            current_time = time.time()
            self.ambient_history = [
                entry
                for entry in self.ambient_history
                if current_time - entry["epoch"] < AMBIENT_HISTORY_MAX_AGE
            ]
            total_chars = sum(
                len(entry["transcription"])
                for entry in self.ambient_history
            )
            while (
                total_chars > AMBIENT_HISTORY_MAX_CHARS
                and len(self.ambient_history) > 1
            ):
                removed = self.ambient_history.pop(0)
                total_chars -= len(removed["transcription"])
            payload = self._ambient_history_payload_locked()

        self.pub_ambient.publish(json.dumps(payload))
        self.last_ambient_publish_time = time.time()
        self._send_feedback(
            header="--- overheard ---",
            body=text,
            header_color="bright_yellow",
            body_color="bright_blue",
            font="term",
        )
        print(Fore.CYAN + f"Ambient published: {text[:80]}")
        return True

    def _handle_classifier_job(self, job):
        self._classifier_job_pending = False
        if self.classifier_sampler is None:
            return
        with self.state_lock:
            is_speaking = self.is_speaking
            state = self.current_state
        if (
            is_speaking
            or state in (LedState.RECORDING, LedState.TRANSCRIBING)
            or len(job["audio"]) < 15600
        ):
            return
        try:
            raw = self.classifier_sampler.classify(job["audio"])
            payload = self.classifier_sampler.get_publication_payload()
            with self.state_lock:
                is_speaking = self.is_speaking
                state = self.current_state
            if is_speaking or state in (
                LedState.RECORDING,
                LedState.TRANSCRIBING,
            ):
                return
            self.last_classifier_sample_time = time.time()
            self.pub_classifier.publish(json.dumps(payload))
            top = ", ".join(
                f"{category['name']}({category['score']:.2f})"
                for category in raw["categories"][:3]
            )
            print(Fore.YELLOW + f"Audio Classifier: {top or '(none)'}")
        except Exception as exc:
            rospy.logwarn(f"Audio Classifier: classify() failed: {exc}")

    def _publish_human_text(self, text, edit_mode=False, stop_reason="normal"):
        text = normalize_vocabulary(text, self.nemotron_vocabulary)
        text = self._strip_control_phrases(text)
        final_text = text.strip()

        if edit_mode and final_text:

            def hook():
                readline.insert_text(final_text)
                readline.redisplay()

            readline.set_pre_input_hook(hook)
            try:
                final_text = input(
                    f"{Fore.GREEN}Edit > {Style.RESET_ALL}"
                ).strip()
            except (EOFError, KeyboardInterrupt):
                pass
            finally:
                readline.set_pre_input_hook(None)

        if not final_text:
            print(Fore.YELLOW + "Nemotron transcript was empty.")
            self._reset_state()
            return

        content_meta = []
        hint_parts = [ "" ]
        if stop_reason == "timeout":
            content_meta.append("\n[Timed out!]")
            hint_parts.append(
                f"<!-- system: The latest human_stt input recording timed out after {RECORDING_TIMEOUT} seconds; "
                "this might indicate an accidental wake trigger."
            )
            hint_parts.append(" -->")
        if content_meta:
            final_text += "\n\n" + "\n".join(content_meta)

        msg = CognitionInput()
        msg.type = "human_stt"
        msg.content = final_text
        msg.system_hint = " ".join(hint_parts)
        msg.loop_cognition = True
        self.pub_cognition.publish(msg)
        print(Fore.GREEN + f"Published <human_stt>: {final_text}")
        self._reset_state()

    def _scribe_loop(self):
        ambient = self._new_stream_state(self.nemotron)
        human = None

        while self.running and not rospy.is_shutdown():
            try:
                job = self.job_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            job_type = job["type"]
            if job_type == "audio_classify":
                self._handle_classifier_job(job)
            elif job_type in ("ambient_start", "ambient_reset"):
                ambient = self._new_stream_state(self.nemotron)
            elif job_type == "asr_audio" and job["mode"] == "ambient":
                emitted = self._feed_stream(ambient, job["audio"])
                self._publish_ambient_event_fragments(ambient, emitted)
            elif job_type == "ambient_event_timeout":
                emitted = self._drain_stream_pending(ambient)
                self._publish_ambient_event_fragments(
                    ambient,
                    emitted,
                    flush=True,
                )
            elif job_type == "ambient_flush":
                emitted = self._finish_stream(ambient)
                self._publish_ambient_event_fragments(
                    ambient,
                    emitted,
                    flush=True,
                )
                self._publish_ambient_text(
                    ambient["recognizer"].transcript,
                    wake_trigger=job.get("wake_trigger", False),
                    wake_context_id=job.get("wake_context_id"),
                )
                ambient = self._new_stream_state(self.nemotron)
                if job.get("prefetch_after"):
                    self._publish_cognition_prefetch()
            elif job_type == "human_start":
                human = self._new_stream_state(self.nemotron)
            elif job_type == "asr_audio" and job["mode"] == "human_stt":
                if human is None:
                    human = self._new_stream_state(self.nemotron)
                emitted = self._feed_stream(human, job["audio"]).strip()
                if emitted:
                    self._send_feedback(
                        header=emitted,
                        header_color="bright_green",
                        font="helvi",
                    )
            elif job_type == "human_cancel":
                human = None
            elif job_type == "human_finish":
                if human is None:
                    human = self._new_stream_state(self.nemotron)
                emitted = self._finish_stream(human).strip()
                if emitted:
                    self._send_feedback(
                        header=emitted,
                        header_color="bright_green",
                        font="helvi",
                    )
                self._publish_human_text(
                    human["recognizer"].transcript,
                    edit_mode=job.get("edit_mode", False),
                    stop_reason=job.get("stop_reason", "normal"),
                )
                human = None


if __name__ == "__main__":
    try:
        LogosNemotronEarsNode()
    except rospy.ROSInterruptException:
        pass
    except KeyboardInterrupt:
        pass
