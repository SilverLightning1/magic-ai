import google.generativeai as genai
import os

# Load .env manually
env_vars = {}
if os.path.exists(".env"):
    with open(".env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

api_key = env_vars.get("GEMINI_API_KEY")
genai.configure(api_key=api_key)

candidates = [
    "gemini-flash-latest",
    "gemini-pro-latest",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash-002",
    "gemini-1.5-pro-002"
]

print(f"Testing Key: {api_key[:5]}...")

for m in candidates:
    print(f"\n--- Testing {m} ---")
    try:
        model = genai.GenerativeModel(m)
        res = model.generate_content("Hello")
        print(f"SUCCESS! Output: {res.text}")
    except Exception as e:
        print(f"FAIL: {e}")
