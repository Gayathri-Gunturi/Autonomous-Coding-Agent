
import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("Gemini API key not found in .env")
else:
    client = genai.Client(api_key=api_key)

    print("Available Gemini models:")

    for model in client.models.list():
        print(model.name)
