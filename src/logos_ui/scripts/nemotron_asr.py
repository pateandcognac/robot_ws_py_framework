"""Small reusable wrapper around ONNX Runtime GenAI Nemotron streaming ASR."""

import json
import re

import numpy as np


LANGUAGE_ID = 0


def strip_language_tags(text):
    return re.sub(r"<[a-z]{2}(?:-[A-Z]{2})?>", "", text)


def read_model_config(model_path):
    config_path = f"{model_path}/genai_config.json"
    with open(config_path, encoding="utf-8") as config_file:
        config = json.load(config_file)
    model = config["model"]
    return {
        "sample_rate": int(model["sample_rate"]),
        "chunk_samples": int(model["chunk_samples"]),
    }


class LanguageTagStripper:
    """Strip language tags even when the tokenizer emits them in pieces."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        output = []
        for character in text:
            if self.pending:
                self.pending += character
                if character == ">":
                    if not re.fullmatch(
                        r"<[a-z]{2}(?:-[A-Z]{2})?>", self.pending
                    ):
                        output.append(self.pending)
                    self.pending = ""
                elif len(self.pending) > 8:
                    output.append(self.pending)
                    self.pending = ""
            elif character == "<":
                self.pending = character
            else:
                output.append(character)
        return "".join(output)


def load_vocabulary(path):
    if not path:
        return []

    with open(path, encoding="utf-8") as vocab_file:
        data = json.load(vocab_file)

    entries = data.get("entries", data)
    if not isinstance(entries, list):
        raise ValueError("Custom vocabulary JSON must contain an 'entries' list")

    vocabulary = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Vocabulary entry {index} must be an object")
        canonical = entry.get("canonical")
        aliases = entry.get("aliases", [])
        if not isinstance(canonical, str):
            raise ValueError(
                f"Vocabulary entry {index} needs a 'canonical' string"
            )
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias for alias in aliases
        ):
            raise ValueError(
                f"Vocabulary entry {index} needs a list of non-empty aliases"
            )
        vocabulary.append((canonical, aliases))
    return vocabulary


def normalize_vocabulary(text, vocabulary):
    normalized = strip_language_tags(text)
    for canonical, aliases in vocabulary:
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = rf"(?<![\w-]){re.escape(alias)}(?![\w-])"
            normalized = re.sub(
                pattern,
                lambda match, replacement=canonical: replacement,
                normalized,
                flags=re.IGNORECASE,
            )
    return re.sub(r"\s+", " ", normalized).strip()


class NemotronModel:
    """Load the expensive model once and create resettable stream sessions."""

    def __init__(self, model_path, threads=1, use_vad=False):
        try:
            import onnxruntime_genai as og
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime-genai is not installed. See "
                "docs/nemotron_asr_poc_notes.md."
            ) from exc

        config = og.Config(model_path)
        config.clear_providers()
        if threads:
            session_options = {
                "intra_op_num_threads": threads,
                "inter_op_num_threads": 1,
            }
            config.overlay(
                json.dumps(
                    {
                        "model": {
                            component: {"session_options": session_options}
                            for component in ("encoder", "decoder", "joiner", "vad")
                        }
                    }
                )
            )

        self.og = og
        self.model = og.Model(config)
        self.tokenizer = og.Tokenizer(self.model)
        self.use_vad = use_vad

    def create_stream(self):
        return NemotronStream(self)


class NemotronStream:
    def __init__(self, loaded_model):
        og = loaded_model.og
        self.processor = og.StreamingProcessor(loaded_model.model)
        self.processor.set_option(
            "use_vad", "true" if loaded_model.use_vad else "false"
        )
        self.tokenizer_stream = loaded_model.tokenizer.create_stream()
        self.language_tag_stripper = LanguageTagStripper()
        params = og.GeneratorParams(loaded_model.model)
        self.generator = og.Generator(loaded_model.model, params)
        self.generator.set_runtime_option("lang_id", str(LANGUAGE_ID))
        self.transcript = ""

    def _decode_available(self):
        text = ""
        while not self.generator.is_done():
            self.generator.generate_next_token()
            tokens = self.generator.get_next_tokens()
            if len(tokens) > 0:
                piece = self.language_tag_stripper.feed(
                    self.tokenizer_stream.decode(tokens[0])
                )
                text += piece
        self.transcript += text
        return text

    def process(self, chunk):
        inputs = self.processor.process(
            np.asarray(chunk, dtype=np.float32)
        )
        if inputs is None:
            return ""
        self.generator.set_inputs(inputs)
        return self._decode_available()

    def flush(self):
        inputs = self.processor.flush()
        if inputs is None:
            return ""
        self.generator.set_inputs(inputs)
        return self._decode_available()
