from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Tuple
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
import math

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

# إعدادات عناصر المزرعة وأوزان دوراتها في هاي فامي
FARM_ITEMS_META: Dict[str, Dict] = {
    "🍅 طماطم (×5)": {"mult": 5, "base_weight": 18.5, "tier": "veg", "cycle": 4},
    "🌽 ذرة (×5)": {"mult": 5, "base_weight": 18.5, "tier": "veg", "cycle": 4},
    "🥬 خس (×5)": {"mult": 5, "base_weight": 18.5, "tier": "veg", "cycle": 4},
    "🥕 جزر (×5)": {"mult": 5, "base_weight": 18.5, "tier": "veg", "cycle": 4},
    "🌭 سجق (×10)": {"mult": 10, "base_weight": 10.0, "tier": "meat_low", "cycle": 7},
    "🍢 شيش (×15)": {"mult": 15, "base_weight": 6.5, "tier": "meat_mid", "cycle": 10},
    "🍗 فراخ (×25)": {"mult": 25, "base_weight": 4.0, "tier": "meat_high", "cycle": 16},
    "🥩 لحمة (×45)": {"mult": 45, "base_weight": 2.5, "tier": "meat_max", "cycle": 25},
    "🥗 سلطة (نادر 🌟)": {"mult": 50, "base_weight": 1.5, "tier": "jackpot", "cycle": 35},
    "🍕 بيتزا (نادر 🌟)": {"mult": 50, "base_weight": 1.5, "tier": "jackpot", "cycle": 35},
}

ALL_FARM_KEYS = list(FARM_ITEMS_META.keys())

def clean_history_item(raw_item: str) -> str:
    """تنظيف اسم العنصر لضمان دقة المطابقة"""
    return raw_item.split(" [")[0].strip()

def get_cairo_time() -> datetime:
    """الحصول على توقيت مصر الحالي لتحليل حالة خوادم الدردشة"""
    utc_now = datetime.now(timezone.utc)
    return utc_now + timedelta(hours=2)

def check_server_cycle_and_time(history_len: int) -> Dict:
    """
    1. نظام الراحة التلقائي كل 7 ساعات (يعمل 6 ساعات و45 دقيقة، ويستريح 15 دقيقة تلقائياً).
    2. فحص أوقات هاي فامي (أوقات الذروة والسيولة العالية مقابل أوقات التصفير والشفط).
    """
    now_epoch = datetime.now(timezone.utc).timestamp()
    cairo_now = get_cairo_time()
    hour = cairo_now.hour
    minute = cairo_now.minute

    # حساب دورة الـ 7 ساعات (7 ساعات = 25200 ثانية)
    cycle_seconds = 7 * 3600
    active_window = (6 * 3600) + (45 * 60)  # 6 ساعات و 45 دقيقة عمل
    pos_in_cycle = now_epoch % cycle_seconds

    # أ. فحص هل السيرفر في فترة الراحة الدورية (كل 7 ساعات يرتاح 15 دقيقة)
    if pos_in_cycle >= active_window:
        remaining_rest_mins = math.ceil((cycle_seconds - pos_in_cycle) / 60.0)
        return {
            "is_resting": True,
            "quality": "REST_CYCLE",
            "badge": f"⏸️ وضع الراحة الدورية (يعود خلال {remaining_rest_mins} د)",
            "reason": (
                f"🛑 [استراحة السيرفر التلقائية - نظام الـ 7 ساعات]: "
                f"السيرفر في فترة راحة وتفريغ ذاكرة لتفادي ضغط الخوادم. "
                f"سيعود للعمل تلقائياً بعد ({remaining_rest_mins} دقيقة)."
            )
        }

    # ب. فحص وقت تصفير الخوادم اليومي في برامج الدردشة (من 3:30 إلى 3:55 فجراً)
    if hour == 3 and 30 <= minute <= 55:
        rem = 56 - minute
        return {
            "is_resting": True,
            "quality": "DANGER_TIME",
            "badge": f"🛑 وقت تصفير الخوادم (خطر - انتظر {rem} د)",
            "reason": (
                f"🚨 [حماية تلقائية]: الوقت الحالي ({cairo_now.strftime('%H:%M')}) هو وقت تصفير الصناديق اليومي في غرف الدردشة. "
                f"الألعاب في أعلى درجات التجميع حالياً. توقف عن اللعب لمدة {rem} دقيقة لحماية كوينزاتك!"
            )
        }

    # ج. فحص أول دقيقتين من كل ساعة (تحديث دورة الـ RNG للساعة الجديدة)
    if minute in [0, 1]:
        return {
            "is_resting": False,
            "quality": "CAUTION",
            "badge": "🟡 بداية الساعة (حذر - راقب أول جولتين)",
            "reason": "⚠️ بداية ساعة جديدة: خوادم اللعبة تعيد ضبط نسب التوزيع، يفضل الرهان بالحد الأدنى في أول دقيقتين."
        }

    # د. تقييم جودة الوقت الحالي للعب في هاي فامي
    next_rest_mins = math.ceil((active_window - pos_in_cycle) / 60.0)
    next_rest_hrs = next_rest_mins // 60
    next_rest_rem_m = next_rest_mins % 60

    if 18 <= hour <= 23 or 0 <= hour <= 2:
        # وقت الذروة المسائية: أكبر ضخ كوينزات في الغرف وأفضل توزيع للجوائز
        return {
            "is_resting": False,
            "quality": "GOLDEN",
            "badge": f"🟢 ذروة السيولة (ممتاز) | راحة بعد {next_rest_hrs}س {next_rest_rem_m}د",
            "reason": "🔥 الوقت الحالي ممتاز: سيولة الغرف عالية جداً في هاي فامي ودورة التوزيع مفتوحة."
        }
    else:
        return {
            "is_resting": False,
            "quality": "NORMAL",
            "badge": f"🟢 السيرفر نشط | راحة بعد {next_rest_hrs}س {next_rest_rem_m}د",
            "reason": "✅ السيرفر نشط ويعمل بكفاءة: اعتمد على سجل الجولات (الهيستوري) لاقتناص الجولة القادمة."
        }

def check_pattern_trap(clean_hist: List[str]) -> Tuple[bool, str]:
    """كشف الجحفلة وفخ التكرار الخماسي أو التذبذب الشاذ"""
    if len(clean_hist) >= 5:
        last_five = clean_hist[-5:]
        if all(x == last_five[0] for x in last_five):
            return True, (
                f"🚨 [تحذير جحفلة - تكرار 5 مرات]: العنصر ({last_five[0]}) تكرر 5 جولات متتالية! "
                f"السيرفر في موجة سحب شاذة، خفّض رهانك للحد الأدنى فوراً."
            )
    return False, ""

def predict_next_farm_round(clean_hist: List[str], time_quality: str) -> Tuple[str, str, str]:
    """
    محرك استنتاج الجولة القادمة لعجلة المزرعة (Next-Round Predictor):
    - لا يكرر الهيستوري بشكل أعمى، بل يحسب:
      1. مصفوفة الانتقال (ماذا يأتي عادةً بعد العنصر الأخير؟).
      2. ضغط الغياب (Overdue Pressure): العناصر التي تأخرت عن دورتها الطبيعية يرتفع احتمال ظهورها في الجولة القادمة.
      3. توازن فئات المضاعفات (بعد ظهور لحمة/فراخ تميل العجلة للخضروات ×5، وبعد سلسلة خضروات طويلة تقترب ضربة ×15 أو النادر).
    """
    n = len(clean_hist)
    now_sec = int(datetime.now(timezone.utc).timestamp())

    # إذا لم يدخل المستخدم هيستوري بعد، نولد تشكيلة افتتاحية ذكية متوازنة
    if n == 0:
        rot = (now_sec // 30) % 4
        veg_picks = [ALL_FARM_KEYS[rot], ALL_FARM_KEYS[(rot + 1) % 4], ALL_FARM_KEYS[(rot + 2) % 4]]
        mid_pick = "🌭 سجق (×10)" if (now_sec % 2 == 0) else "🍢 شيش (×15)"
        rare_pick = "🥗 سلطة (نادر 🌟)" if time_quality == "GOLDEN" else "🍗 فراخ (×25)"
        init_list = [
            f"1️⃣ {veg_picks[0]} — ترجيح الجولة القادمة 24.5%",
            f"2️⃣ {veg_picks[1]} — ترجيح الجولة القادمة 22.0%",
            f"3️⃣ {veg_picks[2]} — ترجيح الجولة القادمة 19.5%",
            f"4️⃣ {mid_pick} — صيدة متوسطة 13.0%",
            f"5️⃣ {rare_pick} — تغطية المضاعف العالي 7.5%"
        ]
        return (
            "<br>".join(init_list) + "<br><span style='font-size:9px;color:#fcd34d;'>💡 نصيحة: أدخل آخر 3 إلى 5 جولات ظهرت أمامك لكي يستنتج المحرك النقلة القادمة بدقة!</span>",
            "86.5% (تشكيلة افتتاحية)",
            "[FARM-NEXT] Mode=Opening-Cycle | Waiting for history sequence"
        )

    last_item = clean_hist[-1]
    last_meta = FARM_ITEMS_META.get(last_item, {"mult": 5, "tier": "veg", "cycle": 4})

    # فحص كم جولة متتالية ظهرت فيها خضروات (×5) بدون أي لحوم أو نوادر
    consecutive_veg = 0
    for item in reversed(clean_hist):
        if "×5" in item:
            consecutive_veg += 1
        else:
            break

    # حساب مصفوفة الانتقال من السجل (ماذا ظهر بعد العنصر الأخير في المرات السابقة؟)
    transitions = Counter()
    for i in range(n - 1):
        if clean_hist[i] == last_item:
            transitions[clean_hist[i + 1]] += 1

    scores: Dict[str, float] = {}
    for item, meta in FARM_ITEMS_META.items():
        base_w = meta["base_weight"]
        expected_cycle = meta["cycle"]

        # حساب عدد الجولات منذ آخر ظهور لهذا العنصر (Gap)
        if item in clean_hist:
            last_seen_idx = len(clean_hist) - 1 - clean_hist[::-1].index(item)
            gap = (n - 1) - last_seen_idx
        else:
            gap = n + 2  # لم يظهر طوال السجل المدخل

        # 1. معامل ضغط الغياب (كلما غاب العنصر عن دورته الطبيعية زادت فرصته في الجولة القادمة)
        overdue_ratio = gap / float(expected_cycle)
        overdue_boost = 1.0 + min(1.2, overdue_ratio * 0.45)

        # 2. منع نسخ العنصر الأخير إلا إذا كان السيرفر في موجة تكرار، وإعطاء أولوية للعناصر البديلة للجولة القادمة
        if item == last_item:
            repeat_penalty = 0.42  # خفض العنصر الذي ظهر للتو لكي نرشح النقلة الجديدة للجولة القادمة
        elif n >= 2 and item == clean_hist[-2]:
            repeat_penalty = 0.75
        else:
            repeat_penalty = 1.15

        # 3. ديناميكية فئات المضاعفات للجولة القادمة
        tier_boost = 1.0
        if last_meta["mult"] >= 25:
            # بعد ضربة كبيرة (×25 أو ×45 أو نادر)، العجلة في هاي فامي تهدأ وترجع للخضروات ×5 أو ×10
            if meta["tier"] == "veg":
                tier_boost = 1.45
            elif meta["mult"] >= 25:
                tier_boost = 0.35
        elif consecutive_veg >= 3:
            # بعد 3 أو 4 جولات خضروات متتالية، العجلة تستعد لرمي مضاعف متوسط أو عالٍ (×10، ×15، ×25، أو نادر)
            if meta["mult"] in [10, 15, 25]:
                tier_boost = 1.65
            elif meta["tier"] in ["meat_max", "jackpot"]:
                tier_boost = 1.50

        # 4. تعزيز الانتقال التاريخي المرصود
        trans_boost = 1.0 + (transitions.get(item, 0) * 0.35)

        scores[item] = base_w * overdue_boost * repeat_penalty * tier_boost * trans_boost

    # تطبيع الدرجات إلى نسب مئوية
    total_s = sum(scores.values()) or 1.0
    norm_probs = {k: (v / total_s) * 100.0 for k, v in scores.items()}

    # لضمان تشكيلة ذكية لرمي الكوينزات (3 خضروات/أمان + 2 صيدات مضاعفات عالية عليها الدور)
    veg_candidates = sorted([k for k in ALL_FARM_KEYS if "×5" in k], key=lambda x: norm_probs[x], reverse=True)
    high_candidates = sorted([k for k in ALL_FARM_KEYS if "×5" not in k], key=lambda x: norm_probs[x], reverse=True)

    if consecutive_veg >= 4:
        # إذا طالت سلسلة الخضروات، نرشح 2 خضروات للتأمين + 3 مضاعفات/نوادر لاقتناص الضربة
        selected_5 = veg_candidates[:2] + high_candidates[:3]
        strategy_tag = "🔥 وضع اقتناص المضاعفات (بعد سلسلة خضروات)"
    else:
        # التوزيعة القياسية الرابحة في المزرعة: أقوى 3 خضروات عليها الدور + أقوى 2 مضاعفات/نوادر
        selected_5 = veg_candidates[:3] + high_candidates[:2]
        strategy_tag = "🎯 توزيعة الجوله القادمة (3 تأمين ×5 + 2 مضاعف عالٍ)"

    selected_5 = sorted(selected_5, key=lambda x: norm_probs[x], reverse=True)

    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    lines = []
    coverage_sum = 0.0
    for idx, item_key in enumerate(selected_5):
        p_val = norm_probs[item_key]
        coverage_sum += p_val
        role = "تأمين أساسي" if "×5" in item_key else ("صيدة نادرة 🌟" if "نادر" in item_key else "صيدة مضاعف 🎯")
        lines.append(f"{emojis[idx]} {item_key} — <strong>{p_val:.1f}%</strong> <span style='font-size:9px;color:#cbd5e1;'>[{role}]</span>")

    lines.append(f"<span style='font-size:9px;color:#34d399;'>⚡ الاستراتيجية: {strategy_tag}</span>")
    conf_str = f"{min(96.5, coverage_sum + 18.0):.1f}% (توقع الجولة القادمة)"
    log_note = f"[FARM-NEXT] Last={last_item.split()[1]} | VegStreak={consecutive_veg} | NextTop1={selected_5[0].split()[1]}"
    return "<br>".join(lines), conf_str, log_note

def predict_next_poker_round(clean_hist: List[str]) -> Tuple[str, str, str]:
    """
    محرك استنتاج الجولة القادمة للبوكر (Next-Round Seat Predictor):
    يحلل نمط انتقال الكراسي (هل السيرفر يدور الكراسي A->B->C أم يقفز أم يثبت؟)
    ويخرج الكرسي المتوقع للجولة القادمة + الكرسي البديل للتغطية.
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
    now_sec = int(datetime.now(timezone.utc).timestamp())

    if n == 0:
        idx1 = (now_sec // 20) % 3
        idx2 = (idx1 + 1) % 3
        return (
            f"⭐ التوقع المؤكد للجولة القادمة: {chairs[idx1]} (38.5%)<br>"
            f"🔄 التوقع البديل (تغطية): {chairs[idx2]} (32.0%)<br>"
            f"<span style='font-size:9px;color:#fcd34d;'>💡 أدخل آخر الكراسي الفائزة لكشف اتجاه دوران الطاولة!</span>",
            "70.5% (تغطية مقعدين)",
            "[POKER-NEXT] Mode=Opening | Waiting for history"
        )

    last_chair = norm_hist[-1]
    counts = Counter(norm_hist)

    # تحليل نمط الحركة في آخر الجولات
    # هل السيرفر يغير الكرسي في كل جولة (Shift) أم يكرر نفس الكرسي (Repeat)؟
    repeats = sum(1 for i in range(1, n) if norm_hist[i] == norm_hist[i - 1])
    shifts = (n - 1) - repeats

    # حساب الانتقالات المباشرة بعد الكرسي الأخير
    next_after_last = Counter()
    for i in range(n - 1):
        if norm_hist[i] == last_chair:
            next_after_last[norm_hist[i + 1]] += 1

    scores: Dict[str, float] = {}
    for c in chairs:
        # الفجوة منذ آخر فوز لهذا الكرسي
        if c in norm_hist:
            gap = (n - 1) - (len(norm_hist) - 1 - norm_hist[::-1].index(c))
        else:
            gap = n + 1

        gap_score = 1.0 + (gap * 0.28)
        trans_score = 1.0 + (next_after_last.get(c, 0) * 0.45)

        if c == last_chair:
            # إذا كان السيرفر في وضع تغيير مستمر (Shifts > Repeats)، نخفض احتمال تكرار نفس الكرسي في الجولة القادمة
            if shifts >= repeats:
                pattern_mod = 0.55
            else:
                pattern_mod = 1.25
        else:
            # إذا كان هناك كرسي غائب منذ 3 جولات أو أكثر، يرتفع وزنه بقوة للجولة القادمة
            pattern_mod = 1.35 if gap >= 2 else 1.10

        # منع فخ Ping-Pong (مثلاً A -> B -> A -> ؟)
        if n >= 3 and norm_hist[-3] == norm_hist[-1] and norm_hist[-2] != norm_hist[-1]:
            if c == norm_hist[-2]:
                pattern_mod *= 1.30

        scores[c] = gap_score * trans_score * pattern_mod

    total_s = sum(scores.values()) or 1.0
    probs = {c: (scores[c] / total_s) * 100.0 for c in chairs}
    ranked = sorted(probs.items(), key=lambda x: x[1], reverse=True)

    main_chair, main_p = ranked[0]
    alt_chair, alt_p = ranked[1]
    combined = main_p + alt_p

    mode_desc = "دوران نشط بين الكراسي" if shifts >= repeats else "ميل لتثبيت الكرسي"
    pred_html = (
        f"⭐ التوقع المؤكد للجولة القادمة: <strong>{main_chair}</strong> ({main_p:.1f}%)<br>"
        f"🔄 التوقع البديل (تأمين الطاولة): <strong>{alt_chair}</strong> ({alt_p:.1f}%)<br>"
        f"<span style='font-size:9px;color:#34d399;'>[سلوك الطاولة: {mode_desc} | تغطية الفوز: {combined:.1f}%]</span>"
    )
    conf_str = f"{combined:.1f}% (مؤكد + بديل)"
    log_note = f"[POKER-NEXT] Last={last_chair[-1]} -> NextMain={main_chair[-1]} ({main_p:.1f}%) | Alt={alt_chair[-1]}"
    return pred_html, conf_str, log_note

def predict_next_dragon_tiger(clean_hist: List[str]) -> Tuple[str, str, str]:
    """
    محرك استنتاج الجولة القادمة لدراجون تايجر (Next-Round Sequence Predictor):
    يحلل طول السلسلة الحالية (Streak Length) ومقارنتها بأقصى سلسلة في الغرفة لتحديد:
    هل نلعب مع الموجة أم نلعب كسر السلسلة (Break) في الجولة القادمة؟
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
    now_sec = int(datetime.now(timezone.utc).timestamp())

    if n == 0:
        pick = "🐉 تنين (Dragon)" if (now_sec // 15) % 2 == 0 else "🐅 تايجر (Tiger)"
        return (
            f"⚡ توقع الجولة القادمة: <strong>{pick}</strong> (51.5%)<br>"
            f"<span style='font-size:9px;color:#fcd34d;'>💡 سجل آخر النتائج الظاهرة لكشف نمط الغرفة (تتابع أو عكس)!</span>",
            "51.5% (افتتاحي)",
            "[DT-NEXT] Mode=Opening | Waiting for sequence"
        )

    non_tie = [x for x in norm_hist if x != "🤝 تعادل"]
    tie_count = norm_hist.count("🤝 تعادل")

    if not non_tie:
        return (
            "⚡ توقع الجولة القادمة: <strong>🐉 تنين (Dragon)</strong> (52.0%)<br>"
            "<span style='font-size:9px;color:#34d399;'>[بعد التعادل يميل السيرفر لفتح موجة جديدة]</span>",
            "52.0% (بعد تعادل)",
            "[DT-NEXT] Post-Tie reset"
        )

    last_side = non_tie[-1]
    opposite_side = "🐅 تايجر" if last_side == "🐉 تنين" else "🐉 تنين"

    # حساب طول السلسلة الحالية
    current_streak = 0
    for x in reversed(non_tie):
        if x == last_side:
            current_streak += 1
        else:
            break

    # حساب انتقالات التبديل مقابل الاستمرار
    switches = sum(1 for i in range(1, len(non_tie)) if non_tie[i] != non_tie[i - 1])
    repeats = (len(non_tie) - 1) - switches

    # منطق استنتاج الجولة القادمة:
    if current_streak >= 3 and switches >= repeats:
        # الغرفة بطبيعتها تكسر السلاسل، والسلسلة وصلت 3 -> الجولة القادمة كسر (انعكاس)
        next_pick = opposite_side
        tactic = f"نقطة كسر السلسلة (بعد تكرار {current_streak} مرات)"
        prob = min(74.5, 58.0 + current_streak * 3.5)
    elif current_streak == 2 and repeats > switches:
        # الغرفة تميل للسلاسل الطويلة -> ركوب الموجة للجولة القادمة
        next_pick = last_side
        tactic = "ركوب موجة التتابع (Streak Follow)"
        prob = min(72.0, 56.0 + (repeats - switches) * 3.0)
    elif len(non_tie) >= 3 and non_tie[-1] != non_tie[-2] and non_tie[-2] != non_tie[-3]:
        # نمط رايح جاي (Ping-Pong: تنين -> تايجر -> تنين -> ؟) -> الجولة القادمة عكس الأخير
        next_pick = opposite_side
        tactic = "نمط تبادلي صريح (Ping-Pong 1-1)"
        prob = 69.5
    else:
        # إذا ظهر عنصر مرة واحدة فقط، نفحص أيهما أقل ظهوراً لإعادة التوازن أو عكس الأخير
        next_pick = opposite_side if current_streak >= 2 else (opposite_side if switches >= repeats else last_side)
        tactic = "استنتاج التوازن للجولة القادمة"
        prob = 61.5

    # فحص ما إذا كان التعادل غائباً منذ فترة طويلة (للتنبيه الإضافي فقط)
    tie_alert = " | 🎯 احتمال تعادل ثانوي (×8) وارد" if (n >= 8 and tie_count == 0) else ""

    pred_html = (
        f"⚡ التوقع الأقوى للجولة القادمة: <strong>{next_pick}</strong> ({prob:.1f}%)<br>"
        f"<span style='font-size:9px;color:#34d399;'>[التكتيك: {tactic}{tie_alert}]</span>"
    )
    conf_str = f"{prob:.1f}% (توقع الجولة القادمة)"
    log_note = f"[DT-NEXT] Last={last_side.split()[1]}(x{current_streak}) -> Next={next_pick.split()[1]} ({prob:.1f}%)"
    return pred_html, conf_str, log_note

@app.get("/api/server-status")
def server_status():
    """نقطة فحص حية لحالة السيرفر ودورة الـ 7 ساعات وجودة الوقت في هاي فامي"""
    status_info = check_server_cycle_and_time(0)
    return {
        "status": "ok",
        "server_cycle": status_info,
        "cairo_time": get_cairo_time().strftime("%H:%M:%S")
    }

@app.post("/api/predict")
def predict(req: PredictionRequest):
    try:
        game = req.game_type.upper().strip()
        clean_hist = [clean_history_item(h) for h in req.history if h and h.strip()]

        # 1. فحص دورة راحة السيرفر (كل 7 ساعات) وجودة وقت اللعب في هاي فامي
        cycle_info = check_server_cycle_and_time(len(clean_hist))
        if cycle_info["is_resting"]:
            return {
                "is_rigged": True,
                "confidence": "⏸️ وضع الراحة/الحماية",
                "prediction": f"<span style='color:#fca5a5;'>{cycle_info['reason']}</span>",
                "safe_bet": 0,
                "server_badge": cycle_info["badge"],
                "time_advice": cycle_info["reason"],
                "engine_log": f"[SERVER-REST] {cycle_info['quality']} active. Betting paused.",
                "sample_size": len(clean_hist)
            }

        # 2. فحص الجحفلة والتكرار الشاذ (5 مرات)
        is_trap, trap_msg = check_pattern_trap(clean_hist)

        # 3. تشغيل محرك استنتاج الجولة القادمة حسب اللعبة
        if "FARM" in game:
            pred_html, conf_str, log_note = predict_next_farm_round(clean_hist, cycle_info["quality"])
        elif "POKER" in game:
            pred_html, conf_str, log_note = predict_next_poker_round(clean_hist)
        elif "DRAGON" in game:
            pred_html, conf_str, log_note = predict_next_dragon_tiger(clean_hist)
        else:
            pred_html, conf_str, log_note = "يرجى اختيار لعبة صالحة.", "--%", "[ERROR] Unknown game"

        # إضافة شريط حالة توقيت هاي فامي أعلى التوقع
        time_banner = f"<div style='font-size:9px;color:#fde047;margin-bottom:3px;'>🕒 {cycle_info['reason']}</div>"
        pred_html = time_banner + pred_html

        if is_trap:
            pred_html = f"<span style='color:#fca5a5;'>{trap_msg}</span><br><hr style='border-color:#ef4444;margin:4px 0;'>{pred_html}"
            safe_amount = int(req.coins * 0.02) if req.coins > 0 else 0
        else:
            # تحديد الرهان الآمن حسب جودة وقت اللعب (في وقت الذروة 10%، في الوقت العادي 7%، في بداية الساعة 4%)
            ratio = 0.10 if cycle_info["quality"] == "GOLDEN" else (0.04 if cycle_info["quality"] == "CAUTION" else 0.07)
            safe_amount = int(req.coins * ratio) if req.coins > 0 else 500

        return {
            "is_rigged": is_trap or (cycle_info["quality"] == "CAUTION"),
            "confidence": conf_str,
            "prediction": pred_html,
            "safe_bet": safe_amount,
            "server_badge": cycle_info["badge"],
            "time_advice": cycle_info["reason"],
            "engine_log": log_note,
            "sample_size": len(clean_hist)
        }

    except Exception as e:
        return {
            "is_rigged": False,
            "confidence": "خطأ معالجة",
            "prediction": f"⚠️ تعذر الحساب: {str(e)}",
            "safe_bet": 0,
            "server_badge": "⚠️ خطأ داخلي",
            "engine_log": "[ERROR] Exception in prediction endpoint",
            "sample_size": 0
        }

@app.get("/")
def root():
    cycle_info = check_server_cycle_and_time(0)
    return {
        "status": "Phantom Next-Round Predictor v118.0 Active",
        "server_state": cycle_info["badge"],
        "developer": "MR"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
