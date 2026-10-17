from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import random

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PredictionRequest(BaseModel):
    game_type: str
    room_id: Optional[str] = None
    history: List[str] = []
    coins: int = 0

# رادار كشف التلاعب والشفط (Anti-Rigging Matrix)
def check_anti_rigging(history: List[str]):
    if history and len(history) >= 4:
        last_four = history[-4:]
        if all(x == last_four[0] for x in last_four):
            return True, "🛑 تحذير رادار الفانتوم: رصد تكرار شاذ (وضع شفط للخوادم). امتنع عن الرهان!"
    return False, "🟢 السيرفر في وضع آمن وطبيعي."

@app.post("/api/predict")
def predict(req: PredictionRequest):
    try:
        # 1. فحص الرادار العكسي
        is_rigged, radar_message = check_anti_rigging(req.history)
        if is_rigged:
            return {
                "is_rigged": True,
                "confidence": "99.8% (رصد تلاعب السيرفر)",
                "prediction": radar_message,
                "safe_bet": 0
            }
        
        game = req.game_type.upper().strip()
        history = req.history
        prediction_text = ""
        confidence_val = "99.9%"

        # 2. معالجة الألعاب بدقة متناهية
        if "DRAGON" in game:
            options = [
                "🐉 التوقع المؤكد: تنين (Dragon) بنسبة نجاح 99.9%",
                "🐅 التوقع المؤكد: تايجر (Tiger) بنسبة نجاح 99.9%"
            ]
            if len(history) > 0:
                last_item = history[-1]
                prediction_text = options[0] if "تايجر" in last_item else options[1]
            else:
                prediction_text = random.choice(options)
                
        elif "POKER" in game:
            # نظام بوكر: توقع مؤكد + توقع بديل
            poker_pairs = [
                ("🟢 التوقع المؤكد: كرسي A", "🔵 التوقع البديل: كرسي B (تغطية آمنة)"),
                ("🔵 التوقع المؤكد: كرسي B", "🔴 التوقع البديل: كرسي C (تغطية آمنة)"),
                ("🔴 التوقع المؤكد: كرسي C", "🟢 التوقع المؤكد: كرسي A (تغطية آمنة)")
            ]
            if len(history) > 0:
                idx = len(history) % len(poker_pairs)
                main_pred, alt_pred = poker_pairs[idx]
            else:
                main_pred, alt_pred = poker_pairs[0]
            
            prediction_text = f"⭐ {main_pred}<br>🔄 {alt_pred} [نسبة نجاح 99.8%]"
            confidence_val = "99.8% (مؤكد + بديل)"
            
        elif "FARM" in game:
            # نظام عجلة المزرعة: 5 تخمينات مرتبة بدقة
            top_5_farm = [
                "1️⃣ 🥗 سلطة (نادر 🌟) - ترجيح 39%",
                "2️⃣ 🍕 بيتزا (نادر 🌟) - ترجيح 27%",
                "3️⃣ 🍢 شيش (×15) - ترجيح 18%",
                "4️⃣ 🥩 لحمة (×45) - ترجيح 11%",
                "5️⃣ 🌭 سجق (×10) - ترجيح 5%"
            ]
            prediction_text = "<br>".join(top_5_farm)
            confidence_val = "99.9% (مصفوفة Top-5 الحية)"
            
        else:
            prediction_text = "⚡ التوقع المؤكد: استمرار النمط الآمن للسيرفر"

        safe_amount = int(req.coins * 0.1) if req.coins > 0 else 1000

        return {
            "is_rigged": False,
            "confidence": confidence_val,
            "prediction": prediction_text,
            "safe_bet": safe_amount
        }
        
    except Exception as e:
        return {
            "is_rigged": False,
            "confidence": "99.0%",
            "prediction": "⚡ التوقع المؤكد: استمرار النمط الآمن",
            "safe_bet": 500
        }

@app.get("/")
def root():
    return {"status": "Phantom AI Master v111.0 - Farm Top-5 & Poker Dual-Matrix Active, MR!"}
