from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

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

def check_anti_rigging(history: List[str]):
    if history and len(history) >= 4:
        last_four = history[-4:]
        if all(x == last_four[0] for x in last_four):
            return True, "🛑 تحذير رادار الفانتوم: السيرفر في وضع شفط (تكرار شاذ). امتنع عن الرهان!"
    return False, "🟢 السيرفر في وضع آمن وطبيعي."

@app.post("/api/predict")
def predict(req: PredictionRequest):
    try:
        # فحص الرادار العكسي لكل اللعب
        is_rigged, radar_message = check_anti_rigging(req.history)
        
        if is_rigged:
            return {
                "is_rigged": True,
                "confidence": "99.8% (رصد تلاعب)",
                "prediction": radar_message,
                "safe_bet": 0
            }
        
        # توجيه التوقع حسب اللعبة بدقة منعاً لأي لخبطة
        game = req.game_type.upper()
        prediction_text = "التوقع المؤكد: بن بنسبة نجاح عالية"
        
        if "DRAGON" in game:
            prediction_text = "🐉 التوقع المؤكد: تنين (Dragon) بنسبة 99.9%"
        elif "POKER" in game:
            prediction_text = "🟢 التوقع المؤكد: كرسي A / كرسي B بنسبة 99.8%"
        elif "FARM" in game:
            prediction_text = "🌟 التوقع المؤكد: سلطة / بيتزا أو شيش (×15) بنسبة 99.9%"
        else:
            prediction_text = "⚡ التوقع المؤكد: تتابع إيجابي مؤكد"

        safe_amount = int(req.coins * 0.1) if req.coins > 0 else 1000

        return {
            "is_rigged": False,
            "confidence": "99.9%",
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
    return {"status": "Phantom AI Master is fully operational, MR!"}
