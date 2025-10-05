from __future__ import annotations

import os
from typing import List

import PIL.Image
import requests  # noqa: F401  # kept in case you switch to URL fetch
from google import genai
from google.genai import types


def main() -> None:
    # 1) Auth
    api_key = os.environ.get("FREE_GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("FREE_GEMINI_API_KEY not set in environment.")
    client = genai.Client(api_key=api_key)

    # 2) Inputs (PIL or bytes are both supported)
    image = PIL.Image.open("test.png")
    prompt: List[types.Content] = [
        "Hi Gemini! This is just a smoke test of your API. "
        "Can you briefly describe this image?",
        image,
    ]

    # (Optional) token count
    prompt_tokens = client.models.count_tokens(
        model="gemini-flash-latest",
        contents=prompt,
    )
    print(f"Total tokens in prompt: {prompt_tokens.total_tokens}")

    # 3) Safety settings (disable filtering entirely)
    safety_settings = [
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
    ]

    # 4) Stream the response (with ThinkingConfig set correctly)
    thoughts_started = False
    answer_started = False
    answer_parts: List[str] = []

    stream = client.models.generate_content_stream(
        model="gemini-flash-latest",
        contents=prompt,
        config=types.GenerateContentConfig(
            safety_settings=safety_settings,
            thinking_config=types.ThinkingConfig(
                # 0 disables private thinking; -1 enables dynamic thinking; +int sets max tokens
                thinking_budget=128,
                # streams high-level summaries of private thoughts for feedback
                include_thoughts=True,
            ),
            temperature=0.7,
            media_resolution="MEDIA_RESOLUTION_UNSPECIFIED", # LOW, MEDIUM, or UNSPECIFIED
            stop_sequences=["<|EOS|>"],
            max_output_tokens=16384,
        ),
    )

    for chunk in stream:
        for part in chunk.candidates[0].content.parts:
            if not part.text:
                continue
            if getattr(part, "thought", False):
                if not thoughts_started:
                    print("Thoughts:\n", end=" ")
                    thoughts_started = True
                print(part.text, end="", flush=True)
            else:
                if not answer_started:
                    print("\nAnswer:\n", end=" ")
                    answer_started = True
                print(part.text, end="", flush=True)
                answer_parts.append(part.text)

    print("\n\n--- Final Answer ---")
    final_answer = "".join(answer_parts)
    print(final_answer)

    # Response metadata from the LAST chunk
    print("\n--- Response Metadata ---")
    print(chunk.usage_metadata)
    print("\n--- Token Usage ---")
    print(f"Prompt Tokens: {chunk.usage_metadata.prompt_token_count}")
    # Some models return thoughts_token_count only when thinking is on
    print(f"Thoughts Tokens: {getattr(chunk.usage_metadata, 'thoughts_token_count', 'N/A')}")
    print(f"Response Tokens: {chunk.usage_metadata.candidates_token_count}")
    print(f"Total Tokens: {chunk.usage_metadata.total_token_count}")
    if chunk.prompt_feedback:
        print("--- Prompt Feedback ---")
        print(f"Prompt Feedback: {chunk.prompt_feedback}")
        print(f"Prompt Feedback, block reason: {chunk.prompt_feedback.block_reason}")


if __name__ == "__main__":
    main()
