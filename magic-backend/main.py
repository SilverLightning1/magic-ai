
import os
import json
import base64
import logging
import requests
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_cors import CORS, cross_origin

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Manual .env parsing
env_vars = {}
if os.path.exists(".env"):
    with open(".env", "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

GEMINI_API_KEY = env_vars.get("GEMINI_API_KEY")
ELEVENLABS_API_KEY = env_vars.get("ELEVENLABS_API_KEY")

# Initialize Flask
app = Flask(__name__)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'

# --- 1. SETUP AI BRAIN (GOOGLE GEN AI) ---
OFFLINE_MODE = False
model = None

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-3-flash-preview")
        logger.info("Connected to Gemini via API Key.")
    except Exception as e:
        logger.error(f"Gemini Init Failed: {e}")
        OFFLINE_MODE = True
else:
    logger.warning("GEMINI_API_KEY not found in .env. Switching to OFFLINE MODE.")
    OFFLINE_MODE = True

# ==========================================
#  MAGIC AI BACKEND v4 - VOICE FIX
# ==========================================

@app.route("/", methods=["GET"])
def health():
    return "Magic AI Brain (Voice Fixed) Online", 200

@app.route("/turn", methods=["POST"])
def handle_turn():
    data = request.json
    user_text = data.get("user_text", "")
    mode = data.get("mode", "coach")
    user_id = data.get("user_id", "Guest")
    target_lang = data.get("target_lang", "Spanish")

    logger.info(f"Received turn: {mode} | {user_text}")

    # --- 1. CONTEXT MANAGEMENT (Local Mock) ---
    history_file = f"history_{user_id}.json"
    history = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f: history = json.load(f)
        except: pass
    
    # Simple Context Window (Last 5 turns)
    recent_history = history[-5:]
    history_context = "\n".join([f"User: {h['user_text']}\nAI: {h['ai_text']}" for h in recent_history])

    # --- 2. PROMPT ENGINEERING & VOICE PERSONA ---
    voice_persona = data.get("voice_persona", "rachel" if mode == "coach" else "antoni")
    
    # Voice ID Mapping (ElevenLabs)
    voice_map = {
        "rachel": "21m00Tcm4TlvDq8ikWAM",  # The Tutor (Coach Default)
        "antoni": "ErXwobaYiN019PkySvjV",  # The Professional (Translator Default)
        "bella": "EXAVITQu4vr4xnSDxMaL",   # The Peer (Casual Female)
        "josh": "TxGEqnHWrfWFTfGW9XjX"     # The Peer (Casual Male)
    }
    voice_id = voice_map.get(voice_persona.lower(), "21m00Tcm4TlvDq8ikWAM")

    instr = ""
    if mode == "coach":
        instr = f"""You are MAGIC (Coach). Target: {target_lang}.
        CONTEXT: {history_context}
        
        BEHAVIORAL PROTOCOL (The "Socratic Loop"):
        1. VALIDATE: Acknowledge the user's attempt positively.
        2. CORRECT: If there is an error, explain it gently. If no error, praise.
        3. PROMPT: Ask a follow-up question to keep the conversation flowing.
        
        ADDITIONAL RULES:
        - If the user switches language, translate their thought back to {target_lang} and guide them.
        - Ignore filler words (um, uh).
        
        OUTPUT JSON:
        {{
            "reply_target_language": "string",
            "reply_user_language": "string",
            "corrections": [{{"corrected":"string"}}],
            "speak_segments": [{{"text":"string"}}]
        }}
        """
    else:
        instr = f"""You are MAGIC (Translator).
        GOAL: Detect the language of the INPUT text automatically.
        ACTION: Translate the input text into {target_lang}.
        
        BEHAVIORAL PROTOCOL (The "Ghost" Protocol):
        - Remove your personality.
        - Preserve the FIRST-PERSON perspective (e.g., "I am hungry" -> "Tengo hambre", NOT "He says he is hungry").
        
        CULTURAL SAFETY:
        - If a translation is grammatically correct but culturally offensive (e.g. wrong formality), WARN the user in the output text processing.
        
        OUTPUT JSON:
        {{
            "translated_text": "string",
            "speak_segments": [{{"text":"string"}}]
        }}
        """

    # --- 3. GENERATE CONTENT ---
    ai_text = ""
    speak_text = ""
    
    if not OFFLINE_MODE and model:
        try:
            # Google Gen AI Call
            prompt = f"{instr}\nUSER INPUT: {user_text}"
            response = model.generate_content(prompt)
            
            try:
                # Clean JSON
                text_res = response.text.strip()
                if text_res.startswith("```json"): text_res = text_res[7:-3]
                
                res_json = json.loads(text_res)
                
                if mode == "coach":
                    speak_text = " ".join([s["text"] for s in res_json.get("speak_segments", [])])
                    ai_text = speak_text 
                else:
                    speak_text = res_json.get("translated_text", "")
                    ai_text = speak_text

            except Exception as e:
                logger.error(f"JSON Parse Error: {e}")
                ai_text = response.text
                speak_text = response.text

        except Exception as e:
            logger.error(f"GenAI Error: {e}")
            ai_text = f"Brain Error: {e}"
            speak_text = "System error."
    
    else:
        # Offline Fallback
        logger.info("Generating Offline Response")
        ai_text = f"I am in offline mode. I heard: {user_text}"
        speak_text = ai_text

    # --- 4. TEXT TO SPEECH (ELEVENLABS) ---
    logger.info(f"Attempting to speak: {speak_text}")
    audio_b64 = ""
    
    if speak_text and ELEVENLABS_API_KEY and ELEVENLABS_API_KEY != "YOUR_ELEVENLABS_API_KEY_HERE":
        try:
            v_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            v_headers = { "xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json" }
            v_payload = { "text": speak_text, "model_id": "eleven_multilingual_v2" }
            
            v_res = requests.post(v_url, json=v_payload, headers=v_headers)
            if v_res.status_code == 200:
                audio_b64 = base64.b64encode(v_res.content).decode('utf-8')
                logger.info("ElevenLabs audio generated successfully.")
            else:
                logger.error(f"ElevenLabs API Error ({v_res.status_code}): {v_res.text}")
        except Exception as e:
            logger.error(f"Audio Logic Crash: {e}")

    # Save History
    history.append({"user_text": user_text, "ai_text": ai_text})
    try:
        with open(history_file, "w") as f: json.dump(history, f)
    except: pass

    return jsonify({
        "reply_text": ai_text,
        "audio_data": audio_b64,
        "mode": mode
    })

# --- USER & ACCOUNT ENDPOINTS ---

@app.route("/register", methods=["POST"])
def register():
    # In a real app, use Firestore/Auth. For this demo, we use a local JSON.
    data = request.json
    username = data.get("username")
    # Mock success
    return jsonify({"user_id": username, "status": "created"})

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    # Mock success - just return the username as ID
    return jsonify({"user_id": username, "status": "logged_in", "default_lang": "Spanish"})

@app.route("/history", methods=["GET"])
def get_history():
    user_id = request.args.get("user_id")
    if not user_id or user_id == "guest":
        return jsonify([])
    
    # Try generic history file
    try:
        fname = f"history_{user_id}.json"
        if os.path.exists(fname):
            with open(fname, "r") as f:
                return jsonify(json.load(f))
    except:
        pass
    return jsonify([])

@app.route("/recommendations", methods=["GET"])
def get_recommendations():
    user_id = request.args.get("user_id")
    # Mock Recommendations based on nothing, dynamic
    recs = [
        "Review: Past Tense Conjugation",
        "Practice: Ordering Coffee in Paris",
        "Daily Challenge: Describe your morning"
    ]
    return jsonify(recs)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))