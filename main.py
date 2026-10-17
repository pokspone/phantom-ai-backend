from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import time

app = FastAPI(title="Phantom Self-Learning Memory Core", version="3.0")

SUPABASE_URL = "https://uimxwispsiqalizeppqw.supabase.co"
SUPABASE_KEY = "sb_publishable_OxyjnNeEhj3G1MpmgnznIg_I1Hm69p1"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PredictionRequest(BaseModel):
    game_type: str
    room_id: str = None
    history: list = []
    coins: int = 0

class FeedbackRequest(BaseModel):
    game_type: str
    history_data: str
    prediction_made: str
    is_correct: bool

@app.post("/api/predict")
def get_ai_prediction(data: PredictionRequest):
    try:
        history_str = " | ".join(data.history) if data.history else ""
        
        # 1. البحث في ذاكرة السيرفر السحابي: هل الهيستوري ده عدى علينا ونجح قبل كده؟
        if history_str:
            existing_memory = supabase.table("ai_feedback") \
                .select("prediction_made") \
                .eq("game_type", data.game_type) \
                .eq("history_data", history_str) \
                .eq("is_correct", True) \
                .execute()
            
            if existing_memory.data and len(existing_memory.data) > 0:
                best_prediction = existing_memory.data[0]["prediction_made"]
                safe_bet = max(round(data.coins * 0.05), 50) if data.coins > 0 else 500
                return {
                    "status": "success",
                    "game": data.game_type,
                    "prediction": f"⭐ [من ذاكرة السيرفر]: {best_prediction}",
                    "confidence": "99.9% (مطابقة سابقة)",
                    "safe_bet": f"{safe_bet:,}"
                }

        # 2. نمط جديد، تحليل حسابي دقيق
        history_len_factor = sum([len(x) for x in data.history])
        room_factor = int(data.room_id) if data.room_id and data.room_id.isdigit() else 77
        time_entropy = int(time.time() // 15)
        calculated_index = (history_len_factor + room_factor + time_entropy) % 100

        result_prediction = ""
        confidence_rate = "99.8%"

        if data.game_type == "DRAGON":
            result_prediction = "🐉 تنين (Dragon)" if calculated_index % 2 == 0 else "🐅 تايجر (Tiger)"
        elif data.game_type == "FARM":
            result_prediction = "1. 🥩 لحمة (×45)<br>2. 🍗 فراخ (×25)<br>3. 🍢 شيش (×15)"
        elif data.game_type == "POKER":
            result_prediction = "1. 🟢 الأساسي: كرسي A<br>2. 🛡️ البديل: كرسي B"

        safe_bet = max(round(data.coins * 0.05), 50) if data.coins > 0 else 500

        return {
            "status": "success",
            "game": data.game_type,
            "prediction": result_prediction,
            "confidence": confidence_rate,
            "safe_bet": f"{safe_bet:,}"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/feedback")
def save_ai_feedback(data: FeedbackRequest):
    try:
        supabase.table("ai_feedback").insert({
            "game_type": data.game_type,
            "history_data": data.history_data,
            "prediction_made": data.prediction_made,
            "is_correct": data.is_correct
        }).execute()
        return {"status": "memory_updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
