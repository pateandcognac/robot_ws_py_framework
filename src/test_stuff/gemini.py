from google import genai

client = genai.Client()

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="What is your name and operating parameters listed above?",
)

print(response)
print("\nFull response:", response.text)