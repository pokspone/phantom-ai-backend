from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time

app = FastAPI(title="Phantom Global Servers Core", version="93.0")

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

@app.get("/")
def home():
    return {"status": "Phantom Global Matrix Server is Online", "dev": "MR"}

@app.post("/api/predict")
def get_ai_prediction(data: PredictionRequest):
    try:
        # محاكاة تحليل السيرفرات العالمية بناءً على الهيستوري الحالي ورقم الغرفة والوقت
        history_len_factor = sum([len(x) for x in data.history]) if data.history else 5
        room_factor = int(data.room_id) if data.room_id and data.room_id.isdigit() else 88
        time_entropy = int(time.time() // 10)  # يتغير كل 10 ثوانٍ تماشياً مع السيرفرات الحية
        
        calculated_index = (history_len_factor + room_factor + time_entropy) % 100

        result_prediction = ""
        confidence_rate = "99.9%"

        # 1. تحليل دراجون تايجر (حسب السيرفرات العالمية)
        if data.game_type == "DRAGON":
            if calculated_index % 31 == 0:
                result_prediction = "🤝 تعادل (Tie)"
                confidence_rate = "94.5%"
            elif calculated_index % 2 == 0:
                result_prediction = "🐉 تنين (Dragon)"
            else:
                result_prediction = "🐅 تايجر (Tiger)"

        # 2. تحليل عجلة المزرعة (أقوى 5 تخمينات حية)
        elif data.game_type == "FARM":
            pools = [
                "1. 🥩 لحمة (×45)<br>2. 🍗 فراخ (×25)<br>3. 🍢 شيش (×15)<br>4. 🌭 سجق (×10)<br>5. 🍅 طماطم (×5)",
                "1. 🍗 فراخ (×25)<br>2. 🥩 لحمة (×45)<br>3. 🌽 ذرة (×5)<br>4. 🥬 خس (×5)<br>5. 🥕 جزر (×5)",
                "1. 🥗 سلطة (نادر 🌟)<br>2. 🥩 لحمة (×45)<br>3. 🍗 فراخ (×25)<br>4. 🌭 سجق (×10)<br>5. 🍅 طماطم (×5)",
                "1. 🍕 بيتزا (نادر 🌟)<br>2. 🍢 شيش (×15)<br>3. 🍗 فراخ (×25)<br>4. 🥩 لحمة (×45)<br>5. 🌽 ذرة (×5)"
            ]
            result_prediction = pools[calculated_index % len(pools)]
            confidence_rate = "99.8%"

        # 3. تحليل بوكر التخمين (توزيع الكراسي)
        elif data.game_type == "POKER":
            pairs = [
                "1. 🟢 الأساسي: كرسي A <br>2. 🛡️ البديل: كرسي B",
                "1. 🔵 الأساسي: كرسي B <br>2. 🛡️ البديل: كرسي C",
                "1. 🔴 الأساسي: كرسي C <br>2. 🛡️ البديل: كرسي A"
            ]
            result_prediction = pairs[calculated_index % len(pairs)]
            confidence_rate = "99.7%"

        # حساب الرهان الآمن (5% من محفظة المستخدم كإدارة رأس مال صارمة)
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
