import os, json, requests, base64, time, logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.cloud import firestore
from google.cloud import secretmanager
import vertexai
from vertexai.generative_models import GenerativeModel

# ==========================================
#  MAGIC AI BACKEND v4 - VOICE FIX
# ==========================================

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

PROJECT_ID = "project-60c97356-c813-498b-988"
LOCATION = "us-central1"

@app.route("/", methods=["GET"])
def health():
    return "Magic AI Brain (Voice Fixed) Online", 200

@app.route("/turn", methods=["POST"])
def handle_turn():
    try:
        # --- 1. INGEST DATA ---
        data = request.json
        mode = data.get("mode", "coach")
        target_lang = data.get("target_lang", "Spanish")
        user_text = data.get("user_text", "")
        session_id = data.get("session_id", "session_demo_1")
        
        logger.info(f"Received turn: {mode} | {user_text[:20]}...")

        # --- 2. INIT CLOUD ---
        global OFFLINE_MODE
        try:
            db = firestore.Client()
            sm = secretmanager.SecretManagerServiceClient()
            vertexai.init(project=PROJECT_ID, location=LOCATION)
            OFFLINE_MODE = False
        except Exception as e:
            logger.warning(f"GCP Init Failed (Switching to Offline Mode): {e}")
            OFFLINE_MODE = True

        # --- 3. CONTEXT ---
        if not OFFLINE_MODE:
            collection_name = "coach_memory" if mode == "coach" else "translator_memory"
            history_context = ""
            try:
                docs = db.collection("sessions").document(session_id).collection(collection_name)\
                         .order_by("timestamp", direction=firestore.Query.DESCENDING).limit(6).stream()
                past_turns = [d.to_dict() for d in docs]
                past_turns.reverse()
                for t in past_turns:
                    ai_resp = t.get("ai_response", {})
                    if mode == "coach":
                        a_text = ai_resp.get("reply_target_language", "")
                    else:
                        a_text = ai_resp.get("translated_text", "")
                    history_context += f"User: {t.get('user_input','')}\nAI: {a_text}\n"
            except:
                history_context = ""
        else:
            history_context = "OFFLINE CONTEXT"

        # --- 4. PROMPT ENGINEERING ---
        if mode == "coach":
            voice_id = "21m00Tcm4TlvDq8ikWAM" # Rachel
        else:
            voice_id = "ErXwobaYiN019PkySvjV" # Antoni

        # --- 5. GENERATE CONTENT ---
        if not OFFLINE_MODE:
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
            
            model = GenerativeModel("gemini-2.0-flash-001", system_instruction=instr)
            response = model.generate_content(user_text)
            
            clean_json = response.text.strip().replace('```json', '').replace('```', '')
            try:
                llm = json.loads(clean_json)
            except:
                logger.error("JSON Parse Failed")
                llm = {"reply_target_language": "I'm having trouble thinking.", "translated_text": "Error."}
        else:
            # OFFLINE DUMMY RESPONSE
            logger.info("Generating Offline Response")
            dummy_text = f"I am in offline mode. I heard: {user_text}"
            llm = {
                "reply_target_language": dummy_text,
                "translated_text": dummy_text,
                "reply_user_language": "Offline Mode",
                "speak_segments": [{"text": dummy_text}]
            }

        # --- 6. AUDIO FIX (ELEVENLABS) ---
        speak_text = ""
        if "speak_segments" in llm and llm["speak_segments"]:
            speak_text = " ".join([s.get("text", "") for s in llm["speak_segments"]])
        
        if not speak_text:
            if mode == "coach":
                speak_text = llm.get("reply_target_language", "")
            else:
                speak_text = llm.get("translated_text", "")

        logger.info(f"Attempting to speak: {speak_text}")
        
        audio_b64 = ""
        if speak_text:
            try:
                # Retrieve Key
                eleven_key = ""
                if not OFFLINE_MODE:
                    try:
                        secret_path = f"projects/{PROJECT_ID}/secrets/ELEVENLABS_API_KEY/versions/latest"
                        eleven_key = sm.access_secret_version(request={"name": secret_path}).payload.data.decode("UTF-8")
                    except Exception as e:
                        logger.warning(f"Secret Manager failed: {e}")
                
                # FALLBACK 1: .env file
                if not eleven_key:
                    try:
                        if os.path.exists(".env"):
                            with open(".env", "r") as f:
                                for line in f:
                                    if line.startswith("ELEVENLABS_API_KEY="):
                                        eleven_key = line.split("=", 1)[1].strip()
                                        logger.info("Loaded ElevenLabs Key from .env")
                                        break
                    except Exception as e:
                        logger.warning(f"Failed to read .env: {e}")

                # FALLBACK 2: Hardcoded String
                if not eleven_key:
                    # REPLACE THIS STRING with your actual API key for testing
                    eleven_key = "YOUR_ELEVENLABS_API_KEY_HERE" 

                if eleven_key and eleven_key != "YOUR_ELEVENLABS_API_KEY_HERE":
                    v_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
                    v_headers = { "xi-api-key": eleven_key, "Content-Type": "application/json" }
                    v_payload = { "text": speak_text, "model_id": "eleven_multilingual_v2" }
                    
                    v_res = requests.post(v_url, json=v_payload, headers=v_headers)
                    if v_res.status_code == 200:
                        audio_b64 = base64.b64encode(v_res.content).decode('utf-8')
                        logger.info("ElevenLabs audio generated successfully.")
                    else:
                        logger.error(f"ElevenLabs API Error ({v_res.status_code}): {v_res.text}")
                else:
                    logger.warning("No valid ElevenLabs API key found (Secret Manager failed and no fallback provided).")
            except Exception as e:
                logger.error(f"Audio Logic Crash: {e}")

        # --- 7. RESPONSE ---
        display_text = llm.get("reply_target_language", "") if mode == "coach" else llm.get("translated_text", "")
        if mode == "coach" and llm.get("reply_user_language"):
            display_text += f"\n({llm.get('reply_user_language')})"

        # Async Save
        return jsonify({
            "reply_text": display_text,
            "audio_data": audio_b64,
            "detected": "Detected"
        })

    except Exception as e:
        logger.critical(f"SERVER CRASH: {e}")
        return jsonify({"error": str(e)}), 500

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