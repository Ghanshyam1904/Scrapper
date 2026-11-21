from openai import OpenAI

c = OpenAI(base_url="http://localhost:3001/v1", api_key="freellmapi-4780d72023793d940d9373d18209f0911a91cf07e2f7eb5f", timeout=60)

models = [
    "gemini-flash", "gemini-2.5-flash",       # Google
    "gpt-oss-120b", "llama-3.3-70b",           # Groq
    "mistral-small-4", "mistral-large-3",      # Mistral
    "command-a", "command-r",                  # Cohere
    "nemotron-3-ultra-550b", "auto",
]

for m in models:
    try:
        r = c.chat.completions.create(
            model=m,
            messages=[{"role": "user", "content": "Say hi"}],
            max_tokens=20,
        )
        print(f"OK    {m}")
    except Exception as e:
        print(f"FAIL  {m}: {str(e)[:90]}")