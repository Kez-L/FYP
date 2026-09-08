import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI()  # reads OPENAI_API_KEY

response = client.responses.create(
    model="gpt-4o-mini",
    input="hi, say hello to me",
)
print(response.output_text)
