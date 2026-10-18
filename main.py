import asyncio
import hashlib
import json
import logging
import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# إعداد نظام تسجيل الأخطاء والأحداث
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("PhantomStatisticalServer")

app = FastAPI(title="Phantom Statistical & Lifecycle Engine", version="120.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE_FILE_PATH = os.environ.get("PHANTOM_STATE_FILE", "phantom_server_state.json")


# =====================================================================
# 1. نماذج البيانات والإعدادات القابلة للتعديل
# =====================================================================

class RuntimeConfig(BaseModel):
    review_cycle_hours: float = Field(default=7.0, ge=0.5, le=72.0)
    idle_timeout_seconds: int = Field(default=600, ge=60, le=7200)
    low_load_rest_minutes: int = Field(default=15, ge=1, le=180)
    max_concurrent_tasks: int = Field(default=12, ge=1, le=100)
    high_load_threshold_rpm: int = Field(default=45, ge=5, le=1000)
    min_sample_for_pattern: int = Field(default=6, ge=3, le=50)
    dedup_window_seconds: int = Field(default=2, ge=0, le=30)


class PredictionRequest(BaseModel):
    game_type: str
    room_id: Optional[str] = "default"
    history: List[str] = []
    coins: int = 0
    client_hour: Optional[int] = None
    client_minute: Optional[int] = None
    force_refresh: Optional[bool] = False


# =====================================================================
# 2. مدير التشغيل والموارد ودورة الـ 7 ساعات (Lifecycle & Load Manager)
# =====================================================================

class ServerLifecycleManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.config = RuntimeConfig()
        self.start_time = time.time()
        self.last_checkpoint_time = time.time()
        self.last_request_time = time.time()
        self.operational_mode = "ACTIVE"  # ACTIVE | LOW_POWER_REST | HIGH_LOAD_THROTTLED | MAINTENANCE_CHECKPOINT
        self.total_requests = 0
        self.error_count = 0
        self.checkpoints_completed = 0
        self.active_tasks = 0
        self.recent_request_timestamps: List[float] = []
        self.dedup_cache: Dict[str, Tuple[float, Dict]] = {}
        self.room_histories: Dict[str, List[str]] = {}
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        self.load_persisted_state()

    def load_persisted_state(self):
        """استرجاع حالة العمل والبيانات المحفوظة عند بدء التشغيل لمنع فقدان السجل"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.total_requests = data.get("total_requests", 0)
                self.checkpoints_completed = data.get("checkpoints_completed", 0)
                self.room_histories = data.get("room_histories", {})
                saved_cfg = data.get("config")
                if saved_cfg:
                    self.config = RuntimeConfig(**saved_cfg)
                    self.semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
                logger.info("Persisted state loaded successfully.")
            except Exception as exc:
                logger.error(f"Failed to load state file: {exc}")

    def save_persisted_state(self):
        """حفظ حالة العمل والبيانات قبل أي نقطة مراجعة أو راحة"""
        try:
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(time.time() - self.start_time, 2),
                "total_requests": self.total_requests,
                "error_count": self.error_count,
                "checkpoints_completed": self.checkpoints_completed,
                "config": self.config.dict(),
                "room_histories": self.room_histories,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"State persistence error: {exc}")

    def update_config(self, new_cfg: RuntimeConfig):
        self.config = new_cfg
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        self.save_persisted_state()

    def get_requests_per_minute(self) -> int:
        now = time.time()
        self.recent_request_timestamps = [t for t in self.recent_request_timestamps if now - t <= 60.0]
        return len(self.recent_request_timestamps)

    def evaluate_lifecycle_state(self, is_incoming_prediction: bool = False) -> Dict:
        now = time.time()
        rpm = self.get_requests_per_minute()
        hours_since_checkpoint = (now - self.last_checkpoint_time) / 3600.0
        idle_seconds = now - self.last_request_time

        # 1. فحص نقطة المراجعة الدورية (كل 7 ساعات افتراضياً) دون إعادة تشغيل قسرية
        if hours_since_checkpoint >= self.config.review_cycle_hours:
            self.operational_mode = "MAINTENANCE_CHECKPOINT"
            self.save_persisted_state()
            # تنظيف الذاكرة المؤقتة القديمة مع الحفاظ على سجلات الغرف
            self.dedup_cache = {
                k: v for k, v in self.dedup_cache.items() if now - v[0] <= self.config.dedup_window_seconds
            }
            self.last_checkpoint_time = now
            self.checkpoints_completed += 1
            logger.info(f"7-Hour Checkpoint #{self.checkpoints_completed} completed without data loss.")

        # 2. الاستئناف التلقائي فور وصول طلب جديد إذا كان في وضع الراحة
        if is_incoming_prediction:
            self.last_request_time = now
            self.recent_request_timestamps.append(now)
            rpm = self.get_requests_per_minute()
            if rpm >= self.config.high_load_threshold_rpm:
                self.operational_mode = "HIGH_LOAD_THROTTLED"
            else:
                self.operational_mode = "ACTIVE"
        else:
            # في حال عدم وجود طلبات لفترة طويلة ينتقل السيرفر تلقائياً لوضع الراحة وتوفير الموارد
            if idle_seconds >= self.config.idle_timeout_seconds:
                if self.operational_mode != "LOW_POWER_REST":
                    self.operational_mode = "LOW_POWER_REST"
                    self.save_persisted_state()
            elif rpm >= self.config.high_load_threshold_rpm:
                self.operational_mode = "HIGH_LOAD_THROTTLED"
            else:
                self.operational_mode = "ACTIVE"

        next_checkpoint_in_hours = max(
            0.0, self.config.review_cycle_hours - ((now - self.last_checkpoint_time) / 3600.0)
        )

        mode_Arabic = {
            "ACTIVE": "🟢 السيرفر نشط (تشغيل قياسي)",
            "LOW_POWER_REST": "🟠 وضع توفير الموارد (يستأنف تلقائياً عند الطلب)",
            "HIGH_LOAD_THROTTLED": "🟡 ضغط مرتفع (تنظيم المهام المتزامنة نشط)",
            "MAINTENANCE_CHECKPOINT": "🔵 نقطة مراجعة الـ 7 ساعات (حفظ البيانات مكتمل)",
        }.get(self.operational_mode, "🟢 نشط")

        return {
            "mode": self.operational_mode,
            "mode_label": mode_Arabic,
            "rpm": rpm,
            "active_tasks": self.active_tasks,
            "max_concurrent_tasks": self.config.max_concurrent_tasks,
            "uptime_hours": round((now - self.start_time) / 3600.0, 2),
            "next_checkpoint_hours": round(next_checkpoint_in_hours, 2),
            "checkpoints_completed": self.checkpoints_completed,
            "total_requests": self.total_requests,
            "error_count": self.error_count,
        }


lifecycle = ServerLifecycleManager(STATE_FILE_PATH)


# =====================================================================
# 3. المحركات الإحصائية المنفصلة للألعاب الثلاث (بدون تسريب للمستقبل)
# =====================================================================

FARM_CATALOG: Dict[str, Dict] = {
    "🍅 طماطم (×5)": {"mult": 5, "prior_prob": 0.19, "expected_gap": 5.26, "group": "x5"},
    "🌽 ذرة (×5)": {"mult": 5, "prior_prob": 0.19, "expected_gap": 5.26, "group": "x5"},
    "🥬 خس (×5)": {"mult": 5, "prior_prob": 0.19, "expected_gap": 5.26, "group": "x5"},
    "🥕 جزر (×5)": {"mult": 5, "prior_prob": 0.19, "expected_gap": 5.26, "group": "x5"},
    "🌭 سجق (×10)": {"mult": 10, "prior_prob": 0.095, "expected_gap": 10.52, "group": "mid"},
    "🍢 شيش (×15)": {"mult": 15, "prior_prob": 0.060, "expected_gap": 16.67, "group": "mid"},
    "🍗 فراخ (×25)": {"mult": 25, "prior_prob": 0.038, "expected_gap": 26.31, "group": "high"},
    "🥩 لحمة (×45)": {"mult": 45, "prior_prob": 0.021, "expected_gap": 47.62, "group": "high"},
    "🥗 سلطة (نادر 🌟)": {"mult": 50, "prior_prob": 0.013, "expected_gap": 76.92, "group": "rare"},
    "🍕 بيتزا (نادر 🌟)": {"mult": 50, "prior_prob": 0.013, "expected_gap": 76.92, "group": "rare"},
}


def normalize_item_name(raw: str) -> str:
    return raw.split(" [")[0].strip()


def _farm_score_next_step(history_slice: List[str]) -> Dict[str, float]:
    """
    حساب التوزيع الاحتمالي للجولة التالية لعجلة المزرعة بالاعتماد فقط على السجل المتاح حتى اللحظة t:
    - يدمج مصفوفة الانتقال الشرطي من الدرجة الأولى والثانية (Markov Order-1 & Order-2).
    - يدمج ضغط التأخر عن الدورة الطبيعية (Overdue Gap Ratio) لكسر ثبات عناصر ×5.
    - يطبق عقوبة عدم نسخ آخر نتيجة مباشرة ما لم يكن معدل التكرار في السجل مرتفعاً فعلياً.
    """
    n = len(history_slice)
    all_items = list(FARM_CATALOG.keys())
    if n == 0:
        return {k: v["prior_prob"] * 100.0 for k, v in FARM_CATALOG.items()}

    counts = Counter(history_slice)
    last_1 = history_slice[-1]
    last_2 = tuple(history_slice[-2:]) if n >= 2 else None

    # انتقالات الدرجة الأولى: ماذا جاء بعد last_1؟
    trans_1 = Counter()
    total_trans_1 = 0
    for i in range(n - 1):
        if history_slice[i] == last_1:
            trans_1[history_slice[i + 1]] += 1
            total_trans_1 += 1

    # انتقالات الدرجة الثانية: ماذا جاء بعد (history[-2], history[-1])؟
    trans_2 = Counter()
    total_trans_2 = 0
    if last_2 and n >= 3:
        for i in range(n - 2):
            if (history_slice[i], history_slice[i + 1]) == last_2:
                trans_2[history_slice[i + 2]] += 1
                total_trans_2 += 1

    # معدل التكرار المتتالي الفعلي في السجل
    consecutive_repeats = sum(1 for i in range(1, n) if history_slice[i] == history_slice[i - 1])
    observed_repeat_rate = (consecutive_repeats / (n - 1)) if n > 1 else 0.15

    raw_scores: Dict[str, float] = {}
    for item, meta in FARM_CATALOG.items():
        prior = meta["prior_prob"]
        obs_freq = counts.get(item, 0) / n

        # 1. تقدير بايزي مرن للتكرار العام
        alpha = 4.0
        empirical_bayes = (counts.get(item, 0) + alpha * prior) / (n + alpha)

        # 2. احتمال الانتقال الشرطي للجولة القادمة
        p_t1 = (trans_1[item] + 1.5 * prior) / (total_trans_1 + 1.5) if total_trans_1 > 0 else empirical_bayes
        p_t2 = (trans_2[item] + 1.0 * prior) / (total_trans_2 + 1.0) if total_trans_2 > 0 else p_t1

        # 3. حساب فجوة الغياب (كم جولة مرت منذ آخر ظهور لهذا العنصر؟)
        if item in history_slice:
            last_pos = len(history_slice) - 1 - history_slice[::-1].index(item)
            gap = (n - 1) - last_pos
        else:
            gap = n + 1

        overdue_ratio = gap / meta["expected_gap"]
        # منحنى ضغط الغياب: يرفع وزن العناصر التي تأخرت عن دورتها لمنع جمود التوقعات
        overdue_multiplier = 1.0 + min(1.40, math.pow(overdue_ratio, 1.15) * 0.48)

        # 4. تصحيح التشبع في آخر 6 جولات (منع احتكار عنصر واحد للتوقعات)
        recent_window = history_slice[-6:]
        recent_share = recent_window.count(item) / len(recent_window)
        saturation_factor = 0.62 if recent_share > (prior * 2.1) else 1.08

        # 5. منع نسخ آخر نتيجة تلقائياً إلا إذا كان السجل يظهر موجة تكرار حقيقية
        if item == last_1:
            anti_copy_factor = 0.92 if observed_repeat_rate >= 0.30 else 0.48
        else:
            anti_copy_factor = 1.12

        combined = (0.30 * empirical_bayes + 0.42 * p_t1 + 0.28 * p_t2)
        raw_scores[item] = combined * overdue_multiplier * saturation_factor * anti_copy_factor

    total = sum(raw_scores.values()) or 1.0
    return {k: (v / total) * 100.0 for k, v in raw_scores.items()}


def _select_farm_top5(prob_map: Dict[str, float]) -> List[Tuple[str, float]]:
    """
    اختيار أقوى 5 عناصر لعجلة المزرعة مع ضمان عدم احتكار فئة ×5 لجميع المراكز
    عندما ترتفع مؤشرات المضاعفات المتوسطة أو العالية.
    """
    x5_items = [k for k, v in FARM_CATALOG.items() if v["group"] == "x5"]
    mult_items = [k for k, v in FARM_CATALOG.items() if v["group"] != "x5"]

    best_x5 = sorted(x5_items, key=lambda k: prob_map[k], reverse=True)[:3]
    best_mult = sorted(mult_items, key=lambda k: prob_map[k], reverse=True)[:2]

    selected_keys = best_x5 + best_mult
    selected_sorted = sorted(selected_keys, key=lambda k: prob_map[k], reverse=True)
    return [(k, prob_map[k]) for k in selected_sorted]


def run_farm_engine(clean_hist: List[str], min_sample: int) -> Dict:
    n = len(clean_hist)
    prob_map = _farm_score_next_step(clean_hist)
    top5 = _select_farm_top5(prob_map)
    top5_coverage = sum(p for _, p in top5)

    # اختبار تاريخي صارم بدون تسريب للمستقبل (Walk-Forward Backtest)
    backtest_hits = 0
    backtest_trials = 0
    baseline_hits = 0
    default_baseline_set = {
        "🍅 طماطم (×5)", "🌽 ذرة (×5)", "🥬 خس (×5)", "🥕 جزر (×5)", "🌭 سجق (×10)"
    }

    if n >= 4:
        for t in range(3, n):
            past_only = clean_hist[:t]  # لا يتضمن clean_hist[t] إطلاقاً
            step_probs = _farm_score_next_step(past_only)
            step_top5 = {k for k, _ in _select_farm_top5(step_probs)}
            actual_next = clean_hist[t]
            if actual_next in step_top5:
                backtest_hits += 1
            if actual_next in default_baseline_set:
                baseline_hits += 1
            backtest_trials += 1

    hit_rate = (backtest_hits / backtest_trials * 100.0) if backtest_trials > 0 else 0.0
    base_rate = (baseline_hits / backtest_trials * 100.0) if backtest_trials > 0 else 0.0

    rank_icons = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    lines = []
    for idx, (item_name, p) in enumerate(top5):
        mult_val = FARM_CATALOG[item_name]["mult"]
        role = "تغطية (×5)" if mult_val == 5 else f"مضاعف (×{mult_val})"
        lines.append(f"{rank_icons[idx]} {item_name} — وزن القادم: {p:.1f}% [{role}]")

    last_seen_str = clean_hist[-1] if n > 0 else "لا يوجد"
    if n < min_sample:
        data_status = (
            f"⚠️ [البيانات غير كافية لجزم نمط]: مسجل {n}/{min_sample} جولات. "
            f"التقدير الحالي يعتمد على الأوزان المرجعية وتحليل أولي."
        )
        conf_badge = f"عينة أولية ({n} جولات)"
    else:
        data_status = (
            f"📊 [تحليل Walk-Forward على {backtest_trials} جولة اختبار]: "
            f"نجاح النموذج {hit_rate:.1f}% (مقابل مرجع ثابت {base_rate:.1f}%) | آخر نتيجة: {last_seen_str}"
        )
        conf_badge = f"اختبار فعلي: {hit_rate:.1f}% (عينة: {n})"

    lines.append(f"<span style='font-size:9px;color:#93c5fd;'>{data_status}</span>")
    log_msg = f"[FARM-ENGINE] N={n} | Backtest={backtest_hits}/{backtest_trials} ({hit_rate:.1f}%) vs Base({base_rate:.1f}%)"

    return {
        "prediction_html": "<br>".join(lines),
        "confidence": conf_badge,
        "engine_log": log_msg,
        "measured_accuracy": round(hit_rate, 1) if backtest_trials > 0 else None,
        "baseline_accuracy": round(base_rate, 1) if backtest_trials > 0 else None,
        "top_coverage": round(top5_coverage, 1),
    }


def _poker_score_next_step(norm_hist: List[str]) -> Dict[str, float]:
    chairs = ["🟢 كرسي A", "🔵 كرسي B", "🔴 كرسي C"]
    n = len(norm_hist)
    if n == 0:
        return {c: 100.0 / 3.0 for c in chairs}

    counts = Counter(norm_hist)
    last_1 = norm_hist[-1]
    last_2 = tuple(norm_hist[-2:]) if n >= 2 else None

    trans_1 = Counter()
    t1_total = 0
    for i in range(n - 1):
        if norm_hist[i] == last_1:
            trans_1[norm_hist[i + 1]] += 1
            t1_total += 1

    trans_2 = Counter()
    t2_total = 0
    if last_2 and n >= 3:
        for i in range(n - 2):
            if (norm_hist[i], norm_hist[i + 1]) == last_2:
                trans_2[norm_hist[i + 2]] += 1
                t2_total += 1

    repeats = sum(1 for i in range(1, n) if norm_hist[i] == norm_hist[i - 1])
    rep_rate = (repeats / (n - 1)) if n > 1 else 0.33

    scores = {}
    for c in chairs:
        emp = (counts.get(c, 0) + 1.0) / (n + 3.0)
        p1 = (trans_1[c] + 0.7) / (t1_total + 2.1) if t1_total > 0 else emp
        p2 = (trans_2[c] + 0.5) / (t2_total + 1.5) if t2_total > 0 else p1

        if c in norm_hist:
            gap = (n - 1) - (len(norm_hist) - 1 - norm_hist[::-1].index(c))
        else:
            gap = n + 1

        overdue_factor = 1.0 + min(0.45, (gap / 3.0) * 0.22)
        if c == last_1:
            flow_factor = 1.08 if rep_rate > 0.42 else 0.72
        else:
            flow_factor = 1.12 if rep_rate <= 0.42 else 0.92

        scores[c] = (0.25 * emp + 0.45 * p1 + 0.30 * p2) * overdue_factor * flow_factor

    total = sum(scores.values()) or 1.0
    return {c: (scores[c] / total) * 100.0 for c in chairs}


def run_poker_engine(clean_hist: List[str], min_sample: int) -> Dict:
    norm_hist = []
    for h in clean_hist:
        if "A" in h:
            norm_hist.append("🟢 كرسي A")
        elif "B" in h:
            norm_hist.append("🔵 كرسي B")
        elif "C" in h:
            norm_hist.append("🔴 كرسي C")

    n = len(norm_hist)
    probs = _poker_score_next_step(norm_hist)
    ranked = sorted(probs.items(), key=lambda x: x[1], reverse=True)
    main_chair, main_p = ranked[0]
    alt_chair, alt_p = ranked[1]
    combined_cov = main_p + alt_p

    # اختبار تاريخي صارم لمقعدين (Primary + Backup) ومقعد واحد
    hits_dual = 0
    hits_single = 0
    trials = 0
    if n >= 4:
        for t in range(3, n):
            sub_probs = _poker_score_next_step(norm_hist[:t])
            sub_ranked = sorted(sub_probs.items(), key=lambda x: x[1], reverse=True)
            if norm_hist[t] == sub_ranked[0][0]:
                hits_single += 1
            if norm_hist[t] in (sub_ranked[0][0], sub_ranked[1][0]):
                hits_dual += 1
            trials += 1

    dual_acc = (hits_dual / trials * 100.0) if trials > 0 else 0.0
    single_acc = (hits_single / trials * 100.0) if trials > 0 else 0.0

    last_chair_str = norm_hist[-1] if n > 0 else "لا يوجد"
    if n < min_sample:
        status_line = f"⚠️ [البيانات غير كافية لجزم نمط]: مسجل {n}/{min_sample} جولات (المرجع النظري لمقعدين: 66.7%)."
        conf_str = f"عينة أولية ({n} جولات)"
    else:
        status_line = (
            f"📊 [اختبار Walk-Forward على {trials} جولة]: "
            f"إصابة(أساسي+بديل)={dual_acc:.1f}% | إصابة(أساسي فقط)={single_acc:.1f}% | آخر جولة: {last_chair_str}"
        )
        conf_str = f"اختبار فعلي: {dual_acc:.1f}% (عينة: {n})"

    pred_html = (
        f"⭐ التوقع المستقل للجولة التالية: {main_chair} ({main_p:.1f}%)<br>"
        f"🔄 التغطية البديلة: {alt_chair} ({alt_p:.1f}%)<br>"
        f"<span style='font-size:9px;color:#93c5fd;'>{status_line}</span>"
    )
    log_msg = f"[POKER-ENGINE] N={n} | DualHit={hits_dual}/{trials} ({dual_acc:.1f}%) | Baseline=66.7%"
    return {
        "prediction_html": pred_html,
        "confidence": conf_str,
        "engine_log": log_msg,
        "measured_accuracy": round(dual_acc, 1) if trials > 0 else None,
        "baseline_accuracy": 66.7,
    }


def _dt_score_next_step(norm_hist: List[str]) -> Tuple[str, float, str]:
    non_tie = [x for x in norm_hist if x != "🤝 تعادل"]
    if not non_tie:
        return "🐉 تنين", 46.3, "توزيع مرجعي متكافئ (لا توجد بيانات حسم)"

    n_nt = len(non_tie)
    last_item = non_tie[-1]
    opposite = "🐅 تايجر" if last_item == "🐉 تنين" else "🐉 تنين"

    # حساب طول السلسلة الحالية
    streak = 1
    for i in range(n_nt - 2, -1, -1):
        if non_tie[i] == last_item:
            streak += 1
        else:
            break

    # حساب مصفوفة الانتقال للأنماط (ماذا حدث سابقاً عندما وصلت السلسلة لهذا الطول أو بعد آخر نتيجتين؟)
    switches = sum(1 for i in range(1, n_nt) if non_tie[i] != non_tie[i - 1])
    repeats = sum(1 for i in range(1, n_nt) if non_tie[i] == non_tie[i - 1])

    if n_nt >= 3:
        last_pair = (non_tie[-2], non_tie[-1])
        pair_next = Counter()
        pair_total = 0
        for i in range(n_nt - 2):
            if (non_tie[i], non_tie[i + 1]) == last_pair:
                pair_next[non_tie[i + 2]] += 1
                pair_total += 1
    else:
        pair_next = Counter()
        pair_total = 0

    d_score = 0.5
    t_score = 0.5

    if pair_total > 0:
        p_d = (pair_next["🐉 تنين"] + 0.5) / (pair_total + 1.0)
        p_t = (pair_next["🐅 تايجر"] + 0.5) / (pair_total + 1.0)
        d_score = 0.6 * p_d + 0.4 * d_score
        t_score = 0.6 * p_t + 0.4 * t_score

    # دمج ميل السلسلة مقابل الانعكاس
    if switches > repeats * 1.15:
        pattern_desc = f"نمط تبادلي نشط (Chop: {switches} تبديل مقابل {repeats} تكرار)"
        if opposite == "🐉 تنين":
            d_score += 0.14
        else:
            t_score += 0.14
    elif repeats > switches * 1.15:
        pattern_desc = f"نمط سلاسل نشط (Streak={streak} | {repeats} تكرار مقابل {switches} تبديل)"
        if last_item == "🐉 تنين":
            d_score += 0.14
        else:
            t_score += 0.14
    else:
        pattern_desc = f"توزيع عشوائي متقارب (تبديل={switches}, تكرار={repeats})"
        if opposite == "🐉 تنين":
            d_score += 0.04
        else:
            t_score += 0.04

    total = d_score + t_score
    d_prob = (d_score / total) * 100.0
    t_prob = (t_score / total) * 100.0

    if d_prob >= t_prob:
        return "🐉 تنين", d_prob, pattern_desc
    else:
        return "🐅 تايجر", t_prob, pattern_desc


def run_dragon_tiger_engine(clean_hist: List[str], min_sample: int) -> Dict:
    norm_hist = []
    for h in clean_hist:
        if "تنين" in h:
            norm_hist.append("🐉 تنين")
        elif "تايجر" in h:
            norm_hist.append("🐅 تايجر")
        elif "تعادل" in h:
            norm_hist.append("🤝 تعادل")

    n = len(norm_hist)
    pred_item, est_prob, pattern_desc = _dt_score_next_step(norm_hist)

    # اختبار تاريخي صارم بدون تسريب للمستقبل
    hits = 0
    trials = 0
    if n >= 4:
        for t in range(3, n):
            if norm_hist[t] == "🤝 تعادل":
                continue
            step_pred, _, _ = _dt_score_next_step(norm_hist[:t])
            if step_pred == norm_hist[t]:
                hits += 1
            trials += 1

    measured_acc = (hits / trials * 100.0) if trials > 0 else 0.0
    d_c = norm_hist.count("🐉 تنين")
    t_c = norm_hist.count("🐅 تايجر")
    tie_c = norm_hist.count("🤝 تعادل")
    last_seen = norm_hist[-1] if n > 0 else "لا يوجد"

    if n < min_sample:
        info_line = f"⚠️ [البيانات غير كافية لجزم علاقة تنبؤية]: مسجل {n}/{min_sample} جولات (المرجع النظري: 46.3%)."
        conf_str = f"عينة أولية ({n} جولات)"
    else:
        info_line = (
            f"📊 [اختبار Walk-Forward على {trials} جولة حسم]: "
            f"الدقة المحققة {measured_acc:.1f}% (المرجع 46.3%) | السجل: تنين({d_c}) تايجر({t_c}) تعادل({tie_c}) | آخر نتيجة: {last_seen}"
        )
        conf_str = f"اختبار فعلي: {measured_acc:.1f}% (عينة: {n})"

    pred_html = (
        f"⚡ التقدير المستقل للجولة التالية: {pred_item} (وزن نسبي: {est_prob:.1f}%)<br>"
        f"<span style='font-size:9px;color:#cbd5e1;'>[تشخيص السجل: {pattern_desc}]</span><br>"
        f"<span style='font-size:9px;color:#93c5fd;'>{info_line}</span>"
    )
    log_msg = f"[DT-ENGINE] N={n} | Hit={hits}/{trials} ({measured_acc:.1f}%) | Mode={pattern_desc[:18]}"
    return {
        "prediction_html": pred_html,
        "confidence": conf_str,
        "engine_log": log_msg,
        "measured_accuracy": round(measured_acc, 1) if trials > 0 else None,
        "baseline_accuracy": 46.3,
    }


def detect_streak_anomaly(clean_hist: List[str]) -> Tuple[bool, str]:
    """فحص تكرار النتيجة 5 مرات متتالية دون افتراض حتمية الانعكاس"""
    if len(clean_hist) >= 5:
        last_five = clean_hist[-5:]
        if all(x == last_five[0] for x in last_five):
            return True, (
                f"⚠️ [تنبيه تذبذب مبني على البيانات]: النتيجة ({last_five[0]}) تكررت 5 مرات متتالية. "
                f"هذا يشير إلى تذبذب مرتفع ولا يعني حتمية انعكاس الجولة التالية؛ يُنصح بخفض المخاطرة."
            )
    return False, ""


# =====================================================================
# 4. نقاط النهاية (API Endpoints) مع حماية التكرار ومراقبة الصحة
# =====================================================================

@app.get("/api/health")
def health_check():
    state = lifecycle.evaluate_lifecycle_state(is_incoming_prediction=False)
    return {
        "ok": True,
        "service": "Phantom Statistical & Lifecycle Server",
        "version": "120.0",
        "lifecycle": state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/config")
def get_runtime_config():
    return {
        "config": lifecycle.config.dict(),
        "lifecycle": lifecycle.evaluate_lifecycle_state(is_incoming_prediction=False),
    }


@app.post("/api/config")
def update_runtime_config(new_config: RuntimeConfig):
    lifecycle.update_config(new_config)
    return {
        "ok": True,
        "message": "تم تحديث إعدادات التشغيل والراحة وحدود الضغط وحفظها بنجاح.",
        "config": lifecycle.config.dict(),
    }


@app.post("/api/predict")
async def predict_endpoint(req: PredictionRequest):
    lifecycle.total_requests += 1
    state_meta = lifecycle.evaluate_lifecycle_state(is_incoming_prediction=True)

    game = req.game_type.upper().strip()
    clean_hist = [normalize_item_name(h) for h in req.history if h and h.strip()]
    room_key = f"{game}:{req.room_id or 'default'}"

    # دمج وحفظ سجل الغرفة لمنع ضياع السجل عند الراحة أو انقطاع اتصال المستخدم
    if clean_hist:
        lifecycle.room_histories[room_key] = clean_hist[-100:]
    elif room_key in lifecycle.room_histories:
        clean_hist = lifecycle.room_histories[room_key]

    # حماية من تكرار المهام المتطابقة (Task Deduplication)
    payload_signature = hashlib.sha256(
        f"{game}|{room_key}|{'|'.join(clean_hist)}|{req.coins}".encode("utf-8")
    ).hexdigest()

    now = time.time()
    if not req.force_refresh and payload_signature in lifecycle.dedup_cache:
        cached_time, cached_resp = lifecycle.dedup_cache[payload_signature]
        if now - cached_time <= lifecycle.config.dedup_window_seconds:
            cached_copy = dict(cached_resp)
            cached_copy["deduplicated"] = True
            return cached_copy

    # تنظيم المهام المتزامنة عند ارتفاع الضغط (Concurrency Control)
    async with lifecycle.semaphore:
        lifecycle.active_tasks += 1
        try:
            min_s = lifecycle.config.min_sample_for_pattern
            if "FARM" in game:
                res = run_farm_engine(clean_hist, min_s)
            elif "POKER" in game:
                res = run_poker_engine(clean_hist, min_s)
            elif "DRAGON" in game:
                res = run_dragon_tiger_engine(clean_hist, min_s)
            else:
                raise HTTPException(status_code=400, detail="نوع اللعبة غير مدعوم.")

            is_warn, warn_text = detect_streak_anomaly(clean_hist)
            pred_html = res["prediction_html"]

            # إضافة شريط حالة دورة التشغيل الحقيقية للسيرفر
            pred_html += (
                f"<br><span style='font-size:9px;color:#fbbf24;'>"
                f"⚙️ [حالة الخدمة]: {state_meta['mode_label']} | مراجعة الـ 7س القادمة بعد: {state_meta['next_checkpoint_hours']}س"
                f"</span>"
            )

            if is_warn:
                pred_html = (
                    f"<span style='color:#fca5a5;'>{warn_text}</span>"
                    f"<hr style='border-color:#ef4444;margin:4px 0;'>{pred_html}"
                )

            # حساب إدارة المحفظة المحافظة بناءً على حجم العينة ووجود تحذير تذبذب
            if len(clean_hist) < min_s:
                bet_ratio = 0.03
            elif is_warn:
                bet_ratio = 0.02
            else:
                bet_ratio = 0.06

            safe_bet = int(req.coins * bet_ratio) if req.coins > 0 else 0

            response_payload = {
                "is_rigged": is_warn,
                "server_mode": state_meta["mode"],
                "server_mood": state_meta["mode_label"],
                "confidence": res["confidence"],
                "prediction": pred_html,
                "safe_bet": safe_bet,
                "engine_log": f"{res['engine_log']} | Mode={state_meta['mode']} | RPM={state_meta['rpm']}",
                "sample_size": len(clean_hist),
                "measured_accuracy": res["measured_accuracy"],
                "baseline_accuracy": res["baseline_accuracy"],
                "deduplicated": False,
            }

            lifecycle.dedup_cache[payload_signature] = (now, response_payload)
            if lifecycle.total_requests % 10 == 0:
                lifecycle.save_persisted_state()

            return response_payload

        except Exception as exc:
            lifecycle.error_count += 1
            logger.exception(f"Prediction error in {game}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            lifecycle.active_tasks -= 1


@app.get("/")
def root():
    return {
        "status": "Phantom Statistical & Lifecycle Server v120.0 Online",
        "endpoints": ["/api/predict", "/api/health", "/api/config"],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
