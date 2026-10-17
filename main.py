from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Tuple
from collections import Counter
import os

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

# الأوزان الأساسية النظرية لعجلة المزرعة بناءً على قيمة المضاعفات
FARM_ITEMS_META: Dict[str, Dict] = {
    "🍅 طماطم (×5)": {"mult": 5, "base_weight": 19.0, "tier": "common"},
    "🌽 ذرة (×5)": {"mult": 5, "base_weight": 19.0, "tier": "common"},
    "🥬 خس (×5)": {"mult": 5, "base_weight": 19.0, "tier": "common"},
    "🥕 جزر (×5)": {"mult": 5, "base_weight": 19.0, "tier": "common"},
    "🌭 سجق (×10)": {"mult": 10, "base_weight": 9.5, "tier": "medium"},
    "🍢 شيش (×15)": {"mult": 15, "base_weight": 6.0, "tier": "medium"},
    "🍗 فراخ (×25)": {"mult": 25, "base_weight": 3.8, "tier": "high"},
    "🥩 لحمة (×45)": {"mult": 45, "base_weight": 2.1, "tier": "high"},
    "🥗 سلطة (نادر 🌟)": {"mult": 50, "base_weight": 1.3, "tier": "rare"},
    "🍕 بيتزا (نادر 🌟)": {"mult": 50, "base_weight": 1.3, "tier": "rare"},
}

def clean_history_item(raw_item: str) -> str:
    """تنظيف اسم العنصر من أي طوابع إضافية للمطابقة الدقيقة"""
    cleaned = raw_item.split(" [")[0].strip()
    return cleaned

def check_statistical_warnings(clean_hist: List[str]) -> Tuple[bool, str]:
    """فحص تكرار النتيجة 5 مرات متتالية أو وجود انحراف حاد في العينة"""
    if len(clean_hist) >= 5:
        last_five = clean_hist[-5:]
        if all(x == last_five[0] for x in last_five):
            return True, (
                f"⚠️ [تحذير إحصائي - تكرار 5 جولات]: العنصر ({last_five[0]}) تكرر 5 مرات متتالية. "
                f"موثوقية النمط الحالي منخفضة (تذبذب عالٍ)، يوصى بتقليل حجم الرهان."
            )
    return False, "🟢 التوزيع الإحصائي ضمن الحدود الطبيعية."

def analyze_farm_top5(clean_hist: List[str]) -> Tuple[str, str, str]:
    """حساب أقوى 5 توقعات لعجلة المزرعة بناءً على الأوزان والتكرار والفجوة الزمنية"""
    n = len(clean_hist)
    counts = Counter(clean_hist)
    scores: Dict[str, float] = {}

    for item, meta in FARM_ITEMS_META.items():
        base_prob = meta["base_weight"] / 100.0
        if n == 0:
            scores[item] = base_prob
            continue

        observed_count = counts.get(item, 0)

        if item in clean_hist:
            last_idx = len(clean_hist) - 1 - clean_hist[::-1].index(item)
            gap = len(clean_hist) - 1 - last_idx
        else:
            gap = n

        alpha = 5.0
        smoothed_prob = (observed_count + alpha * base_prob) / (n + alpha)

        expected_cycle = 1.0 / max(base_prob, 0.01)
        cycle_factor = 1.0 + min(0.35, (gap / expected_cycle) * 0.15)

        recent_penalty = 0.85 if (n >= 2 and clean_hist[-1] == item and clean_hist[-2] == item) else 1.0

        scores[item] = smoothed_prob * cycle_factor * recent_penalty

    total_score = sum(scores.values()) or 1.0
    normalized = {k: (v / total_score) * 100.0 for k, v in scores.items()}

    sorted_top5 = sorted(normalized.items(), key=lambda x: x[1], reverse=True)[:5]

    rank_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    lines = []
    top5_sum = 0.0
    for idx, (item_name, prob) in enumerate(sorted_top5):
        top5_sum += prob
        lines.append(f"{rank_emojis[idx]} {item_name} — احتمال {prob:.1f}%")

    if n >= 4:
        hits = 0
        trials = 0
        for i in range(2, n):
            sub_hist = clean_hist[:i]
            sub_counts = Counter(sub_hist)
            sub_scores = {
                k: ((sub_counts.get(k, 0) + 5.0 * (v["base_weight"] / 100.0)) / (len(sub_hist) + 5.0))
                for k, v in FARM_ITEMS_META.items()
            }
            sub_top5 = [k for k, _ in sorted(sub_scores.items(), key=lambda x: x[1], reverse=True)[:5]]
            if clean_hist[i] in sub_top5:
                hits += 1
            trials += 1
        measured_acc = (hits / trials) * 100.0 if trials > 0 else 0.0
        conf_str = f"{top5_sum:.1f}% (إصابة تاريخية: {measured_acc:.0f}%)"
    else:
        conf_str = f"{top5_sum:.1f}% (عينة: {n} جولات)"

    log_note = f"[FARM-STAT] Sample={n} | Top5_Coverage={top5_sum:.1f}% | Mode=Empirical-Bayes"
    return "<br>".join(lines), conf_str, log_note

def analyze_poker_dual(clean_hist: List[str]) -> Tuple[str, str, str]:
    """تحليل لعبة البوكر (3 كراسي A, B, C) لإخراج توقع أساسي + توقع بديل"""
    chairs = ["🟢 كرسي A", "🔵 كرسي B", "🔴 كرسي C"]
    norm_hist = []
    for h in clean_hist:
        if "A" in h:
            norm_hist.append("🟢 كرسي A")
        elif "B" in h:
            norm_hist.append("🔵 كرسي B")
        elif "C" in h:
            norm_hist.append("🔴 كرسي C")

    n = len(norm_hist)
    base_prob = 1.0 / 3.0

    if n == 0:
        return (
            "⭐ التوقع الأساسي: 🟢 كرسي A (33.3%)<br>🔄 التوقع البديل: 🔵 كرسي B (33.3%)<br>"
            "<span style='font-size:9px;color:#94a3b8;'>[لا يوجد سجل مدخل بعد - توزيع متساوٍ]</span>",
            "66.7% (تغطية مقعدين)",
            "[POKER-STAT] Sample=0 | Baseline=33.3% per chair"
        )

    counts = Counter(norm_hist)
    last_chair = norm_hist[-1]
    transition_counts = Counter()
    total_transitions = 0
    for i in range(n - 1):
        if norm_hist[i] == last_chair:
            transition_counts[norm_hist[i + 1]] += 1
            total_transitions += 1

    scores = {}
    for c in chairs:
        emp_freq = (counts.get(c, 0) + 2.0 * base_prob) / (n + 2.0)
        if total_transitions > 0:
            trans_prob = (transition_counts.get(c, 0) + 1.0 * base_prob) / (total_transitions + 1.0)
            scores[c] = 0.55 * trans_prob + 0.45 * emp_freq
        else:
            scores[c] = emp_freq

    total_s = sum(scores.values()) or 1.0
    probs = {c: (scores[c] / total_s) * 100.0 for c in chairs}
    ranked = sorted(probs.items(), key=lambda x: x[1], reverse=True)

    main_chair, main_p = ranked[0]
    alt_chair, alt_p = ranked[1]
    combined_p = main_p + alt_p

    pred_html = (
        f"⭐ التوقع الأساسي: {main_chair} ({main_p:.1f}%)<br>"
        f"🔄 التوقع البديل: {alt_chair} ({alt_p:.1f}%)<br>"
        f"<span style='font-size:9px;color:#93c5fd;'>[تغطية المقعدين إحصائياً: {combined_p:.1f}% | عينة: {n} جولات]</span>"
    )
    conf_str = f"{combined_p:.1f}% (أساسي + بديل)"
    log_note = f"[POKER-MARKOV] Sample={n} | Main={main_p:.1f}% | Alt={alt_p:.1f}% | Baseline=66.7%"
    return pred_html, conf_str, log_note

def analyze_dragon_tiger(clean_hist: List[str]) -> Tuple[str, str, str]:
    """تحليل Dragon & Tiger بناءً على السجل الفعلي وحساب احتمالات الانتقال والتوازن"""
    norm_hist = []
    for h in clean_hist:
        if "تنين" in h:
            norm_hist.append("🐉 تنين")
        elif "تايجر" in h:
            norm_hist.append("🐅 تايجر")
        elif "تعادل" in h:
            norm_hist.append("🤝 تعادل")

    n = len(norm_hist)
    if n == 0:
        return (
            "🐉 التوقع الإحصائي: تنين (Dragon) — 46.3% | 🐅 تايجر: 46.3%<br>"
            "<span style='font-size:9px;color:#94a3b8;'>[أدخل سجل الجولات السابقة لبدء التحليل الإحصائي]</span>",
            "46.3% (خط الأساس)",
            "[DT-STAT] Sample=0 | Baseline=46.3%"
        )

    counts = Counter(norm_hist)
    d_count = counts.get("🐉 تنين", 0)
    t_count = counts.get("🐅 تايجر", 0)
    tie_count = counts.get("🤝 تعادل", 0)

    switches = 0
    repeats = 0
    non_tie = [x for x in norm_hist if x != "🤝 تعادل"]
    for i in range(1, len(non_tie)):
        if non_tie[i] == non_tie[i - 1]:
            repeats += 1
        else:
            switches += 1

    last_non_tie = non_tie[-1] if non_tie else "🐉 تنين"
    opposite = "🐅 تايجر" if last_non_tie == "🐉 تنين" else "🐉 تنين"

    if switches > repeats:
        favored = opposite
        pattern_type = "نمط تبادلي (Chop)"
    elif repeats > switches:
        favored = last_non_tie
        pattern_type = "نمط متسلسل (Streak)"
    else:
        favored = "🐅 تايجر" if d_count > t_count else "🐉 تنين"
        pattern_type = "نمط متوازن"

    total_decisive = max(1, d_count + t_count)
    imbalance = abs(d_count - t_count) / total_decisive
    est_prob = min(68.0, 50.0 + (imbalance * 12.0) + (abs(switches - repeats) * 1.5))

    pred_html = (
        f"⚡ التوقع الإحصائي الأرجح: {favored} ({est_prob:.1f}%)<br>"
        f"<span style='font-size:9px;color:#93c5fd;'>[الحالة: {pattern_type} | سجل: تنين({d_count}) - تايجر({t_count}) - تعادل({tie_count})]</span>"
    )
    conf_str = f"{est_prob:.1f}% (عينة: {n})"
    log_note = f"[DT-STAT] Sample={n} | D={d_count},T={t_count},Tie={tie_count} | Mode={pattern_type}"
    return pred_html, conf_str, log_note

@app.post("/api/predict")
def predict(req: PredictionRequest):
    try:
        game = req.game_type.upper().strip()
        clean_hist = [clean_history_item(h) for h in req.history if h and h.strip()]

        is_warning, warning_msg = check_statistical_warnings(clean_hist)

        if "FARM" in game:
            pred_html, conf_str, log_note = analyze_farm_top5(clean_hist)
        elif "POKER" in game:
            pred_html, conf_str, log_note = analyze_poker_dual(clean_hist)
        elif "DRAGON" in game:
            pred_html, conf_str, log_note = analyze_dragon_tiger(clean_hist)
        else:
            pred_html, conf_str, log_note = "يرجى اختيار لعبة صالحة.", "--%", "[ERROR] Unknown game"

        if is_warning:
            pred_html = f"<span style='color:#fca5a5;'>{warning_msg}</span><br><hr style='border-color:#ef4444;margin:4px 0;'>{pred_html}"
            safe_amount = int(req.coins * 0.03) if req.coins > 0 else 0
        else:
            safe_amount = int(req.coins * 0.08) if req.coins > 0 else 500

        return {
            "is_rigged": is_warning,
            "confidence": conf_str,
            "prediction": pred_html,
            "safe_bet": safe_amount,
            "engine_log": log_note,
            "sample_size": len(clean_hist)
        }

    except Exception as e:
        return {
            "is_rigged": False,
            "confidence": "خطأ في المعالجة",
            "prediction": f"⚠️ تعذر إكمال الحساب الإحصائي: {str(e)}",
            "safe_bet": 0,
            "engine_log": "[ERROR] Exception in prediction endpoint",
            "sample_size": 0
        }

@app.get("/")
def root():
    return {
        "status": "Phantom Statistical Engine v117.1 Active",
        "data_source": "User-Recorded Round History (Empirical Bayes & Markov Analysis)",
        "developer": "MR"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
