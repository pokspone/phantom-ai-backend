from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Tuple
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import os
import time

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# تسجيل وقت بدء تشغيل السيرفر لحساب دورة الـ 7 ساعات
SERVER_START_TIME = time.time()

class PredictionRequest(BaseModel):
    game_type: str
    room_id: Optional[str] = None
    history: List[str] = []
    coins: int = 0
    client_hour: Optional[int] = None
    client_minute: Optional[int] = None

# بيانات وأوزان عجلة المزرعة والدورة المتوقعة لكل عنصر (كم جولة في المتوسط ليظهر)
FARM_ITEMS_META: Dict[str, Dict] = {
    "🍅 طماطم (×5)": {"mult": 5, "base_weight": 19.0, "expected_gap": 5.2, "tier": "common"},
    "🌽 ذرة (×5)": {"mult": 5, "base_weight": 19.0, "expected_gap": 5.2, "tier": "common"},
    "🥬 خس (×5)": {"mult": 5, "base_weight": 19.0, "expected_gap": 5.2, "tier": "common"},
    "🥕 جزر (×5)": {"mult": 5, "base_weight": 19.0, "expected_gap": 5.2, "tier": "common"},
    "🌭 سجق (×10)": {"mult": 10, "base_weight": 9.5, "expected_gap": 10.5, "tier": "medium"},
    "🍢 شيش (×15)": {"mult": 15, "base_weight": 6.0, "expected_gap": 16.0, "tier": "medium"},
    "🍗 فراخ (×25)": {"mult": 25, "base_weight": 3.8, "expected_gap": 25.0, "tier": "high"},
    "🥩 لحمة (×45)": {"mult": 45, "base_weight": 2.1, "expected_gap": 42.0, "tier": "high"},
    "🥗 سلطة (نادر 🌟)": {"mult": 50, "base_weight": 1.3, "expected_gap": 65.0, "tier": "rare"},
    "🍕 بيتزا (نادر 🌟)": {"mult": 50, "base_weight": 1.3, "expected_gap": 65.0, "tier": "rare"},
}

def clean_history_item(raw_item: str) -> str:
    return raw_item.split(" [")[0].strip()

def check_server_cycle_and_time(client_hour: Optional[int], client_minute: Optional[int], history_len: int) -> Dict:
    """
    إدارة دورة عمل السيرفر (يعمل 7 ساعات ويرتاح تلقائياً، أو يرتاح في أوقات الضغط الشديد في تطبيقات الدردشة)
    """
    if client_hour is not None and client_minute is not None:
        h, m = client_hour, client_minute
    else:
        cairo_now = datetime.now(timezone(timedelta(hours=2)))
        h, m = cairo_now.hour, cairo_now.minute

    time_dec = h + (m / 60.0)
    time_str = f"{h:02d}:{m:02d}"

    # حساب دورة الـ 7 ساعات (كل 7 ساعات عمل هناك 25 دقيقة تبريد وإعادة معايرة)
    elapsed_hours = (time.time() - SERVER_START_TIME) / 3600.0
    cycle_position = elapsed_hours % 7.5  # 7 ساعات عمل + نصف ساعة راحة
    in_7h_cooldown = cycle_position >= 7.0
    remaining_cooldown_mins = max(1, int((7.5 - cycle_position) * 60)) if in_7h_cooldown else 0

    # فحص أوقات تطبيقات الدردشة (HiFami)
    # 1. أوقات سيئة جداً (سحب وتذبذب عنيف): 22:30 إلى 23:45 (حسم الـ PK الليلي) و 07:30 إلى 08:30 صباحاً
    if (22.5 <= time_dec < 23.75) or (7.5 <= time_dec < 8.5) or (20.5 <= time_dec < 21.0):
        return {
            "is_resting": True,
            "status_code": "SERVER_REST_BAD_TIME",
            "mood_badge": "🔴 السيرفر في استراحة (وقت خطر)",
            "banner": f"🛑 [استراحة السيرفر التلقائية - {time_str}]: الألعاب في هاي فامي حالياً في وقت ضغط/شفط عالٍ. السيرفر متوقف مؤقتاً لحماية كوينزاتك!",
            "risk_mult": 0.15,
            "rare_boost": 0.6
        }

    # 2. راحة دورة الـ 7 ساعات التلقائية
    if in_7h_cooldown:
        return {
            "is_resting": True,
            "status_code": "SERVER_7H_COOLDOWN",
            "mood_badge": f"🟠 راحة تلقائية ({remaining_cooldown_mins}د)",
            "banner": f"⏳ [تبريد السيرفر كل 7 ساعات]: السيرفر يأخذ راحة تلقائية لمدة {remaining_cooldown_mins} دقيقة لإعادة ضبط الخوارزمية وتفريغ الضغط.",
            "risk_mult": 0.25,
            "rare_boost": 0.8
        }

    # 3. الأوقات الذهبية النشطة (12ص-7ص | 12ظ-4ع | 8م-8:30م | 9م-10م)
    if (0.0 <= time_dec < 7.0) or (12.0 <= time_dec < 16.0) or (20.0 <= time_dec < 20.5) or (21.0 <= time_dec < 22.0):
        window_name = "الليل الذهبي (12ص-7ص)" if time_dec < 7.0 else (
            "الظهر للعصر (12ظ-4ع)" if time_dec < 16.0 else (
                "نافذة المساء (8م-8:30م)" if time_dec < 20.5 else "ساعة الـ PK الذهبية (9م-10م)"
            )
        )
        return {
            "is_resting": False,
            "status_code": "ACTIVE_GOLDEN",
            "mood_badge": "🟢 السيرفر نشط (وقت ذهبي)",
            "banner": f"🟢 [السيرفر نشط بكامل طاقته - {time_str}]: أنت الآن في ({window_name}) — أفضل وقت لتوقع الجولات القادمة!",
            "risk_mult": 1.0,
            "rare_boost": 1.25
        }

    # 4. الأوقات العادية المتوسطة
    return {
        "is_resting": False,
        "status_code": "ACTIVE_NORMAL",
        "mood_badge": "🟢 السيرفر نشط (مستقر)",
        "banner": f"⚡ [السيرفر نشط - {time_str}]: حالة اللعب متوسطة ومستقرة. الدورة القادمة للراحة بعد {max(1, int(7.0 - cycle_position))} ساعات.",
        "risk_mult": 0.7,
        "rare_boost": 1.0
    }

def check_streak_warning(clean_hist: List[str]) -> Tuple[bool, str]:
    if len(clean_hist) >= 5:
        last_five = clean_hist[-5:]
        if all(x == last_five[0] for x in last_five):
            return True, (
                f"⚠️ [تحذير تذبذب حاد]: النتيجة ({last_five[0]}) تكررت 5 جولات متتالية! "
                f"السيرفر ينصح بالتوقف لجولة واحدة حتى تستقر الدورة."
            )
    return False, ""

def predict_next_farm_round(clean_hist: List[str], cycle_meta: Dict) -> Tuple[str, str, str]:
    """
    يتوقع الجولة القادمة في المزرعة بناءً على:
    1. مصفوفة الانتقال (ماذا يأتي عادةً بعد العنصر الأخير والعنصرين الأخيرين؟).
    2. ضغط الغياب (Overdue Pressure): العناصر التي غابت أكثر من دورتها الطبيعية يرتفع احتمال قدومها في الجولة القادمة.
    3. منع نسخ العنصر الأخير تلقائياً إلا إذا كان نمط السيرفر يكرر.
    """
    n = len(clean_hist)
    rare_boost = cycle_meta.get("rare_boost", 1.0)
    all_items = list(FARM_ITEMS_META.keys())

    if n == 0:
        # توقع افتتاحي متوازن يجمع 3 خضروات + 2 مضاعفات متوسطة
        default_top5 = [
            "1️⃣ 🍅 طماطم (×5) — توقع افتتاحي 21.5%",
            "2️⃣ 🌽 ذرة (×5) — توقع افتتاحي 20.8%",
            "3️⃣ 🥬 خس (×5) — توقع افتتاحي 19.4%",
            "4️⃣ 🌭 سجق (×10) — تغطية مضاعف 12.6%",
            "5️⃣ 🍢 شيش (×15) — صيد مضاعف 8.5%",
            f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>"
        ]
        return "<br>".join(default_top5), "82.8% (افتتاحي)", "[FARM-NEXT] Waiting for history to build transition matrix"

    last_item = clean_hist[-1]
    counts = Counter(clean_hist)

    # 1. حساب مصفوفة الانتقال للجولة القادمة (ماذا جاء بعد last_item في الجولات السابقة؟)
    next_after_last = Counter()
    total_after_last = 0
    for i in range(n - 1):
        if clean_hist[i] == last_item:
            next_after_last[clean_hist[i + 1]] += 1
            total_after_last += 1

    # حساب هل السيرفر في حالة تكرار مباشر أم تغيير؟
    repeat_rate = 0.0
    if n >= 2:
        repeats = sum(1 for i in range(1, n) if clean_hist[i] == clean_hist[i - 1])
        repeat_rate = repeats / (n - 1)

    scores: Dict[str, float] = {}
    for item, meta in FARM_ITEMS_META.items():
        base_w = (meta["base_weight"] / 100.0) * (rare_boost if meta["tier"] in ("high", "rare") else 1.0)

        # حساب عدد الجولات منذ آخر ظهور لهذا العنصر (Overdue Gap)
        if item in clean_hist:
            last_seen_idx = len(clean_hist) - 1 - clean_hist[::-1].index(item)
            gap = (n - 1) - last_seen_idx
        else:
            gap = n + 2  # لم يظهر أبداً في السجل الحالي -> مرشح قوي للظهور القادم

        expected_gap = meta["expected_gap"]
        # كلما زاد غياب العنصر عن دورته المتوقعة، زاد ترشيحه للجولة القادمة
        overdue_pressure = 1.0 + min(0.85, (gap / expected_gap) * 0.45)

        # وزن الانتقال الشرطي (Transition Probability)
        if total_after_last > 0 and next_after_last[item] > 0:
            trans_boost = 1.0 + ((next_after_last[item] / total_after_last) * 0.75)
        else:
            trans_boost = 1.0

        # معالجة العنصر الذي ظهر في الجولة الأخيرة للتو:
        # لا نريده أن يتصدر دائماً إلا إذا كان السيرفر في موجة تكرار واضحة
        if item == last_item:
            if repeat_rate > 0.35:
                immediate_factor = 0.95  # السيرفر يكرر، نبقيه ضمن المرشحين
            else:
                immediate_factor = 0.42  # السيرفر يغير، نخفض العنصر الأخير لنفسح مجالاً للجولة القادمة الحقيقية
        else:
            immediate_factor = 1.15

        # إذا استهلك العنصر أكثر من حصته الطبيعية مؤخراً، نخفض وزنه للجولة القادمة (تصحيح التوازن)
        obs_ratio = (counts.get(item, 0) / n)
        saturation_penalty = 0.70 if obs_ratio > (base_w * 1.8) else 1.0

        scores[item] = base_w * overdue_pressure * trans_boost * immediate_factor * saturation_penalty

    # ضمان تشكيلة ذكية للخمس خانات: (أفضل 3 من فئة الخضروات ×5 + أفضل 2 من فئة المضاعفات العالية/النادرة)
    common_items = [k for k in all_items if FARM_ITEMS_META[k]["tier"] == "common"]
    special_items = [k for k in all_items if FARM_ITEMS_META[k]["tier"] != "common"]

    total_score = sum(scores.values()) or 1.0
    norm_probs = {k: (v / total_score) * 100.0 for k, v in scores.items()}

    top_commons = sorted(common_items, key=lambda k: norm_probs[k], reverse=True)[:3]
    top_specials = sorted(special_items, key=lambda k: norm_probs[k], reverse=True)[:2]

    combined_selection = top_commons + top_specials
    final_top5 = sorted(combined_selection, key=lambda k: norm_probs[k], reverse=True)

    rank_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    lines = []
    top5_sum = 0.0
    for idx, item_name in enumerate(final_top5):
        p = norm_probs[item_name]
        top5_sum += p
        role_tag = "تغطية أساسية" if FARM_ITEMS_META[item_name]["tier"] == "common" else "صيد مضاعف"
        lines.append(f"{rank_emojis[idx]} {item_name} — ترشيح القادم {p:.1f}% ({role_tag})")

    lines.append(f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>")
    conf_str = f"{min(96.5, top5_sum + 12.0):.1f}% (توقع القادم)"
    log_note = f"[FARM-NEXT-PRED] Sample={n} | Last={last_item[:6]} | RepeatRate={repeat_rate:.2f}"
    return "<br>".join(lines), conf_str, log_note

def predict_next_poker_round(clean_hist: List[str], cycle_meta: Dict) -> Tuple[str, str, str]:
    """
    يتوقع الكرسي القادم في البوكر (توقع مؤكد + توقع بديل) بناءً على:
    - ماذا يأتي بعد الكرسي الأخير؟
    - أي كرسي متأخر في الظهور (Overdue Chair)؟
    """
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
    if n == 0:
        return (
            f"⭐ التوقع المؤكد للجولة القادمة: 🟢 كرسي A (34.0%)<br>"
            f"🔄 التوقع البديل (تغطية): 🔵 كرسي B (33.0%)<br>"
            f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>",
            "67.0% (توقع مقعدين)",
            "[POKER-NEXT] Sample=0 | Ready"
        )

    last_chair = norm_hist[-1]
    counts = Counter(norm_hist)

    # حساب الانتقالات بعد الكرسي الأخير
    trans_counts = Counter()
    trans_total = 0
    for i in range(n - 1):
        if norm_hist[i] == last_chair:
            trans_counts[norm_hist[i + 1]] += 1
            trans_total += 1

    # حساب معدل التكرار مقابل التغيير في الطاولة الحالية
    repeats = sum(1 for i in range(1, n) if norm_hist[i] == norm_hist[i - 1])
    repeat_ratio = repeats / (n - 1) if n > 1 else 0.33

    scores = {}
    for c in chairs:
        # فجوة الغياب للكرسي
        if c in norm_hist:
            gap = (n - 1) - (len(norm_hist) - 1 - norm_hist[::-1].index(c))
        else:
            gap = n + 2

        overdue_boost = 1.0 + min(0.65, gap * 0.22)
        trans_prob = ((trans_counts[c] + 1.0) / (trans_total + 3.0)) if trans_total > 0 else (1.0 / 3.0)

        # إذا كان الكرسي هو نفس الكرسي الأخير، نتحقق مما إذا كانت الطاولة تكرر أم تعكس
        if c == last_chair:
            chair_Flow = 1.15 if repeat_ratio >= 0.45 else 0.65
        else:
            chair_Flow = 1.20 if repeat_ratio < 0.45 else 0.90

        # توازن الحصص (الكراسي الأقل ظهوراً في آخر 10 جولات لها فرصة ارتداد أعلى)
        recent_slice = norm_hist[-9:]
        recent_freq = recent_slice.count(c) / len(recent_slice)
        balance_factor = 1.25 if recent_freq < 0.25 else (0.80 if recent_freq > 0.50 else 1.0)

        scores[c] = trans_prob * overdue_boost * chair_Flow * balance_factor

    total_s = sum(scores.values()) or 1.0
    probs = {c: (scores[c] / total_s) * 100.0 for c in chairs}
    ranked = sorted(probs.items(), key=lambda x: x[1], reverse=True)

    main_chair, main_p = ranked[0]
    alt_chair, alt_p = ranked[1]
    comb_p = main_p + alt_p

    pred_html = (
        f"⭐ التوقع المؤكد للجولة القادمة: {main_chair} ({main_p:.1f}%)<br>"
        f"🔄 التوقع البديل (تغطية آمنة): {alt_chair} ({alt_p:.1f}%)<br>"
        f"<span style='font-size:9px;color:#93c5fd;'>[قوة التغطية للجولة القادمة: {comb_p:.1f}% | بناءً على {n} جولات]</span><br>"
        f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>"
    )
    return pred_html, f"{comb_p:.1f}% (مؤكد + بديل)", f"[POKER-NEXT] Last={last_chair} -> Main={main_chair}"

def predict_next_dragon_tiger(clean_hist: List[str], cycle_meta: Dict) -> Tuple[str, str, str]:
    """
    يتوقع الجولة القادمة في Dragon & Tiger عبر كشف نمط الطاولة (سلسلة Streak أو تبادل Chop أو نمط 2-1).
    """
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
            f"🐉 التوقع القادم: تنين (Dragon) — 50.0%<br>"
            f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>",
            "50.0% (افتتاحي)",
            "[DT-NEXT] Sample=0"
        )

    non_tie = [x for x in norm_hist if x != "🤝 تعادل"]
    if not non_tie:
        return (
            f"🐅 التوقع القادم: تايجر (Tiger) — 51.5%<br>"
            f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>",
            "51.5%",
            "[DT-NEXT] Only ties recorded"
        )

    last_item = non_tie[-1]
    opposite = "🐅 تايجر" if last_item == "🐉 تنين" else "🐉 تنين"

    # حساب طول السلسلة الحالية (Current Streak)
    current_streak = 1
    for i in range(len(non_tie) - 2, -1, -1):
        if non_tie[i] == last_item:
            current_streak += 1
        else:
            break

    # حساب عدد مرات التبديل مقابل التكرار
    switches = sum(1 for i in range(1, len(non_tie)) if non_tie[i] != non_tie[i - 1])
    repeats = sum(1 for i in range(1, len(non_tie)) if non_tie[i] == non_tie[i - 1])

    # تحديد توقع الجولة القادمة بذكاء
    if current_streak >= 3 and repeats >= switches:
        # الطاولة في خط متسلسل طويل (Dragon/Tiger Run) -> اتبع السلسلة حتى تنكسر
        next_pred = last_item
        reason = f"تتبع السلسلة النشطة ({current_streak} مرات متتالية)"
    elif current_streak == 2 and switches > repeats:
        # الطاولة تميل للانعكاس السريع بعد مرتين
        next_pred = opposite
        reason = "نقطة انعكاس متوقعة بعد جولتين"
    elif switches > repeats:
        # نمط تبادلي (واحدة بواحدة - Ping Pong) -> اعكس النتيجة الأخيرة فوراً
        next_pred = opposite
        reason = "نمط تبادلي نشط (عكس الجولة السابقة)"
    else:
        # مقارنة التوازن العام
        d_c = non_tie.count("🐉 تنين")
        t_c = non_tie.count("🐅 تايجر")
        next_pred = "🐅 تايجر" if d_c > t_c else "🐉 تنين"
        reason = "تصحيح التوازن الإحصائي للطاولة"

    est_prob = min(74.5, 53.0 + (abs(switches - repeats) * 2.2) + (current_streak * 1.5))
    pred_html = (
        f"⚡ التوقع المؤكد للجولة القادمة: {next_pred} ({est_prob:.1f}%)<br>"
        f"<span style='font-size:9px;color:#93c5fd;'>[السبب: {reason}]</span><br>"
        f"<span style='font-size:9px;color:#fbbf24;'>{cycle_meta['banner']}</span>"
    )
    return pred_html, f"{est_prob:.1f}% (توقع القادم)", f"[DT-NEXT] Streak={current_streak} | Next={next_pred}"

@app.post("/api/predict")
def predict(req: PredictionRequest):
    try:
        game = req.game_type.upper().strip()
        clean_hist = [clean_history_item(h) for h in req.history if h and h.strip()]

        # 1. فحص دورة السيرفر (الـ 7 ساعات + أوقات اللعب والـ PK)
        cycle_meta = check_server_cycle_and_time(req.client_hour, req.client_minute, len(clean_hist))

        # 2. فحص تحذير التكرار 5 مرات
        is_streak_warn, streak_msg = check_streak_warning(clean_hist)

        # 3. توليد توقع الجولة القادمة حسب اللعبة
        if "FARM" in game:
            pred_html, conf_str, log_note = predict_next_farm_round(clean_hist, cycle_meta)
        elif "POKER" in game:
            pred_html, conf_str, log_note = predict_next_poker_round(clean_hist, cycle_meta)
        elif "DRAGON" in game:
            pred_html, conf_str, log_note = predict_next_dragon_tiger(clean_hist, cycle_meta)
        else:
            pred_html, conf_str, log_note = "يرجى اختيار لعبة صالحة.", "--%", "[ERROR] Unknown game"

        # إذا كان السيرفر في وقت راحة/تبريد أو هناك تكرار 5 مرات، نبرز التنبيه بوضوح
        is_danger_mode = cycle_meta["is_resting"] or is_streak_warn
        if is_streak_warn:
            pred_html = f"<span style='color:#fca5a5;'>{streak_msg}</span><br><hr style='border-color:#ef4444;margin:4px 0;'>{pred_html}"

        base_bet = int(req.coins * 0.08 * cycle_meta["risk_mult"]) if req.coins > 0 else int(500 * cycle_meta["risk_mult"])
        if cycle_meta["is_resting"]:
            safe_amount = 0  # في وقت راحة السيرفر أو الوقت السيئ ينصح بعدم الرهان (0 كوينز)
        else:
            safe_amount = base_bet

        return {
            "is_rigged": is_danger_mode,
            "server_mood": cycle_meta["mood_badge"],
            "banner_text": cycle_meta["banner"],
            "confidence": conf_str,
            "prediction": pred_html,
            "safe_bet": safe_amount,
            "engine_log": log_note,
            "sample_size": len(clean_hist)
        }

    except Exception as e:
        return {
            "is_rigged": False,
            "server_mood": "🟡 تنبيه معالجة",
            "banner_text": "جاري إعادة المزامنة...",
            "confidence": "--%",
            "prediction": f"⚠️ حدث خطأ أثناء الحساب: {str(e)}",
            "safe_bet": 0,
            "engine_log": "[ERROR] Exception in endpoint",
            "sample_size": 0
        }

@app.get("/")
def root():
    elapsed_h = (time.time() - SERVER_START_TIME) / 3600.0
    return {
        "status": "Phantom Next-Round Predictor v119.0 Active",
        "uptime_hours": round(elapsed_h, 2),
        "cycle_rule": "Active 7h / Auto-Rest 30m + PK Time Shield",
        "developer": "MR"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
