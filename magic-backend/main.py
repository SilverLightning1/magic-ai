
import os
import json
import base64
import logging
import requests
import datetime
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.cloud import firestore
from passlib.hash import pbkdf2_sha256

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment Loading
env_vars = {}
env_path = ".env"
if not os.path.exists(env_path):
    env_path = "../.env"

if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()

GEMINI_API_KEY = env_vars.get("GEMINI_API_KEY")
ELEVENLABS_API_KEY = env_vars.get("ELEVENLABS_API_KEY")

# Initialize Flask
app = Flask(__name__)
CORS(app)

# Initialize Firestore
db = None
try:
    # Assumes GOOGLE_APPLICATION_CREDENTIALS or default env
    db = firestore.Client()
    logger.info("Firestore Connected")
except Exception as e:
    logger.warning(f"Firestore Init Failed: {e}. Auth persistence disabled (Guest Mode Only).")

# Initialize Gemini
model = None
OFFLINE_MODE = False
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash-exp")
        logger.info("Connected to Gemini 2.0 Flash Exp.")
    except Exception as e:
        logger.error(f"Gemini Init Failed: {e}")
        OFFLINE_MODE = True
else:
    logger.warning("No GEMINI_API_KEY. Offline Mode.")
    OFFLINE_MODE = True

# --- HELPER FUNCTIONS ---

def fetch_user_context(user_id, limit=6):
    """Fetch last N interactions from Firestore for context."""
    if not db: return ""
    try:
        ref = db.collection("users").document(user_id).collection("conversations")
        query = ref.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
        docs = query.stream()
        history = []
        for d in docs:
            history.append(d.to_dict())
        history.reverse() # Chronological order
        
        context_str = "\n".join([f"User: {h.get('user_text','')}\nAI: {h.get('reply_text','')}" for h in history])
        return context_str
    except Exception as e:
        logger.error(f"Context Fetch Error: {e}")
        return ""

def save_turn(user_id, user_text, reply_text, mode):
    """Async-like save to Firestore."""
    if not db or user_id == "Guest": return
    try:
        ref = db.collection("users").document(user_id).collection("conversations")
        ref.add({
            "user_text": user_text,
            "reply_text": reply_text,
            "mode": mode,
            "timestamp": datetime.datetime.now(datetime.timezone.utc)
        })
    except Exception as e:
        logger.error(f"Save Turn Error: {e}")

# --- ROUTES ---

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "online",
        "brain": "offline" if OFFLINE_MODE else "online", 
        "db": "connected" if db else "disconnected"
    }), 200

@app.route("/auth/signup", methods=["POST"])
def signup():
    if not db: return jsonify({"error": "Database unavailable"}), 503
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    if not username or not password:
        return jsonify({"error": "Missing fields"}), 400
        
    user_ref = db.collection("users").document(username)
    if user_ref.get().exists:
        return jsonify({"error": "User exists"}), 409
        
    pwd_hash = pbkdf2_sha256.hash(password)
    user_ref.set({
        "password_hash": pwd_hash,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "settings": {"default_lang": "es-ES"}
    })
    
    return jsonify({"message": "User created", "user_id": username}), 201

@app.route("/auth/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    # Guest Bypass
    if username == "Guest":
         return jsonify({"user_id": "Guest", "status": "guest_mode"}), 200

    if not db: return jsonify({"error": "Database unavailable"}), 503
    
    user_ref = db.collection("users").document(username)
    doc = user_ref.get()
    
    if not doc.exists:
        return jsonify({"error": "Invalid credentials"}), 401
    
    user_data = doc.to_dict()
    if not pbkdf2_sha256.verify(password, user_data.get("password_hash")):
        return jsonify({"error": "Invalid credentials"}), 402

    return jsonify({
        "user_id": username,
        "status": "logged_in",
        "settings": user_data.get("settings", {})
    })

@app.route("/turn", methods=["POST"])
def turn():
    data = request.json
    user_text = data.get("user_text", "")
    mode = data.get("mode", "coach")
    user_id = data.get("user_id", "Guest")
    native_lang = data.get("native_lang", "en-US")
    msg_direction = data.get("msg_direction", "me_to_them")
    voice_persona = data.get("voice_persona", "rachel")

    logger.info(f"Turn: {mode} | User: {user_id} | Dir: {msg_direction} | Txt: {user_text}")

    # 1. CONTEXT
    history_context = ""
    target_lang = "Unspecified" 
    # In a real app we'd fetch target_lang from profile too, sticking to basics here
    
    if user_id != "Guest":
        history_context = fetch_user_context(user_id)

    # 2. PROMPT ENGINEERING
    json_schema = json.dumps({
        "reply_text": "str (The main spoken response)",
        "correction": "str or null (correction if user made a mistake in Coach mode)",
        "ui_state": "str (neutral, thinking, speaking)",
        "speak_segments": [{"text": "str (same as reply_text broken down)"}],
        "update_target_lang": "str or null (if user changed language)"
    })

    system_instr = ""
    if mode == "coach":
        system_instr = f"""Role: Polyglot Language Coach.
        Native Lang: {native_lang}.
        Task: Teach and correct the user.
        IMPORTANT: 
        1. BE DIRECT. Start teaching immediately. Do NOT ask about proficiency level (beginner/advanced). Do NOT use conversational filler ("Great request!", "Let's dive in").
        2. If user asks "How do I say X", give the phrase immediately, then explain briefly.
        3. Input is transcribed speech and may contain errors. Infer intent (e.g., "add me harder" -> "help me order").
        4. Explain in {native_lang}, then practice the target language.
        5. If user asks to "speak in all languages", respond with a single sentence greeting in: {native_lang}, French, Spanish, German, Mandarin, Hindi, and Japanese.
        6. ALWAYS end your response with a relevant simple follow-up question to keep the conversation going.
        Context: {history_context}
        Output JSON: {json_schema}
        """
    else:
        # Translator Mode (Optimized for Pure Speed)
        if msg_direction == "them_to_me":
             system_instr = f"""Role: Polyglot Interpreter.
             Mode: Foreign Language -> Native({native_lang}).
             Action: Translate immediately. Preserve perspective (I am -> I am).
             IMPORTANT: 
             1. Output ONLY the translated text. No conversational filler.
             2. If user says "Switch to [Lang]", set "update_target_lang".
             3. If input is clearly foreign text (not {native_lang}) and no target lang is set, infer "update_target_lang".
             Output JSON: {json_schema}
             """
        else:
            system_instr = f"""Role: Polyglot Interpreter.
            Mode: Native({native_lang}) -> Target Language.
            Action: Translate immediately. Preserve perspective.
            IMPORTANT:
            1. Output ONLY the translated text. No conversational filler.
            2. If user says "Switch to [Lang]" or "I'm speaking to [Lang]", set "update_target_lang".
            Output JSON: {json_schema}
            """

    # 3. GENERATION
    ai_response = {}
    if OFFLINE_MODE:
        ai_response = {
            "reply_text": f"[Offline] You said: {user_text}",
            "speak_segments": [{"text": "Offline mode."}],
            "ui_state": "neutral"
        }
    else:
        # FALLBACK STRATEGY
        # We try a list of models in order. If one fails with Quota (429), we try the next.
        fallback_models = [
            "gemini-2.0-flash-exp",
            "gemini-2.0-flash", 
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-pro-latest"
        ]
        
        success = False
        last_error = None

        prompt = f"{system_instr}\nUser Input: {user_text}"

        for model_name in fallback_models:
            try:
                # Configure specific model for this attempt
                active_model = genai.GenerativeModel(model_name)
                # Note: We don't use response_mime_type="application/json" for all models 
                # as some older/lite ones might be stricter, but pure text prompting usually works.
                # using response_mime_type if supported is better but let's trust the prompt for raw JSON 
                # to be safe across diverse models unless we are sure.
                # Actually, 1.5+ Flash supports it. Let's try to keep it simple.
                
                logger.info(f"Attempting GenAI with model: {model_name}")
                res = active_model.generate_content(prompt)
                
                # Parse JSON
                text_res = res.text.strip()
                if text_res.startswith("```json"): text_res = text_res[7:-3]
                if text_res.startswith("```"): text_res = text_res[3:-3]
                
                ai_response = json.loads(text_res)
                success = True
                break # Exit loop on success

            except Exception as e:
                last_error = e
                error_str = str(e)
                if "429" in error_str:
                    logger.warning(f"Quota Exceeded on {model_name}. Failing over...")
                    continue # Try next model
                else:
                    logger.error(f"GenAI Error ({model_name}): {e}")
                    # If it's not a quota error (e.g. safety, bad request), maybe we shouldn't retry?
                    # For stability, let's retry anyway unless it's a persistent issue.
                    continue

        if not success:
            logger.error(f"All models failed. Last error: {last_error}")
            ai_response = {
                "reply_text": "System overload. Please try again in a moment.",
                 "speak_segments": [{"text": "System overload."}],
                "ui_state": "error"
            }

    # 4. VOICE (ElevenLabs)
    audio_b64 = ""
    silent_mode = False
    speak_text = ai_response.get("reply_text", "")
    
    # Use speak_segments if available for cleaner text
    if "speak_segments" in ai_response and ai_response["speak_segments"]:
        speak_text = " ".join([s["text"] for s in ai_response["speak_segments"]])

    if speak_text and ELEVENLABS_API_KEY:
        try:
            # Voice Mapping
            voice_map = {
                "rachel": "21m00Tcm4TlvDq8ikWAM",
                "antoni": "ErXwobaYiN019PkySvjV",
                "bella": "EXAVITQu4vr4xnSDxMaL",
                "josh": "TxGEqnHWrfWFTfGW9XjX"
            }
            v_id = voice_map.get(voice_persona.lower(), "21m00Tcm4TlvDq8ikWAM")
            
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{v_id}"
            headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
            payload = {
                "text": speak_text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.75, "similarity_boost": 0.8} # High clarity settings
            }
            
            tts_res = requests.post(url, json=payload, headers=headers, timeout=5)
            if tts_res.status_code == 200:
                audio_b64 = base64.b64encode(tts_res.content).decode("utf-8")
            else:
                logger.warning(f"TTS Failed: {tts_res.status_code}")
                silent_mode = True
        except Exception as e:
             logger.error(f"TTS Exception: {e}")
             silent_mode = True
    else:
        silent_mode = True

    # 5. PERSISTENCE
    save_turn(user_id, user_text, ai_response.get("reply_text"), mode)

    # 6. RESPONSE
    return jsonify({
        "reply_text": ai_response.get("reply_text"),
        "audio_data": audio_b64,
        "mode": mode,
        "silent_mode": silent_mode,
        "lang_update": ai_response.get("update_target_lang"),
        # Compatibility fields
        "native_lang_update": None 
    })

# --- MORE ENDPOINTS ---
@app.route("/history", methods=["GET"])
def get_history():
    user_id = request.args.get("user_id")
    if not user_id or user_id == "Guest": return jsonify([])
    # Reuse context fetcher but return raw list?
    # Keeping it simple for now, return empty or implement if needed
    return jsonify([])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))