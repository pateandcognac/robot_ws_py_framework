
# pip install google-genai
from google import genai  
import os
from google.genai import types
import requests
import PIL.Image

# get api key from os env
api_key = os.environ.get("FREE_GEMINI_API_KEY")
client = genai.Client(api_key=api_key) 

"""
# gemini sdk supports using bytes or PIL image directly. Convienient!
# Either of these two methods work:
with open('./test.png', 'rb') as f:
      image_bytes = f.read()

image = types.Part.from_bytes(
  data=image_bytes, mime_type="image/jpeg"
)      
"""
# or load image with PIL
image = PIL.Image.open("test.png")

prompt = ["Hi Gemini! This is just a smoke test of your API. Can you please briefly describe this image? TY!", image]

prompt_tokens = client.models.count_tokens(model="gemini-flash-latest", contents=prompt)
print(f"Total tokens in prompt: {prompt_tokens.total_tokens}")


"""
models:
gemini-2.5-pro
gemini-flash-latest
gemini-flash-lite-latest
gemini-robotics-er-1.5-preview

note: all of the above models support thinking, but, flash-lite and robotics-er do *not* support streaming thought summaries. 
"""

# Disable filtering for all categories
safety_settings = [
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
    types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=types.HarmBlockThreshold.BLOCK_NONE),
]

thoughts_started = False
answer_started = False
answer = ""

for chunk in client.models.generate_content_stream(
        model="gemini-flash-latest",
        contents=prompt,
        config=genai.types.GenerateContentConfig(
            safety_settings=safety_settings,
            thinking_config=genai.types.ThinkingConfig(
                thinking_budget=256), # 0 disables thinking, -1 for auto
                include_thoughts=True, #  whether to stream thought summaries
            temperature=0.7,
            media_resolution="MEDIA_RESOLUTION_UNSPECIFIED", # LOW, MEDIUM, or UNSPECIFIED
            stop_sequences=["<|EOS|>"],
            max_output_tokens=16384
        )
    ):
    for part in chunk.candidates[0].content.parts:
        if not part.text:
            continue
        if part.thought:
            if not thoughts_started:
                print("Thoughts:\n", end=' ')
                thoughts_started = True
            print(part.text, end='', flush=True)         # stream thought summary
        else:
            if not answer_started:
                print("\nAnswer:\n", end=' ')
                answer_started = True
            print(part.text, end='', flush=True)         # stream answer
            answer += part.text

print("\n\n--- Final Answer ---")
print(answer)

# response meta data
print("\n--- Response Metadata ---")
print(f"{chunk.usage_metadata}")
print("\n--- Token Usage ---")
print(f"Prompt Tokens: {chunk.usage_metadata.prompt_token_count}")
print(f"Thoughts Tokens: {chunk.usage_metadata.thoughts_token_count}")
print(f"Response Tokens: {chunk.usage_metadata.candidates_token_count}")
print(f"Total Tokens: {chunk.usage_metadata.total_token_count}")
if chunk.prompt_feedback:
    print("--- Prompt Feedback ---")
    print(f"Prompt Feedback: {chunk.prompt_feedback}")
    print(f"Prompt Feedback, block reason: {chunk.prompt_feedback.block_reason}")
