import asyncio
import base64
import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
import threading
import hmac
import uuid
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# إعداد نظام تسجيل الأخطاء والأحداث
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("PhantomStatisticalServer")

app = FastAPI(title="BOOM PREMIUM QUILT EDITION API", version="123.2")

# Use explicit origins in production via PHANTOM_CORS_ORIGINS (comma-separated).
# The app authenticates with bearer-style headers, so cross-origin cookies are not needed.
_cors_env = os.environ.get("PHANTOM_CORS_ORIGINS", "*").strip()
_cors_origins = [origin.strip() for origin in _cors_env.split(",") if origin.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Token", "X-Reseller-Token", "Authorization", "apikey"],
)

def _storage_path(env_name: str, default_name: str) -> str:
    """Resolve a writable local path without assuming a Railway Volume exists.

    An explicitly configured /data path is accepted only when /data is an actual
    mount point and writable. Otherwise, safely fall back to a local file in the
    application working directory so a missing Volume cannot crash startup.
    Local fallback is ephemeral on Railway; the startup log makes that explicit.
    """
    configured = os.environ.get(env_name, "").strip()
    candidate = configured or default_name
    parent = os.path.dirname(os.path.abspath(candidate))
    is_data_path = os.path.abspath(candidate).startswith(os.path.abspath("/data") + os.sep) or os.path.abspath(candidate) == os.path.abspath("/data")
    try:
        if is_data_path and not os.path.ismount("/data"):
            raise OSError("/data is not a mounted Volume")
        if not os.path.isdir(parent):
            raise OSError(f"directory does not exist: {parent}")
        if not os.access(parent, os.W_OK | os.X_OK):
            raise OSError(f"directory is not writable: {parent}")
        # Check actual write access, not just permission bits.
        probe = os.path.join(parent, f".phantom_write_test_{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        if configured:
            logger.info("Storage path %s -> %s", env_name, candidate)
        else:
            logger.warning("%s not set; using local ephemeral storage path %s", env_name, candidate)
        return candidate
    except Exception as exc:
        fallback = os.path.abspath(default_name)
        logger.warning("Storage path %s=%r is unavailable (%s); falling back to %s. Data may be lost on redeploy.", env_name, candidate, exc, fallback)
        return fallback


STATE_FILE_PATH = _storage_path("PHANTOM_STATE_FILE", "phantom_server_state.json")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://uimxwispsiqalizeppqw.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_publishable_OxyjnNeEhj3G1MpmgnznIg_I1Hm69p1")
ADMIN_SECRET = os.environ.get("PHANTOM_ADMIN_PASS", "").strip()
LEARNING_FILE_PATH = _storage_path("PHANTOM_LEARNING_FILE", "phantom_learning.json")
# Durable learning storage: use a server-only Supabase secret when configured.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
LEARNING_TABLE = os.environ.get("PHANTOM_LEARNING_TABLE", "phantom_learning_records")
RESELLER_TABLE = os.environ.get("PHANTOM_RESELLER_TABLE", "phantom_reseller_state")
_learning_lock = threading.RLock()
_learning_rate_lock = threading.RLock()
_learning_request_times: Dict[str, List[float]] = {}


def _check_learning_rate_limit(client_ip: str, limit: int = 60, window_seconds: int = 60) -> Tuple[bool, int]:
    """Lightweight per-process abuse guard for public client-reported outcomes."""
    now = time.time()
    with _learning_rate_lock:
        recent = [stamp for stamp in _learning_request_times.get(client_ip, []) if now - stamp < window_seconds]
        if len(recent) >= limit:
            retry_after = max(1, int(window_seconds - (now - recent[0])))
            _learning_request_times[client_ip] = recent
            return False, retry_after
        recent.append(now)
        _learning_request_times[client_ip] = recent
        # Bound memory if many unique addresses hit the endpoint.
        if len(_learning_request_times) > 2000:
            for key, stamps in list(_learning_request_times.items()):
                if not stamps or now - stamps[-1] >= window_seconds:
                    _learning_request_times.pop(key, None)
        return True, 0


def _learning_db_request(path: str, method: str = "GET", payload: Any = None) -> Tuple[int, Any]:
    """Use the Supabase service-role key only on the backend; never expose it to the client."""
    if not SUPABASE_SERVICE_ROLE_KEY:
        return 503, {"error": "SUPABASE_SERVICE_ROLE_KEY is not configured"}
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if method in ("POST", "PATCH"):
        headers["Prefer"] = "return=representation"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else []
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        logger.error("Learning database HTTP %s: %s", exc.code, detail[:500])
        return exc.code, {"error": detail}
    except Exception as exc:
        logger.error("Learning database request failed: %s", exc)
        return 503, {"error": str(exc)}


def _load_learning_from_supabase() -> Optional[Dict[str, Any]]:
    if not SUPABASE_SERVICE_ROLE_KEY:
        return None
    path = f"{LEARNING_TABLE}?select=record&order=created_at.desc&limit=10000"
    status, rows = _learning_db_request(path)
    if status != 200 or not isinstance(rows, list):
        logger.warning("Could not load learning records from Supabase; using local file fallback")
        return None
    records = [row.get("record") for row in reversed(rows) if isinstance(row, dict) and isinstance(row.get("record"), dict)]
    logger.info("Loaded %s learning records from Supabase", len(records))
    return {"records": records[-10000:]}


def _persist_learning_record(record: Dict[str, Any]) -> bool:
    if not SUPABASE_SERVICE_ROLE_KEY:
        return False
    payload = {"id": record["id"], "created_at": record["timestamp"], "record": record}
    status, result = _learning_db_request(LEARNING_TABLE, "POST", payload)
    if status not in (200, 201):
        logger.error("Learning record was not saved to Supabase (HTTP %s)", status)
        return False
    return True


def _load_learning_store() -> Dict[str, Any]:
    try:
        with _learning_lock:
            if os.path.exists(LEARNING_FILE_PATH):
                with open(LEARNING_FILE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("records", []), list):
                    return {"records": data.get("records", [])[-10000:]}
    except Exception as exc:
        logger.warning("Learning store load failed: %s", exc)
    return {"records": []}


def _save_learning_store(store: Dict[str, Any]) -> None:
    try:
        directory = os.path.dirname(os.path.abspath(LEARNING_FILE_PATH))
        os.makedirs(directory, exist_ok=True)
        temp_path = LEARNING_FILE_PATH + ".tmp"
        with _learning_lock:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({"records": store.get("records", [])[-10000:]}, f, ensure_ascii=False)
            os.replace(temp_path, LEARNING_FILE_PATH)
    except Exception as exc:
        logger.error("Learning store save failed: %s", exc)


learning_store = _load_learning_from_supabase() or _load_learning_store()


# =====================================================================
# 1. نماذج البيانات والإعدادات ومصادقة الأدمن (محفوظة كما هي)
# =====================================================================

class RuntimeConfig(BaseModel):
    review_cycle_hours: float = Field(default=7.0, ge=0.5, le=72.0)
    idle_timeout_seconds: int = Field(default=600, ge=60, le=7200)
    low_load_rest_minutes: int = Field(default=15, ge=1, le=180)
    max_concurrent_tasks: int = Field(default=12, ge=1, le=100)
    high_load_threshold_rpm: int = Field(default=45, ge=5, le=1000)
    min_sample_for_pattern: int = Field(default=6, ge=3, le=50)
    dedup_window_seconds: int = Field(default=2, ge=0, le=30)


class AdminLoginRequest(BaseModel):
    password: str
    trigger_clicks: Optional[int] = 6


class LicenseVerifyRequest(BaseModel):
    key_code: str
    device_token: str


class ResellerCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)
    initial_points: int = Field(default=0, ge=0, le=100000000)


class ResellerLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class ResellerPointsRequest(BaseModel):
    amount: int = Field(ge=-100000000, le=100000000)
    reason: Optional[str] = Field(default="تعديل رصيد بواسطة المالك", max_length=200)
    confirmation_password: str = Field(min_length=1, max_length=128)


class ResellerStatusRequest(BaseModel):
    active: bool
    confirmation_password: str = Field(min_length=1, max_length=128)


class ResellerUpdateRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=80)
    confirmation_password: str = Field(min_length=1, max_length=128)


class ResellerCredentialsRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=40)
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)
    confirmation_password: str = Field(min_length=1, max_length=128)


class ResellerKeyStatusRequest(BaseModel):
    status: str = Field(pattern=r"^(active|banned)$")
    confirmation_password: str = Field(min_length=1, max_length=128)


class ResellerKeyRequest(BaseModel):
    key_code: str = Field(min_length=3, max_length=80)
    duration_minutes: int


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    reseller_id: Optional[str] = Field(default=None, max_length=100)


class ChatSettingRequest(BaseModel):
    enabled: bool
    confirmation_password: str = Field(min_length=1, max_length=128)


class LearningOutcomeRequest(BaseModel):
    game_type: str = Field(min_length=3, max_length=20)
    room_id: Optional[str] = Field(default="default", max_length=100)
    actual: str = Field(min_length=1, max_length=100)
    predicted: List[str] = Field(min_length=1, max_length=5)
    history_before: List[str] = Field(default_factory=list, max_length=100)
    probabilities: Dict[str, float] = Field(default_factory=dict, max_length=20)
    source: str = Field(default="client", max_length=20)


class PredictionRequest(BaseModel):
    game_type: str = Field(min_length=3, max_length=20)
    room_id: Optional[str] = Field(default="default", max_length=100)
    history: List[str] = Field(default_factory=list, max_length=100)
    coins: int = Field(default=0, ge=0, le=1_000_000_000_000)
    client_hour: Optional[int] = Field(default=None, ge=0, le=23)
    client_minute: Optional[int] = Field(default=None, ge=0, le=59)
    force_refresh: Optional[bool] = False
    poker_primary_ratio: Optional[float] = Field(default=0.65, ge=0.50, le=0.90)
    poker_alt_ratio: Optional[float] = Field(default=0.35, ge=0.10, le=0.50)


# =====================================================================
# 2. مساعد اتصال قاعدة البيانات الخلفي (Supabase Proxy Helper - محفوظ كما هو)
# =====================================================================

def supabase_server_request(path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    # Prefer the server-only service-role key for privileged license/admin operations.
    # Never place this key in frontend HTML or return it from an API response.
    db_key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY
    headers = {
        "apikey": db_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if db_key.startswith("eyJ") or db_key == SUPABASE_SERVICE_ROLE_KEY and bool(SUPABASE_SERVICE_ROLE_KEY):
        headers["Authorization"] = f"Bearer {db_key}"
    if method in ("POST", "PATCH"):
        headers["Prefer"] = "return=representation"

    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else []
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        logger.error(f"Supabase HTTP {e.code}: {err_body}")
        return e.code, {"error": err_body}
    except Exception as e:
        logger.error(f"Supabase connection error: {e}")
        return 503, {"error": str(e)}


# =====================================================================
# 3. مدير التشغيل والأمان وسجل الأنشطة الإدارية (محفوظ كما هو)
# =====================================================================

class ServerLifecycleManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.config = RuntimeConfig()
        self.start_time = time.time()
        self.last_checkpoint_time = time.time()
        self.last_request_time = time.time()
        self.operational_mode = "ACTIVE"
        self.total_requests = 0
        self.error_count = 0
        self.checkpoints_completed = 0
        self.active_tasks = 0
        self.recent_request_timestamps: List[float] = []
        self.dedup_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self.room_histories: Dict[str, List[str]] = {}
        self.game_request_counts: Dict[str, int] = {"DRAGON": 0, "FARM": 0, "POKER": 0}
        self.audit_logs: List[Dict[str, str]] = []
        self.error_logs: List[Dict[str, str]] = []
        self.failed_admin_attempts: Dict[str, List[float]] = {}
        self.active_admin_tokens: Dict[str, float] = {}
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        self.load_persisted_state()

    def record_audit_event(self, event_type: str, details: str, severity: str = "INFO"):
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "type": event_type,
            "severity": severity,
            "details": details,
        }
        self.audit_logs.append(entry)
        if len(self.audit_logs) > 80:
            self.audit_logs = self.audit_logs[-80:]

    def record_error_event(self, source: str, message: str):
        self.error_count += 1
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "source": source,
            "message": message[:160],
        }
        self.error_logs.append(entry)
        if len(self.error_logs) > 50:
            self.error_logs = self.error_logs[-50:]

    def check_admin_rate_limit(self, client_ip: str) -> Tuple[bool, int]:
        now = time.time()
        attempts = [t for t in self.failed_admin_attempts.get(client_ip, []) if now - t < 300.0]
        self.failed_admin_attempts[client_ip] = attempts
        if len(attempts) >= 5:
            wait_secs = int(300.0 - (now - attempts[0]))
            return False, max(1, wait_secs)
        return True, 0

    def register_admin_failure(self, client_ip: str):
        now = time.time()
        attempts = [t for t in self.failed_admin_attempts.get(client_ip, []) if now - t < 300.0]
        attempts.append(now)
        self.failed_admin_attempts[client_ip] = attempts

    def create_admin_session(self, client_ip: str) -> str:
        self.failed_admin_attempts.pop(client_ip, None)
        token = secrets.token_hex(24)
        self.active_admin_tokens[token] = time.time() + 14400.0
        return token

    def verify_admin_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        now = time.time()
        self.active_admin_tokens = {k: exp for k, exp in self.active_admin_tokens.items() if exp > now}
        return token in self.active_admin_tokens

    def load_persisted_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.total_requests = data.get("total_requests", 0)
                self.checkpoints_completed = data.get("checkpoints_completed", 0)
                self.room_histories = data.get("room_histories", {})
                self.game_request_counts = data.get("game_request_counts", {"DRAGON": 0, "FARM": 0, "POKER": 0})
                self.audit_logs = data.get("audit_logs", [])[-80:]
                saved_cfg = data.get("config")
                if saved_cfg:
                    self.config = RuntimeConfig(**saved_cfg)
                    self.semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
                logger.info("Persisted state loaded successfully.")
            except Exception as exc:
                logger.error(f"Failed to load state file: {exc}")

    def save_persisted_state(self):
        try:
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(time.time() - self.start_time, 2),
                "total_requests": self.total_requests,
                "error_count": self.error_count,
                "checkpoints_completed": self.checkpoints_completed,
                "config": self.config.dict(),
                "room_histories": self.room_histories,
                "game_request_counts": self.game_request_counts,
                "audit_logs": self.audit_logs[-80:],
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"State persistence error: {exc}")

    def update_config(self, new_cfg: RuntimeConfig):
        self.config = new_cfg
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)
        self.record_audit_event("CONFIG_UPDATE", f"Cycle={new_cfg.review_cycle_hours}h, MaxTasks={new_cfg.max_concurrent_tasks}")
        self.save_persisted_state()

    def get_requests_per_minute(self) -> int:
        now = time.time()
        self.recent_request_timestamps = [t for t in self.recent_request_timestamps if now - t <= 60.0]
        return len(self.recent_request_timestamps)

    def evaluate_lifecycle_state(self, is_incoming_prediction: bool = False) -> Dict[str, Any]:
        now = time.time()
        rpm = self.get_requests_per_minute()
        hours_since_checkpoint = (now - self.last_checkpoint_time) / 3600.0
        idle_seconds = now - self.last_request_time

        if hours_since_checkpoint >= self.config.review_cycle_hours:
            self.operational_mode = "MAINTENANCE_CHECKPOINT"
            self.save_persisted_state()
            self.dedup_cache = {
                k: v for k, v in self.dedup_cache.items() if now - v[0] <= self.config.dedup_window_seconds
            }
            self.last_checkpoint_time = now
            self.checkpoints_completed += 1
            self.record_audit_event("CHECKPOINT_7H", f"Completed checkpoint #{self.checkpoints_completed}")
            logger.info(f"7-Hour Checkpoint #{self.checkpoints_completed} completed without data loss.")

        if is_incoming_prediction:
            self.last_request_time = now
            self.recent_request_timestamps.append(now)
            rpm = self.get_requests_per_minute()
            if rpm >= self.config.high_load_threshold_rpm:
                self.operational_mode = "HIGH_LOAD_THROTTLED"
            else:
                self.operational_mode = "ACTIVE"
        else:
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

        mode_arabic = {
            "ACTIVE": "🟢 السيرفر نشط (تشغيل قياسي)",
            "LOW_POWER_REST": "🟠 وضع توفير الموارد (يستأنف تلقائياً عند الطلب)",
            "HIGH_LOAD_THROTTLED": "🟡 ضغط مرتفع (تنظيم المهام المتزامنة نشط)",
            "MAINTENANCE_CHECKPOINT": "🔵 نقطة مراجعة الـ 7 ساعات (حفظ البيانات مكتمل)",
        }.get(self.operational_mode, "🟢 نشط")

        return {
            "mode": self.operational_mode,
            "mode_label": mode_arabic,
            "rpm": rpm,
            "active_tasks": self.active_tasks,
            "max_concurrent_tasks": self.config.max_concurrent_tasks,
            "uptime_hours": round((now - self.start_time) / 3600.0, 2),
            "next_checkpoint_hours": round(next_checkpoint_in_hours, 2),
            "checkpoints_completed": self.checkpoints_completed,
            "total_requests": self.total_requests,
            "error_count": self.error_count,
            "game_request_counts": self.game_request_counts,
        }


lifecycle = ServerLifecycleManager(STATE_FILE_PATH)

# بيانات الوكلاء: Supabase هو التخزين الدائم الأساسي عند ضبط مفتاح الخدمة؛ JSON بديل محلي فقط.
RESELLER_STATE_FILE = _storage_path("PHANTOM_RESELLER_FILE", "phantom_resellers.json")
reseller_lock = threading.RLock()
reseller_sessions: Dict[str, Dict[str, Any]] = {}


def _normalize_reseller_store(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("resellers", {})
    data.setdefault("key_history", [])
    data.setdefault("audit_log", [])
    data.setdefault("chat_enabled", True)
    data.setdefault("chat_messages", [])
    if not isinstance(data["resellers"], dict):
        data["resellers"] = {}
    if not isinstance(data["key_history"], list):
        data["key_history"] = []
    if not isinstance(data["audit_log"], list):
        data["audit_log"] = []
    if not isinstance(data.get("chat_enabled"), bool):
        data["chat_enabled"] = True
    if not isinstance(data.get("chat_messages"), list):
        data["chat_messages"] = []
    return data


def _load_reseller_file() -> Dict[str, Any]:
    try:
        with open(RESELLER_STATE_FILE, "r", encoding="utf-8") as f:
            return _normalize_reseller_store(json.load(f))
    except FileNotFoundError:
        return {"resellers": {}, "key_history": [], "audit_log": [], "chat_enabled": True, "chat_messages": []}
    except Exception as exc:
        logger.error("Reseller local store load failed: %s", exc)
        return {"resellers": {}, "key_history": [], "audit_log": [], "chat_enabled": True, "chat_messages": []}


def _load_reseller_store() -> Dict[str, Any]:
    # Load the persistent database first. Never silently prefer ephemeral JSON when DB is configured.
    if SUPABASE_SERVICE_ROLE_KEY:
        status, rows = _learning_db_request(f"{RESELLER_TABLE}?id=eq.primary&select=state&limit=1")
        if status == 200 and isinstance(rows, list):
            if rows and isinstance(rows[0].get("state"), dict):
                logger.info("Loaded reseller accounts from durable Supabase table %s", RESELLER_TABLE)
                return _normalize_reseller_store(rows[0]["state"])
            logger.info("No reseller state row found in Supabase yet; starting empty")
            return {"resellers": {}, "key_history": [], "audit_log": [], "chat_enabled": True, "chat_messages": []}
        logger.error("Could not load reseller state from Supabase (HTTP %s); refusing to use ephemeral reseller data", status)
        # A bad/missing table must not look like a successful empty persistent store.
        raise RuntimeError(f"Reseller database unavailable (HTTP {status}); check Supabase table and service key")
    logger.warning("SUPABASE_SERVICE_ROLE_KEY not configured; reseller data uses local storage and may be lost on redeploy")
    return _load_reseller_file()


reseller_store = _load_reseller_store()
_last_persisted_reseller_store = copy.deepcopy(reseller_store)


def _save_reseller_store() -> None:
    global reseller_store, _last_persisted_reseller_store
    snapshot = copy.deepcopy(reseller_store)
    if SUPABASE_SERVICE_ROLE_KEY:
        # Upsert one JSONB state row. The table must be created using the supplied SQL setup file.
        url = f"{SUPABASE_URL}/rest/v1/{RESELLER_TABLE}?on_conflict=id"
        headers = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        payload = [{"id": "primary", "state": snapshot, "updated_at": datetime.now(timezone.utc).isoformat()}]
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201, 204):
                    raise RuntimeError(f"Supabase reseller state save returned HTTP {resp.status}")
        except Exception as exc:
            # Roll back in-memory changes; don't claim a write succeeded if durable storage failed.
            logger.error("Durable reseller save failed; rolling back in-memory state: %s", exc)
            reseller_store = copy.deepcopy(_last_persisted_reseller_store)
            raise RuntimeError("تعذر حفظ بيانات الوكلاء في التخزين الدائم؛ لم يتم اعتماد التغيير. تحقق من إعدادات Supabase.") from exc
        _last_persisted_reseller_store = copy.deepcopy(snapshot)
    else:
        logger.warning("Saving reseller state to local JSON only; this is not durable on Railway")
    directory = os.path.dirname(os.path.abspath(RESELLER_STATE_FILE))
    os.makedirs(directory, exist_ok=True)
    tmp_path = RESELLER_STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, RESELLER_STATE_FILE)

def _hash_reseller_password(password: str, salt_hex: Optional[str] = None) -> Tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240000).hex()
    return salt.hex(), digest

# VIP tiers count only lifetime points credited by the owner; gifts and spending do not count.
VIP_TIERS = [
    {"name": "Bronze", "name_ar": "برونزي", "threshold": 100, "gift": 10, "medal": "🥉", "badge": "وسام البرونز"},
    {"name": "Silver", "name_ar": "فضي", "threshold": 500, "gift": 40, "medal": "🥈", "badge": "وسام الفضة"},
    {"name": "Gold", "name_ar": "ذهبي", "threshold": 1500, "gift": 120, "medal": "🥇", "badge": "وسام الذهب"},
    {"name": "Platinum", "name_ar": "بلاتيني", "threshold": 5000, "gift": 400, "medal": "💎", "badge": "وسام البلاتين"},
    {"name": "Diamond", "name_ar": "ماسي", "threshold": 15000, "gift": 1500, "medal": "👑", "badge": "وسام الماس"},
]

def _vip_details(row: Dict[str, Any]) -> Dict[str, Any]:
    funded = max(0, int(row.get("funded_points_total", 0)))
    reached = [tier for tier in VIP_TIERS if funded >= tier["threshold"]]
    current = reached[-1] if reached else {"name": "Starter", "name_ar": "مبتدئ", "threshold": 0, "gift": 0, "medal": "♜", "badge": "وسام البداية"}
    nxt = next((tier for tier in VIP_TIERS if funded < tier["threshold"]), None)
    percent = 100 if not nxt else max(0, min(100, round((funded-current["threshold"])*100/max(1,nxt["threshold"]-current["threshold"]))))
    return {"name": current["name"], "name_ar": current["name_ar"], "medal": current["medal"], "badge": current["badge"],
            "threshold": current["threshold"], "gift_points": current["gift"], "next_name": nxt["name"] if nxt else None,
            "next_threshold": nxt["threshold"] if nxt else None, "points_to_next": max(0,nxt["threshold"]-funded) if nxt else 0,
            "progress_percent": percent}

def _award_vip_milestones(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    funded = max(0, int(row.get("funded_points_total", 0)))
    awards = row.setdefault("vip_awards", [])
    known = {int(item.get("threshold", 0)) for item in awards}
    new_awards = []
    for tier in VIP_TIERS:
        if funded >= tier["threshold"] and tier["threshold"] not in known:
            bonus = int(tier["gift"])
            row["points"] = int(row.get("points", 0)) + bonus
            row["vip_bonus_points"] = int(row.get("vip_bonus_points", 0)) + bonus
            timestamp = datetime.now(timezone.utc).isoformat()
            award = {"timestamp": timestamp, "tier": tier["name"], "threshold": tier["threshold"], "bonus_points": bonus, "badge": tier["badge"]}
            awards.append(award)
            row.setdefault("ledger", []).append({"timestamp": timestamp, "type": "VIP_BONUS", "amount": bonus,
                "balance_after": row["points"], "reason": f"مكافأة ترقية VIP {tier['name']} - {tier['badge']}"})
            new_awards.append(award)
    row["vip_awards"] = awards[-20:]
    row["ledger"] = row.get("ledger", [])[-500:]
    return new_awards

def _public_reseller(row: Dict[str, Any]) -> Dict[str, Any]:
    row.setdefault("funded_points_total", 0)
    row.setdefault("vip_bonus_points", 0)
    row.setdefault("vip_awards", [])
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
            "points": int(row.get("points", 0)), "active": bool(row.get("active", True)),
            "created_at": row.get("created_at"), "keys_created": int(row.get("keys_created", 0)),
            "funded_points_total": int(row.get("funded_points_total", 0)), "vip_bonus_points": int(row.get("vip_bonus_points", 0)),
            "vip_awards": row.get("vip_awards", []), "vip": _vip_details(row),
            "access_policy": "lifetime_until_owner_deactivates"}

def _require_sensitive_confirmation(password: str) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="تأكيد العمليات الحساسة غير متاح: كلمة مرور المالك غير مضبوطة على السيرفر.")
    if not hmac.compare_digest(password.strip(), ADMIN_SECRET):
        lifecycle.record_audit_event("SENSITIVE_ACTION_REJECTED", "Invalid owner confirmation password", "WARNING")
        raise HTTPException(status_code=403, detail="تأكيد كلمة مرور المالك غير صحيح؛ لم يتم تنفيذ العملية.")


def _record_owner_audit(action: str, reseller: Dict[str, Any], details: str) -> None:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "actor": "owner", "action": action,
             "reseller_id": reseller.get("id"), "reseller_username": reseller.get("username"), "details": details[:300]}
    reseller_store.setdefault("audit_log", []).append(entry)
    reseller_store["audit_log"] = reseller_store["audit_log"][-2000:]
    lifecycle.record_audit_event(action, f"{reseller.get('username')}: {details[:160]}")


def _require_admin(token: Optional[str]) -> None:
    if not lifecycle.verify_admin_token(token):
        raise HTTPException(status_code=401, detail="جلسة الأدمن غير صالحة أو انتهت. سجّل الدخول مرة أخرى.")

def _get_reseller_session(token: Optional[str]) -> Dict[str, Any]:
    if not token:
        raise HTTPException(status_code=401, detail="سجّل الدخول بحساب الوكيل أولاً.")
    session = reseller_sessions.get(token)
    if not session or session.get("expires_at", 0) <= time.time():
        reseller_sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="انتهت جلسة الوكيل. سجّل الدخول مرة أخرى.")
    reseller = reseller_store["resellers"].get(session["reseller_id"])
    if not reseller or not reseller.get("active", True):
        reseller_sessions.pop(token, None)
        raise HTTPException(status_code=403, detail="حساب الوكيل موقوف.")
    return reseller

lifecycle.record_audit_event("SERVER_BOOT", "Phantom Statistical Server v120.8 initialized.")


# =====================================================================
# 4.1 و 4.2 محركا المزرعة والبوكر (محفوظان كما هما تماماً بدون أي تعديل)
# =====================================================================

FARM_CATALOG: Dict[str, Dict[str, Any]] = {
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
    n = len(history_slice)
    if n == 0:
        return {k: v["prior_prob"] * 100.0 for k, v in FARM_CATALOG.items()}

    counts = Counter(history_slice)
    last_1 = history_slice[-1]
    last_2 = tuple(history_slice[-2:]) if n >= 2 else None

    trans_1 = Counter()
    total_trans_1 = 0
    for i in range(n - 1):
        if history_slice[i] == last_1:
            trans_1[history_slice[i + 1]] += 1
            total_trans_1 += 1

    trans_2 = Counter()
    total_trans_2 = 0
    if last_2 and n >= 3:
        for i in range(n - 2):
            if (history_slice[i], history_slice[i + 1]) == last_2:
                trans_2[history_slice[i + 2]] += 1
                total_trans_2 += 1

    consecutive_repeats = sum(1 for i in range(1, n) if history_slice[i] == history_slice[i - 1])
    observed_repeat_rate = (consecutive_repeats / (n - 1)) if n > 1 else 0.15

    raw_scores: Dict[str, float] = {}
    for item, meta in FARM_CATALOG.items():
        prior = meta["prior_prob"]
        alpha = 4.0
        empirical_bayes = (counts.get(item, 0) + alpha * prior) / (n + alpha)

        p_t1 = (trans_1[item] + 1.5 * prior) / (total_trans_1 + 1.5) if total_trans_1 > 0 else empirical_bayes
        p_t2 = (trans_2[item] + 1.0 * prior) / (total_trans_2 + 1.0) if total_trans_2 > 0 else p_t1

        # لا نرفع احتمال العنصر لمجرد أنه غاب؛ ذلك مغالطة "المتأخر" إذا كانت الجولات مستقلة.
        # نستخدم فقط التكرارات الفعلية والانتقالات المرصودة مع تنعيم يمنع تضخيم العينات الصغيرة.
        combined = (0.40 * empirical_bayes + 0.35 * p_t1 + 0.25 * p_t2)
        raw_scores[item] = combined

    total = sum(raw_scores.values()) or 1.0
    return {k: (v / total) * 100.0 for k, v in raw_scores.items()}


def _select_farm_top5(prob_map: Dict[str, float]) -> List[Tuple[str, float]]:
    x5_items = [k for k, v in FARM_CATALOG.items() if v["group"] == "x5"]
    mult_items = [k for k, v in FARM_CATALOG.items() if v["group"] != "x5"]

    best_x5 = sorted(x5_items, key=lambda k: prob_map[k], reverse=True)[:3]
    best_mult = sorted(mult_items, key=lambda k: prob_map[k], reverse=True)[:2]

    selected_keys = best_x5 + best_mult
    selected_sorted = sorted(selected_keys, key=lambda k: prob_map[k], reverse=True)
    return [(k, prob_map[k]) for k in selected_sorted]


def run_farm_engine(clean_hist: List[str], min_sample: int) -> Dict[str, Any]:
    n = len(clean_hist)
    prob_map = _farm_score_next_step(clean_hist)
    top5 = _select_farm_top5(prob_map)
    top5_coverage = sum(p for _, p in top5)

    backtest_hits = 0
    backtest_trials = 0
    baseline_hits = 0
    default_baseline_set = {
        "🍅 طماطم (×5)", "🌽 ذرة (×5)", "🥬 خس (×5)", "🥕 جزر (×5)", "🌭 سجق (×10)"
    }

    if n >= 4:
        for t in range(3, n):
            past_only = clean_hist[:t]
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
        "farm_details": {
            "top5": [name for name, _ in top5],
            "probabilities": {name: round(float(prob_map[name]), 2) for name in FARM_CATALOG},
            "sample_size": n,
        },
    }


def _poker_score_next_step(norm_hist: List[str]) -> Tuple[Dict[str, float], Dict[str, Any]]:
    chairs = ["🟢 كرسي A", "🔵 كرسي B", "🔴 كرسي C"]
    n = len(norm_hist)
    base_equal_prob = 1.0 / 3.0

    if n == 0:
        return (
            {c: 100.0 / 3.0 for c in chairs},
            {"repeat_rate": 33.3, "switch_rate": 66.7, "current_streak": 0, "mode": "توزيع متكافئ قياسي (33.3% لكل خانة)"},
        )

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
    rep_rate = (repeats / (n - 1)) if n > 1 else base_equal_prob
    switch_rate = 1.0 - rep_rate

    cur_streak = 1
    for i in range(n - 2, -1, -1):
        if norm_hist[i] == last_1:
            cur_streak += 1
        else:
            break

    scores: Dict[str, float] = {}
    for c in chairs:
        emp = (counts.get(c, 0) + 3.0 * base_equal_prob) / (n + 3.0)
        p1 = (trans_1[c] + 1.5 * base_equal_prob) / (t1_total + 1.5) if t1_total > 0 else emp
        p2 = (trans_2[c] + 1.0 * base_equal_prob) / (t2_total + 1.0) if t2_total > 0 else p1

        # لا نكافئ الخانة لمجرد طول فترة غيابها؛ نحتفظ فقط بإشارات الانتقال المرصودة.
        if c == last_1:
            if rep_rate >= 0.36 or (t1_total > 0 and (trans_1[c] / t1_total) >= 0.38):
                flow_factor = 1.06
            elif cur_streak >= 4:
                flow_factor = 0.88
            else:
                flow_factor = 0.94
        else:
            flow_factor = 1.05 if switch_rate >= 0.65 else 0.98

        scores[c] = (0.30 * emp + 0.45 * p1 + 0.25 * p2) * flow_factor

    total = sum(scores.values()) or 1.0
    prob_map = {c: (scores[c] / total) * 100.0 for c in chairs}

    if rep_rate >= 0.40:
        mode_desc = f"ميل للتكرار الطبيعي ({rep_rate*100:.0f}% تكرار)"
    elif switch_rate >= 0.72:
        mode_desc = f"ميل للتناوب بين الخانات ({switch_rate*100:.0f}% تغيير)"
    else:
        mode_desc = f"توزيع متوازن (تكرار {rep_rate*100:.0f}% | تغيير {switch_rate*100:.0f}%)"

    return prob_map, {
        "repeat_rate": round(rep_rate * 100.0, 1),
        "switch_rate": round(switch_rate * 100.0, 1),
        "current_streak": cur_streak,
        "mode": mode_desc,
    }


def run_poker_engine(
    clean_hist: List[str],
    min_sample: int,
    coins: int = 0,
    primary_ratio: float = 0.65,
    alt_ratio: float = 0.35,
) -> Dict[str, Any]:
    norm_hist = []
    for h in clean_hist:
        if "A" in h:
            norm_hist.append("🟢 كرسي A")
        elif "B" in h:
            norm_hist.append("🔵 كرسي B")
        elif "C" in h:
            norm_hist.append("🔴 كرسي C")

    n = len(norm_hist)
    probs, diag = _poker_score_next_step(norm_hist)
    ranked = sorted(probs.items(), key=lambda x: x[1], reverse=True)

    primary_chair, primary_p = ranked[0]
    alt_chair, alt_p = ranked[1]
    third_chair, third_p = ranked[2]
    dual_coverage = primary_p + alt_p

    hits_primary = 0
    hits_alt = 0
    trials = 0
    if n >= 4:
        for t in range(3, n):
            sub_probs, _ = _poker_score_next_step(norm_hist[:t])
            sub_ranked = sorted(sub_probs.items(), key=lambda x: x[1], reverse=True)
            actual = norm_hist[t]
            if actual == sub_ranked[0][0]:
                hits_primary += 1
            elif actual == sub_ranked[1][0]:
                hits_alt += 1
            trials += 1

    primary_acc = (hits_primary / trials * 100.0) if trials > 0 else 0.0
    alt_acc = (hits_alt / trials * 100.0) if trials > 0 else 0.0
    combined_acc = ((hits_primary + hits_alt) / trials * 100.0) if trials > 0 else 0.0

    total_round_stake = int(coins * (0.03 if n < min_sample else 0.06)) if coins > 0 else 0
    norm_p_ratio = max(0.55, min(0.85, primary_ratio / max(0.01, primary_ratio + alt_ratio)))
    norm_a_ratio = 1.0 - norm_p_ratio
    primary_bet = int(round(total_round_stake * norm_p_ratio))
    alt_bet = max(0, total_round_stake - primary_bet)

    last_chair_str = norm_hist[-1] if n > 0 else "لا يوجد"
    is_repeat_prediction = (n > 0 and primary_chair == norm_hist[-1])
    relation_tag = "توقع تكرار طبيعي للخانة" if is_repeat_prediction else "توقع انتقال لخانة مختلفة"

    if n < min_sample:
        status_line = (
            f"⚠️ [عينة أولية {n}/{min_sample} جولات]: القواعد الأساسية متساوية (33.3% لكل خانة). "
            f"لا توجد علاقة تنبؤية جازمة بعد."
        )
        conf_str = f"عينة أولية ({n} جولات)"
    else:
        status_line = (
            f"📊 [اختبار Walk-Forward ({trials} جولة)]: "
            f"إصابة الأساسي={primary_acc:.1f}% | إصابة البديل={alt_acc:.1f}% | التغطية المزدوجة={combined_acc:.1f}%"
        )
        conf_str = f"تغطية الاختبار: {combined_acc:.1f}% (عينة: {n})"

    pred_html = (
        f"<div style='margin-bottom:4px;font-size:10px;color:#cbd5e1;'>"
        f"⚖️ <strong>توزيع الخانات الثلاث:</strong> "
        f"A: {probs['🟢 كرسي A']:.1f}% | B: {probs['🔵 كرسي B']:.1f}% | C: {probs['🔴 كرسي C']:.1f}%"
        f"</div>"
        f"<div style='background:#052e16;border:1px solid #10b981;padding:5px;border-radius:5px;margin-bottom:4px;'>"
        f"🎯 <strong>[1] البوكر الأساسي (الخانة الأولى):</strong> {primary_chair} — احتمال: <strong>{primary_p:.1f}%</strong> ({relation_tag})<br>"
        f"<span style='font-size:9px;color:#6ee7b7;'>💰 الرهان الأساسي المقترح ({int(norm_p_ratio*100)}%): {primary_bet} كوينز | دقة الأساسي تاريخياً: {primary_acc:.1f}%</span>"
        f"</div>"
        f"<div style='background:#172554;border:1px solid #3b82f6;padding:5px;border-radius:5px;margin-bottom:4px;'>"
        f"🛡️ <strong>[2] البوكر البديل المستقل (خانة التغطية الثانية):</strong> {alt_chair} — احتمال مستقل: <strong>{alt_p:.1f}%</strong><br>"
        f"<span style='font-size:9px;color:#93c5fd;'>💰 الرهان البديل الأقل ({int(norm_a_ratio*100)}%): {alt_bet} كوينز | دقة البديل تاريخياً: {alt_acc:.1f}% | مستبعد: {third_chair} ({third_p:.1f}%)</span>"
        f"</div>"
        f"<span style='font-size:9px;color:#93c5fd;'>{status_line} | آخر نتيجة مسجلة: {last_chair_str}</span>"
    )

    log_msg = (
        f"[POKER-ENGINE] N={n} | Primary={primary_chair}({primary_p:.1f}%) | "
        f"Alt={alt_chair}({alt_p:.1f}%) | DualHit={combined_acc:.1f}%"
    )

    return {
        "prediction_html": pred_html,
        "confidence": conf_str,
        "engine_log": log_msg,
        "measured_accuracy": round(combined_acc, 1) if trials > 0 else None,
        "baseline_accuracy": 66.7,
        "poker_details": {
            "seat_probabilities": {
                "A": round(probs["🟢 كرسي A"], 1),
                "B": round(probs["🔵 كرسي B"], 1),
                "C": round(probs["🔴 كرسي C"], 1),
            },
            "primary": {
                "chair": primary_chair,
                "probability": round(primary_p, 1),
                "bet": primary_bet,
                "ratio_percent": int(round(norm_p_ratio * 100)),
                "historical_hit_rate": round(primary_acc, 1),
                "relation": relation_tag,
            },
            "alternate": {
                "chair": alt_chair,
                "probability": round(alt_p, 1),
                "bet": alt_bet,
                "ratio_percent": int(round(norm_a_ratio * 100)),
                "historical_hit_rate": round(alt_acc, 1),
            },
            "excluded": {
                "chair": third_chair,
                "probability": round(third_p, 1),
            },
            "dual_coverage_prob": round(dual_coverage, 1),
            "total_stake": total_round_stake,
            "diagnostics": diag,
            "sample_size": n,
        },
    }


# =====================================================================
# 4.3 إصلاح جذري لمحرك Dragon Tiger وفق قواعد اللعبة الفعلية في HiFami:
# - التعرف الصحيح على Dragon (🐉 تنين) و Tiger (🐅 تايجر) و Tie (🤝 تعادل)
# - إزالة النسخ التلقائي لآخر نتيجة مدخلة وإزالة العكس الإجباري
# - تحليل تتابعي حقيقي (Order-1 & Order-2 Markov + Window Balance + Streak/Chop)
# - معالجة Tie كنتيجة مستقلة ونادرة (~7.46%) داخل التسلسل دون حذفها أو تضخيمها
# =====================================================================

def _normalize_dt_symbol(raw_str: str) -> Optional[str]:
    if not raw_str:
        return None
    s = str(raw_str).strip()
    upper_s = s.upper()
    if "تنين" in s or "DRAGON" in upper_s or "🐉" in s:
        return "🐉 تنين"
    if "تايجر" in s or "نمر" in s or "TIGER" in upper_s or "🐅" in s:
        return "🐅 تايجر"
    if "تعادل" in s or "TIE" in upper_s or "🤝" in s:
        return "🤝 تعادل"
    return None


def _dt_score_next_step(norm_hist: List[str]) -> Tuple[str, Dict[str, float], str, str, str]:
    """
    حساب التوزيع الاحتمالي للجولة القادمة في Dragon Tiger استناداً إلى السجل الفعلي:
    - القواعد المرجعية (8-Deck Shoe): Dragon = 46.27%، Tiger = 46.27%، Tie = 7.46%.
    - يقرأ السجل بالترتيب الزمني الدقيق دون حذف Tie من مصفوفة الانتقال.
    - لا ينسخ آخر نتيجة تلقائياً عند غياب النمط، ولا يعكسها إجبارياً.
    """
    priors = {"🐉 تنين": 0.4627, "🐅 تايجر": 0.4627, "🤝 تعادل": 0.0746}
    outcomes = ["🐉 تنين", "🐅 تايجر", "🤝 تعادل"]
    n = len(norm_hist)

    if n == 0:
        return (
            "⚖️ متكافئ (انتظار بيانات السجل)",
            {"🐉 تنين": 46.3, "🐅 تايجر": 46.3, "🤝 تعادل": 7.4},
            "لا توجد جولات مسجلة بعد — القواعد القياسية متكافئة (46.3% تنين | 46.3% تايجر | 7.4% تعادل)",
            "احتمال التعادل المرجعي: 7.4% (حدث نادر نسبياً — مرة كل ~13.4 جولة)",
            "بانتظار إدخال الجولات لتحليل مصفوفة الانتقال",
        )

    counts = Counter(norm_hist)
    last_1 = norm_hist[-1]
    last_2 = tuple(norm_hist[-2:]) if n >= 2 else None

    # 1. مصفوفة الانتقال من الدرجة الأولى على التسلسل الحقيقي الكامل (شاملة Tie)
    trans_1 = Counter()
    t1_total = 0
    for i in range(n - 1):
        if norm_hist[i] == last_1:
            trans_1[norm_hist[i + 1]] += 1
            t1_total += 1

    # 2. مصفوفة الانتقال من الدرجة الثانية (ماذا ظهر تاريخياً بعد آخر زوج من النتائج؟)
    trans_2 = Counter()
    t2_total = 0
    if last_2 and n >= 3:
        for i in range(n - 2):
            if (norm_hist[i], norm_hist[i + 1]) == last_2:
                trans_2[norm_hist[i + 2]] += 1
                t2_total += 1

    # 3. تحليل التكرار مقابل التناوب في التسلسل الزمني الفعلي
    adj_repeats = 0
    adj_switches = 0
    for i in range(1, n):
        if norm_hist[i] in ("🐉 تنين", "🐅 تايجر") and norm_hist[i - 1] in ("🐉 تنين", "🐅 تايجر"):
            if norm_hist[i] == norm_hist[i - 1]:
                adj_repeats += 1
            else:
                adj_switches += 1

    total_adj_pairs = adj_repeats + adj_switches
    repeat_rate = (adj_repeats / total_adj_pairs) if total_adj_pairs > 0 else 0.50
    switch_rate = (adj_switches / total_adj_pairs) if total_adj_pairs > 0 else 0.50

    # حساب طول السلسلة الحالية لآخر نتيجة فعلية (سواء تنين أو تايجر أو تعادل)
    cur_streak = 1
    for i in range(n - 2, -1, -1):
        if norm_hist[i] == last_1:
            cur_streak += 1
        else:
            break

    # 4. توازن النافذة القصيرة (آخر 8 جولات) لرصد التشبع أو النقص الإحصائي
    recent_window = norm_hist[-8:]
    rec_d = recent_window.count("🐉 تنين")
    rec_t = recent_window.count("🐅 تايجر")
    rec_decisive = max(1, rec_d + rec_t)
    d_window_share = rec_d / rec_decisive
    t_window_share = rec_t / rec_decisive

    # 5. فجوة غياب التعادل النادر (متوسط دورته الطبيعية ~13.4 جولة)
    if "🤝 تعادل" in norm_hist:
        last_tie_pos = len(norm_hist) - 1 - norm_hist[::-1].index("🤝 تعادل")
        tie_gap = (n - 1) - last_tie_pos
    else:
        tie_gap = n

    scores: Dict[str, float] = {}
    for outcome in outcomes:
        prior_p = priors[outcome]

        if outcome == "🤝 تعادل":
            # تنعيم بايزي قوي للتعادل يمنع المبالغة في توقعه مع الحفاظ على استقلاله
            alpha_tie = 14.0
            emp_tie = (counts.get(outcome, 0) + alpha_tie * prior_p) / (n + alpha_tie)
            p1_tie = (trans_1[outcome] + 4.0 * prior_p) / (t1_total + 4.0) if t1_total > 0 else emp_tie
            p2_tie = (trans_2[outcome] + 3.0 * prior_p) / (t2_total + 3.0) if t2_total > 0 else p1_tie
            base_tie = 0.40 * emp_tie + 0.35 * p1_tie + 0.25 * p2_tie

            # غياب التعادل وحده لا يزيد احتماله؛ نستخدم التكرارات والانتقالات الفعلية فقط.
            immediate_tie_mod = 0.78 if (last_1 == "🤝 تعادل" and trans_1["🤝 تعادل"] == 0) else 1.0
            scores[outcome] = base_tie * immediate_tie_mod
        else:
            # حساب التنين والتايجر بدون أي انحياز مسبق لنسخ أو عكس النتيجة الأخيرة
            alpha_main = 6.0
            emp_main = (counts.get(outcome, 0) + alpha_main * prior_p) / (n + alpha_main)
            p1_main = (trans_1[outcome] + 2.5 * prior_p) / (t1_total + 2.5) if t1_total > 0 else emp_main
            p2_main = (trans_2[outcome] + 1.8 * prior_p) / (t2_total + 1.8) if t2_total > 0 else p1_main

            # وزن الانتقال الشرطي الفعلي من السجل يمثل المحرك الأساسي
            base_main = 0.28 * emp_main + 0.42 * p1_main + 0.30 * p2_main

            # تقييم سلوك السلاسل (Streak) مقابل التناوب (Chop) فقط عند توفر انتقالات فعلية كافية (total_adj_pairs >= 2)
            pattern_mod = 1.0
            if last_1 in ("🐉 تنين", "🐅 تايجر") and total_adj_pairs >= 2:
                if repeat_rate >= 0.58:
                    # البيانات الفعلية في السجل تُثبت ميل الطاولة لتكرار نفس النتيجة (Streak)
                    if outcome == last_1:
                        pattern_mod = 1.0 + min(0.16, (repeat_rate - 0.50) * 0.40)
                    else:
                        pattern_mod = 0.94
                elif switch_rate >= 0.58:
                    # البيانات الفعلية في السجل تُثبت ميل الطاولة للتناوب العكسي (Chop)
                    if outcome != last_1:
                        pattern_mod = 1.0 + min(0.16, (switch_rate - 0.50) * 0.40)
                    else:
                        pattern_mod = 0.94

            # تصحيح التوازن الإحصائي الطفيف عندما لا يكون هناك نمط سلاسل مهيمن
            balance_mod = 1.0
            if n >= 2 and repeat_rate < 0.58:
                outcome_share = d_window_share if outcome == "🐉 تنين" else t_window_share
                # الكفة الأقل ظهوراً في النافذة الأخيرة تحصل على تصحيح توازن إحصائي طفيف
                balance_mod = 1.0 + ((0.50 - outcome_share) * 0.08)

            scores[outcome] = base_main * pattern_mod * balance_mod

    total_s = sum(scores.values()) or 1.0
    prob_map = {k: (v / total_s) * 100.0 for k, v in scores.items()}

    d_prob = prob_map["🐉 تنين"]
    t_prob = prob_map["🐅 تايجر"]
    tie_prob = prob_map["🤝 تعادل"]
    diff_dt = abs(d_prob - t_prob)

    # تحديد التوقع المستقبلي وسبب القرار بشفافية إحصائية
    if tie_prob >= 20.0 and tie_prob > max(d_prob, t_prob):
        predicted = "🤝 تعادل"
        decision_reason = f"وزن التعادل المحسوب من التكرارات والانتقالات المرصودة ({tie_prob:.1f}%)؛ الغياب وحده لا يرفع الاحتمال"
    elif n < 2 or diff_dt < 0.35:
        # في حال تسجيل جولة واحدة فقط أو تساوي الكفتين تماماً، لا ننسخ ولا نعكس عشوائياً بل نعرض الأوزان المتقاربة بوضوح
        predicted = "🐉 تنين" if d_prob >= t_prob else "🐅 تايجر"
        decision_reason = (
            f"البيانات الحالية ({n} جولة) تُظهر تقارباً إحصائياً شبه متكافئ "
            f"(فارق {diff_dt:.2f}% فقط) — لا يوجد نمط حاسم بعد"
        )
    elif d_prob > t_prob:
        predicted = "🐉 تنين"
        if last_1 == "🐉 تنين":
            decision_reason = f"السجل يدعم استمرار السلسلة (تنين) بناءً على معدل التكرار ({repeat_rate*100:.0f}%) ومصفوفة الانتقال"
        else:
            decision_reason = f"ترجيح الانتقال إلى (تنين) بناءً على معدل التناوب ({switch_rate*100:.0f}%) ومصفوفة الانتقال بعد ({last_1})"
    else:
        predicted = "🐅 تايجر"
        if last_1 == "🐅 تايجر":
            decision_reason = f"السجل يدعم استمرار السلسلة (تايجر) بناءً على معدل التكرار ({repeat_rate*100:.0f}%) ومصفوفة الانتقال"
        else:
            decision_reason = f"ترجيح الانتقال إلى (تايجر) بناءً على معدل التناوب ({switch_rate*100:.0f}%) ومصفوفة الانتقال بعد ({last_1})"

    if total_adj_pairs == 0:
        pattern_desc = f"جولة واحدة مسجلة ({last_1}) — يلزم إدخال جولات إضافية لرصد التكرار أو التناوب"
    elif repeat_rate >= 0.58:
        pattern_desc = f"نمط سلاسل نشط (Streak={cur_streak} | تكرار={adj_repeats} مقابل تبديل={adj_switches})"
    elif switch_rate >= 0.58:
        pattern_desc = f"نمط تناوب عكسي (Chop: تبديل={adj_switches} مقابل تكرار={adj_repeats})"
    else:
        pattern_desc = f"توزيع مختلط متوازن (تكرار={adj_repeats}, تبديل={adj_switches})"

    tie_note = (
        f"احتمال التعادل المستقل: {tie_prob:.1f}% (المرجع النظري 7.4% | غائب منذ {tie_gap} جولة)"
        if tie_prob < 13.5
        else f"⚠️ مؤشر التعادل المستقل مرتفع نسبياً: {tie_prob:.1f}% (غائب منذ {tie_gap} جولة)"
    )

    return predicted, prob_map, pattern_desc, tie_note, decision_reason


def run_dragon_tiger_engine(clean_hist: List[str], min_sample: int) -> Dict[str, Any]:
    norm_hist = []
    for h in clean_hist:
        sym = _normalize_dt_symbol(h)
        if sym:
            norm_hist.append(sym)

    n = len(norm_hist)
    pred_item, prob_map, pattern_desc, tie_note, decision_reason = _dt_score_next_step(norm_hist)
    est_prob = prob_map.get(pred_item, 46.3)

    # اختبار Walk-Forward على الجولات السابقة (تبدأ المقارنة من الجولة 4 لمنع تسريب المستقبل)
    hits = 0
    trials = 0
    if n >= 4:
        for t in range(3, n):
            step_pred, _, _, _, _ = _dt_score_next_step(norm_hist[:t])
            if step_pred == norm_hist[t]:
                hits += 1
            trials += 1

    measured_acc = (hits / trials * 100.0) if trials > 0 else 0.0
    d_c = norm_hist.count("🐉 تنين")
    t_c = norm_hist.count("🐅 تايجر")
    tie_c = norm_hist.count("🤝 تعادل")
    last_recorded = norm_hist[-1] if n > 0 else "لا يوجد"
    is_repeat_pred = (n > 0 and pred_item == last_recorded)

    if n == 0:
        rel_label = "بانتظار تسجيل الجولات"
    elif is_repeat_pred:
        rel_label = "تكرار مدعوم ببيانات السجل"
    else:
        rel_label = "انتقال لنتيجة مختلفة وفق التحليل"

    if n < min_sample:
        info_line = (
            f"⚠️ [عينة غير كافية لجزم نمط ({n}/{min_sample} جولات)]: اللعبة تعتمد على RNG مستقل في السيرفر "
            f"(الاحتمال النظري: تنين 46.3% | تايجر 46.3% | تعادل 7.4%)."
        )
        conf_str = f"عينة أولية ({n}/{min_sample})"
    else:
        info_line = (
            f"📊 [اختبار Walk-Forward على {trials} جولة سابقة]: "
            f"نسبة الإصابة الفعلية {measured_acc:.1f}% (مقابل مرجع نظري 46.3%) | "
            f"إحصاء السجل: تنين({d_c}) تايجر({t_c}) تعادل({tie_c})"
        )
        conf_str = f"اختبار فعلي: {measured_acc:.1f}% (عينة: {n})"

    pred_html = (
        f"<div style='margin-bottom:5px;padding:4px 6px;background:#090d16;border:1px solid #1e293b;border-radius:5px;font-size:10px;color:#cbd5e1;'>"
        f"📥 <strong>آخر نتيجة مسجلة في الـ History (جولة #{n}):</strong> "
        f"<span style='color:#fbbf24;font-weight:bold;'>{last_recorded}</span><br>"
        f"⚖️ <strong>الأوزان المحسوبة للجولة #{n+1}:</strong> "
        f"🐉 تنين: <strong>{prob_map['🐉 تنين']:.1f}%</strong> | "
        f"🐅 تايجر: <strong>{prob_map['🐅 تايجر']:.1f}%</strong> | "
        f"🤝 تعادل: <strong>{prob_map['🤝 تعادل']:.1f}%</strong>"
        f"</div>"
        f"<div style='background:#052e16;border:1px solid #10b981;padding:6px;border-radius:6px;margin-bottom:5px;'>"
        f"⚡ <strong>التقدير الإحصائي للجولة القادمة (#{n+1}):</strong> "
        f"<span style='color:#34d399;font-size:12px;'>{pred_item}</span> — وزن: <strong>{est_prob:.1f}%</strong> [{rel_label}]<br>"
        f"<span style='font-size:9.5px;color:#a7f3d0;'>🔍 الأساس التحليلي: {decision_reason}</span><br>"
        f"<span style='font-size:9px;color:#fde68a;'>🤝 {tie_note}</span>"
        f"</div>"
        f"<span style='font-size:9px;color:#cbd5e1;'>[حالة السلسلة: {pattern_desc}]</span><br>"
        f"<span style='font-size:9px;color:#93c5fd;'>{info_line}</span>"
    )

    log_msg = (
        f"[DT-ENGINE] N={n} | HistoryLast={last_recorded} | NextEst={pred_item}({est_prob:.1f}%) | "
        f"Tie={prob_map['🤝 تعادل']:.1f}% | Backtest={hits}/{trials}"
    )

    return {
        "prediction_html": pred_html,
        "confidence": conf_str,
        "engine_log": log_msg,
        "measured_accuracy": round(measured_acc, 1) if trials > 0 else None,
        "baseline_accuracy": 46.3,
        "dt_details": {
            "last_recorded": last_recorded,
            "predicted_next": pred_item,
            "is_repeat_prediction": is_repeat_pred,
            "decision_reason": decision_reason,
            "probabilities": {
                "dragon": round(prob_map["🐉 تنين"], 1),
                "tiger": round(prob_map["🐅 تايجر"], 1),
                "tie": round(prob_map["🤝 تعادل"], 1),
            },
            "counts": {"dragon": d_c, "tiger": t_c, "tie": tie_c},
            "pattern_desc": pattern_desc,
            "tie_note": tie_note,
            "sample_size": n,
        },
    }


def detect_streak_anomaly(clean_hist: List[str]) -> Tuple[bool, str]:
    if len(clean_hist) >= 5:
        last_five = clean_hist[-5:]
        if all(x == last_five[0] for x in last_five):
            return True, (
                f"⚠️ [تنبيه تذبذب مبني على البيانات]: النتيجة ({last_five[0]}) تكررت 5 مرات متتالية. "
                f"هذا يشير إلى تذبذب مرتفع ولا يعني حتمية انعكاس الجولة التالية؛ يُنصح بخفض المخاطرة."
            )
    return False, ""


# =====================================================================
# 5. نقاط النهاية (API Endpoints - محفوظة كما هي)
# =====================================================================

@app.post("/api/admin/login")
def admin_login_endpoint(req: AdminLoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    allowed, wait_secs = lifecycle.check_admin_rate_limit(client_ip)

    if not allowed:
        lifecycle.record_audit_event("ADMIN_LOCKOUT", f"Blocked login from {client_ip} ({wait_secs}s left)", "WARNING")
        raise HTTPException(
            status_code=429,
            detail=f"تم تجاوز الحد المسموح لمحاولات الدخول. يرجى الانتظار {wait_secs} ثانية.",
        )

    if req.trigger_clicks is not None and req.trigger_clicks < 6:
        lifecycle.record_audit_event("ADMIN_TRIGGER_REJECT", f"Insufficient clicks ({req.trigger_clicks}/6) from {client_ip}", "WARNING")
        raise HTTPException(status_code=403, detail="يجب إكمال 6 ضغطات متتالية لفتح بوابة التصريح.")

    clean_pass = req.password.strip()
    if not ADMIN_SECRET:
        logger.error("PHANTOM_ADMIN_PASS is not configured; admin login is disabled.")
        raise HTTPException(status_code=503, detail="لم يتم ضبط كلمة مرور الأدمن على السيرفر بعد.")
    if hmac.compare_digest(clean_pass, ADMIN_SECRET):
        admin_token = lifecycle.create_admin_session(client_ip)
        status_code, _ = supabase_server_request("licenses?select=key_code&limit=1", "GET")
        db_connected = status_code == 200
        lifecycle.record_audit_event("ADMIN_LOGIN_SUCCESS", f"Admin authenticated from {client_ip}", "INFO")
        return {
            "ok": True,
            "authenticated": True,
            "admin_token": admin_token,
            "db_connected": db_connected,
            "message": "تم التحقق من صلاحيات الأدمن بنجاح.",
        }

    lifecycle.register_admin_failure(client_ip)
    lifecycle.record_audit_event("ADMIN_LOGIN_FAILED", f"Invalid password attempt from {client_ip}", "ERROR")
    logger.warning(f"Failed admin login attempt from {client_ip}.")
    raise HTTPException(status_code=401, detail="كلمة المرور غير صحيحة.")


@app.get("/api/admin/telemetry")
def admin_telemetry_endpoint(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    state = lifecycle.evaluate_lifecycle_state(is_incoming_prediction=False)
    token_valid = True
    room_summary = {k: len(v) for k, v in lifecycle.room_histories.items()}

    return {
        "ok": True,
        "token_verified": token_valid,
        "lifecycle": state,
        "game_request_counts": lifecycle.game_request_counts,
        "room_round_counts": room_summary,
        "audit_logs": lifecycle.audit_logs[-25:][::-1],
        "error_logs": lifecycle.error_logs[-15:][::-1],
    }


@app.post("/api/license/verify")
def verify_license_proxy(req: LicenseVerifyRequest, request: Request):
    clean_code = req.key_code.strip().upper()
    encoded_code = urllib.parse.quote(clean_code)
    status_code, rows = supabase_server_request(
        f"licenses?key_code=eq.{encoded_code}&select=*&order=created_at.desc&limit=1",
        "GET",
    )

    if status_code != 200 or not isinstance(rows, list):
        lifecycle.record_error_event("SUPABASE_VERIFY", f"Status {status_code} on key {clean_code}")
        return {"ok": False, "error_type": "DB_QUERY_ERROR", "message": "تعذر الوصول إلى جدول التراخيص في قاعدة البيانات."}
    if not rows:
        return {"ok": False, "error_type": "KEY_NOT_FOUND", "message": "❌ هذا المفتاح غير موجود في قاعدة البيانات!"}

    record = rows[0]
    if record.get("status") == "banned":
        return {"ok": False, "error_type": "KEY_BANNED", "message": "🚫 هذا المفتاح محظور وتم إيقافه."}

    # Railway's proxy normally supplies X-Forwarded-For. Store it as a recovery/support hint,
    # never as a standalone credential. The key itself is still required for recovery.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    client_ip = forwarded or (request.client.host if request.client else "unknown")
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat().replace("+00:00", "Z")
    duration_mins = max(1, min(int(record.get("duration_minutes") or 60), 525600))
    patch = {}

    # Start the countdown only on the first successful activation, not when owner/reseller creates it.
    activated_at = record.get("activated_at")
    expires_at = record.get("expires_at")
    if not activated_at or not expires_at:
        activated_at = now_iso
        expires_at = datetime.fromtimestamp(now_dt.timestamp() + duration_mins * 60, timezone.utc).isoformat().replace("+00:00", "Z")
        patch.update({"activated_at": activated_at, "expires_at": expires_at, "last_ip": client_ip})

    try:
        expiry_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        expiry_ms = int(expiry_dt.timestamp() * 1000)
    except Exception:
        # Legacy rows: preserve old created_at expiry only when activated_at columns were not migrated.
        try:
            created_dt = datetime.fromisoformat(str(record.get("created_at", "")).replace("Z", "+00:00"))
            expiry_ms = int((created_dt.timestamp() + duration_mins * 60) * 1000)
        except Exception:
            return {"ok": False, "error_type": "DB_SCHEMA_ERROR", "message": "بيانات مدة الكود غير مكتملة. نفّذ تحديث SQL الخاص بالتراخيص."}

    now_ms = int(now_dt.timestamp() * 1000)
    if expiry_ms <= now_ms:
        return {"ok": False, "error_type": "KEY_EXPIRED", "message": "⚠️ انتهت صلاحية الكود. يمكن للمالك إعادة تنشيطه من لوحة التحكم."}

    bound_dev = record.get("bound_device")
    # If browser storage was cleared, allow recovery only with the same saved IP and the correct key.
    # IP can be shared/dynamic, so it is a recovery aid, not proof of identity by itself.
    if bound_dev and bound_dev != req.device_token:
        previous_ip = record.get("last_ip")
        if previous_ip and previous_ip == client_ip:
            patch["bound_device"] = req.device_token
            patch["last_ip"] = client_ip
        else:
            return {"ok": False, "error_type": "DEVICE_MISMATCH", "message": "هذا الكود مرتبط بجهاز آخر. لو فقدت بيانات المتصفح، استخدم نفس الشبكة أو تواصل مع المالك لإعادة التنشيط."}
    elif not bound_dev:
        patch["bound_device"] = req.device_token
        patch["last_ip"] = client_ip
    else:
        # Refresh the IP for support/recovery when the same device returns.
        patch["last_ip"] = client_ip

    if patch:
        patch_status, patch_result = supabase_server_request(
            f"licenses?key_code=eq.{encoded_code}", "PATCH", patch
        )
        if patch_status not in (200, 204):
            lifecycle.record_error_event("LICENSE_ACTIVATE_SAVE", f"Status {patch_status} on key {clean_code}")
            return {"ok": False, "error_type": "DB_WRITE_ERROR", "message": "تعذر حفظ تفعيل الكود في قاعدة البيانات. لم يتم بدء الجلسة."}

    lifecycle.record_audit_event("LICENSE_VERIFIED", f"Key {clean_code} verified; device={req.device_token[:10]}; ip_saved=yes")
    return {
        "ok": True, "error_type": None, "key_code": clean_code,
        "duration_minutes": duration_mins, "activated_at": activated_at,
        "expiry_ms": expiry_ms, "remaining_minutes": round((expiry_ms - now_ms) / 60000.0, 2),
    }


@app.post("/api/license/{key_code}/activate")
def reactivate_license(key_code: str, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    clean_code = key_code.strip().upper()
    encoded_code = urllib.parse.quote(clean_code)
    status_code, rows = supabase_server_request(
        f"licenses?key_code=eq.{encoded_code}&select=key_code,status&limit=1", "GET"
    )
    if status_code != 200 or not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="تعذر الاتصال بجدول الأكواد في Supabase.")
    if not rows:
        raise HTTPException(status_code=404, detail="الكود غير موجود.")
    patch_status, result = supabase_server_request(
        f"licenses?key_code=eq.{encoded_code}", "PATCH",
        {"status": "active", "activated_at": None, "expires_at": None,
         "bound_device": None, "last_ip": None}
    )
    if patch_status not in (200, 204):
        raise HTTPException(status_code=502, detail="تعذر إعادة تنشيط الكود؛ راجع أعمدة جدول licenses.")
    lifecycle.record_audit_event("LICENSE_REACTIVATED", f"Key {clean_code} reset by admin; timer starts on next successful use")
    return {"ok": True, "key_code": clean_code, "message": "تمت إعادة تنشيط الكود. يبدأ الوقت عند أول دخول ناجح بعد التنشيط."}


@app.get("/api/health")
def health_check():
    state = lifecycle.evaluate_lifecycle_state(is_incoming_prediction=False)
    reseller_db_check = "not_configured"
    platform_db_check = "not_configured"
    if SUPABASE_SERVICE_ROLE_KEY:
        db_status, db_rows = _learning_db_request(f"{RESELLER_TABLE}?select=id&limit=1")
        reseller_db_check = "connected" if db_status == 200 and isinstance(db_rows, list) else f"error_{db_status}"
        platform_status, platform_rows = _learning_db_request(f"{PLATFORM_TABLE}?select=id&limit=1")
        platform_db_check = "connected" if platform_status == 200 and isinstance(platform_rows, list) else f"error_{platform_status}"
    return {
        "ok": True,
        "service": "PHANTOM ELITE Platform & Farm Engine",
        "version": "123.2",
        "lifecycle": state,
        "storage": {
            "mode": "supabase_reseller_state" if SUPABASE_SERVICE_ROLE_KEY else ("railway_volume" if os.path.ismount("/data") else "local_ephemeral"),
            "data_mount_detected": os.path.ismount("/data"),
            "reseller_durable_storage_configured": bool(SUPABASE_SERVICE_ROLE_KEY),
            "reseller_database_connection": reseller_db_check,
            "reseller_table": RESELLER_TABLE if SUPABASE_SERVICE_ROLE_KEY else None,
            "platform_table": PLATFORM_TABLE if SUPABASE_SERVICE_ROLE_KEY else None,
            "platform_database_connection": platform_db_check,
            "platform_durable_storage_configured": bool(SUPABASE_SERVICE_ROLE_KEY),
            "state_path": STATE_FILE_PATH,
            "reseller_path": RESELLER_STATE_FILE,
            "learning_path": LEARNING_FILE_PATH,
            "warning": (None if SUPABASE_SERVICE_ROLE_KEY else ("لا يوجد Volume مركّب عند /data؛ ملفات الوكلاء محلية وقد تضيع بعد إعادة النشر. فعّل Supabase باستخدام ملف SQL المرفق." if not os.path.ismount("/data") else "ملف الوكلاء لا يزال محليًا؛ تأكد من أن مساره داخل Volume.")),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/config")
def get_runtime_config():
    return {
        "config": lifecycle.config.dict(),
        "lifecycle": lifecycle.evaluate_lifecycle_state(is_incoming_prediction=False),
    }


@app.post("/api/config")
def update_runtime_config(new_config: RuntimeConfig, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    lifecycle.update_config(new_config)
    return {
        "ok": True,
        "message": "تم تحديث إعدادات التشغيل والراحة وحدود الضغط وحفظها بنجاح.",
        "config": lifecycle.config.dict(),
    }


@app.post("/api/resellers")
def create_reseller(req: ResellerCreateRequest, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    username = req.username.strip().lower()
    if not username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="اسم المستخدم يقبل الحروف والأرقام والشرطة فقط.")
    with reseller_lock:
        if any(r.get("username", "").lower() == username for r in reseller_store["resellers"].values()):
            raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم بالفعل.")
        salt, digest = _hash_reseller_password(req.password)
        rid = uuid.uuid4().hex
        row = {"id": rid, "username": username, "display_name": req.display_name.strip(),
               "password_salt": salt, "password_hash": digest, "points": req.initial_points,
               "active": True, "created_at": datetime.now(timezone.utc).isoformat(),
               "keys_created": 0, "funded_points_total": int(req.initial_points), "vip_bonus_points": 0, "vip_awards": [],
               "ledger": [{"timestamp": datetime.now(timezone.utc).isoformat(), "type": "CREATE", "amount": req.initial_points,
               "balance_after": req.initial_points, "reason": "رصيد افتتاحي من المالك"}]}
        _award_vip_milestones(row)
        reseller_store["resellers"][rid] = row
        _record_owner_audit("RESELLER_CREATED", row, f"إنشاء حساب وكيل؛ الرصيد الافتتاحي {req.initial_points} نقطة")
        _save_reseller_store()
        return {"ok": True, "reseller": _public_reseller(row)}


@app.get("/api/resellers")
def list_resellers(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        rows = [_public_reseller(r) for r in reseller_store["resellers"].values()]
        rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return {"ok": True, "resellers": rows}


@app.get("/api/reseller-keys")
def list_reseller_keys(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        return {"ok": True, "keys": reseller_store.get("key_history", [])[-1000:][::-1]}


@app.post("/api/resellers/{reseller_id}/points")
def adjust_reseller_points(reseller_id: str, req: ResellerPointsRequest,
                           x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    _require_sensitive_confirmation(req.confirmation_password)
    if req.amount == 0:
        raise HTTPException(status_code=400, detail="قيمة تعديل النقاط لا يمكن أن تكون صفرًا.")
    with reseller_lock:
        row = reseller_store["resellers"].get(reseller_id)
        if not row:
            raise HTTPException(status_code=404, detail="الوكيل غير موجود.")
        new_balance = int(row.get("points", 0)) + req.amount
        if new_balance < 0:
            raise HTTPException(status_code=400, detail="لا يمكن خصم نقاط أكثر من الرصيد الحالي.")
        row["points"] = new_balance
        row.setdefault("ledger", []).append({"timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "CREDIT" if req.amount > 0 else "DEBIT", "amount": req.amount,
            "balance_after": new_balance, "reason": (req.reason or "تعديل بواسطة المالك").strip()})
        new_awards = []
        if req.amount > 0:
            row["funded_points_total"] = int(row.get("funded_points_total", 0)) + req.amount
            new_awards = _award_vip_milestones(row)
        row["ledger"] = row["ledger"][-500:]
        _record_owner_audit("RESELLER_POINTS", row, f"{req.amount:+d} نقطة؛ السبب: {(req.reason or '').strip()}")
        _save_reseller_store()
        return {"ok": True, "reseller": _public_reseller(row), "new_vip_awards": new_awards}


@app.post("/api/resellers/{reseller_id}/status")
def set_reseller_status(reseller_id: str, req: ResellerStatusRequest,
                        x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    _require_sensitive_confirmation(req.confirmation_password)
    with reseller_lock:
        row = reseller_store["resellers"].get(reseller_id)
        if not row:
            raise HTTPException(status_code=404, detail="الوكيل غير موجود.")
        row["active"] = req.active
        _record_owner_audit("RESELLER_STATUS", row, "تفعيل الوكيل" if req.active else "إيقاف الوكيل وإنهاء الجلسات")
        _save_reseller_store()
        if not req.active:
            for token, session in list(reseller_sessions.items()):
                if session.get("reseller_id") == reseller_id:
                    reseller_sessions.pop(token, None)
        return {"ok": True, "reseller": _public_reseller(row)}


@app.patch("/api/resellers/{reseller_id}/profile")
def update_reseller_profile(reseller_id: str, req: ResellerUpdateRequest, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    _require_sensitive_confirmation(req.confirmation_password)
    with reseller_lock:
        row = reseller_store["resellers"].get(reseller_id)
        if not row:
            raise HTTPException(status_code=404, detail="الوكيل غير موجود.")
        old = row.get("display_name", "")
        row["display_name"] = req.display_name.strip()
        _record_owner_audit("RESELLER_PROFILE_UPDATED", row, f"تغيير الاسم الظاهر من {old} إلى {row['display_name']}")
        _save_reseller_store()
        return {"ok": True, "reseller": _public_reseller(row)}


@app.patch("/api/resellers/{reseller_id}/credentials")
def update_reseller_credentials(reseller_id: str, req: ResellerCredentialsRequest, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    _require_sensitive_confirmation(req.confirmation_password)
    if not req.username and not req.password:
        raise HTTPException(status_code=400, detail="أدخل اسم مستخدم جديدًا أو كلمة مرور جديدة على الأقل.")
    with reseller_lock:
        row = reseller_store["resellers"].get(reseller_id)
        if not row:
            raise HTTPException(status_code=404, detail="الوكيل غير موجود.")
        if req.username:
            username = req.username.strip().lower()
            if any(rid != reseller_id and r.get("username", "").lower() == username for rid, r in reseller_store["resellers"].items()):
                raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم بالفعل.")
            old_username = row.get("username", "")
            row["username"] = username
        else:
            old_username = row.get("username", "")
        changes = []
        if req.username:
            changes.append(f"اسم المستخدم {old_username} → {row['username']}")
        if req.password:
            salt, digest = _hash_reseller_password(req.password)
            row["password_salt"], row["password_hash"] = salt, digest
            changes.append("تغيير كلمة المرور (لا يتم حفظها كنص صريح)")
        _record_owner_audit("RESELLER_CREDENTIALS_UPDATED", row, "؛ ".join(changes))
        # Force re-login after credential changes, including all sessions on this worker.
        for token, session in list(reseller_sessions.items()):
            if session.get("reseller_id") == reseller_id:
                reseller_sessions.pop(token, None)
        _save_reseller_store()
        return {"ok": True, "reseller": _public_reseller(row), "sessions_revoked": True}


@app.get("/api/reseller-audit")
def list_reseller_audit(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        return {"ok": True, "events": reseller_store.get("audit_log", [])[-500:][::-1]}


@app.get("/api/reseller-stats")
def reseller_statistics(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        rows = list(reseller_store.get("resellers", {}).values())
        keys = reseller_store.get("key_history", [])
        by_id = {}
        for key in keys:
            rid = key.get("reseller_id")
            if rid:
                stats = by_id.setdefault(rid, {"keys_created": 0, "points_spent": 0, "last_key_at": None})
                stats["keys_created"] += 1
                stats["points_spent"] += int(key.get("cost_points", 0) or 0)
                if not stats["last_key_at"] or (key.get("created_at", "") > stats["last_key_at"]):
                    stats["last_key_at"] = key.get("created_at")
        items = []
        for row in rows:
            stat = by_id.get(row.get("id"), {})
            items.append({"id": row.get("id"), "username": row.get("username"), "display_name": row.get("display_name"),
                "active": bool(row.get("active", True)), "points": int(row.get("points", 0)),
                "keys_created": int(stat.get("keys_created", row.get("keys_created", 0))),
                "points_spent": int(stat.get("points_spent", 0)), "funded_points_total": int(row.get("funded_points_total", 0)),
                "last_key_at": stat.get("last_key_at")} )
        return {"ok": True, "total_resellers": len(rows), "active_resellers": sum(1 for r in rows if r.get("active", True)),
            "total_keys_created": len(keys), "total_points_available": sum(int(r.get("points", 0)) for r in rows), "resellers": items}


@app.post("/api/reseller-keys/{key_code}/status")
def set_reseller_key_status(key_code: str, req: ResellerKeyStatusRequest, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    _require_sensitive_confirmation(req.confirmation_password)
    clean_code = key_code.strip().upper()
    encoded_code = urllib.parse.quote(clean_code, safe="")
    status_code, rows = supabase_server_request(f"licenses?key_code=eq.{encoded_code}&select=key_code,status&limit=1", "GET")
    if status_code != 200 or not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="تعذر قراءة حالة الكود من قاعدة البيانات.")
    if not rows:
        raise HTTPException(status_code=404, detail="الكود غير موجود.")
    status_code, result = supabase_server_request(f"licenses?key_code=eq.{encoded_code}", "PATCH", {"status": req.status})
    if status_code not in (200, 204):
        raise HTTPException(status_code=502, detail="تعذر تحديث حالة الكود في قاعدة البيانات.")
    with reseller_lock:
        for item in reseller_store.get("key_history", []):
            if str(item.get("key_code", "")).upper() == clean_code:
                item["status"] = req.status
        reseller_store.setdefault("audit_log", []).append({"timestamp": datetime.now(timezone.utc).isoformat(), "actor": "owner",
            "action": "RESELLER_KEY_STATUS", "reseller_id": None, "reseller_username": None,
            "details": f"الكود {clean_code}: {rows[0].get('status')} → {req.status}"})
        reseller_store["audit_log"] = reseller_store["audit_log"][-2000:]
        _save_reseller_store()
    lifecycle.record_audit_event("RESELLER_KEY_STATUS", f"Key {clean_code}: {req.status}")
    return {"ok": True, "key_code": clean_code, "status": req.status}


@app.get("/api/admin/chat/settings")
def admin_chat_settings(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        return {"ok": True, "enabled": bool(reseller_store.get("chat_enabled", True)),
                "messages_count": len(reseller_store.get("chat_messages", []))}


@app.post("/api/admin/chat/settings")
def update_admin_chat_settings(req: ChatSettingRequest, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    _require_sensitive_confirmation(req.confirmation_password)
    with reseller_lock:
        previous = bool(reseller_store.get("chat_enabled", True))
        reseller_store["chat_enabled"] = req.enabled
        reseller_store.setdefault("audit_log", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(), "actor": "owner",
            "action": "CHAT_ENABLED" if req.enabled else "CHAT_DISABLED", "reseller_id": None,
            "reseller_username": None, "details": f"الشات: {previous} → {req.enabled}"})
        reseller_store["audit_log"] = reseller_store["audit_log"][-2000:]
        _save_reseller_store()
    lifecycle.record_audit_event("CHAT_SETTING_CHANGED", f"enabled={req.enabled}")
    return {"ok": True, "enabled": req.enabled}


@app.get("/api/admin/chat/messages")
def admin_chat_messages(reseller_id: Optional[str] = None, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        rows = reseller_store.get("chat_messages", [])
        if reseller_id:
            rows = [m for m in rows if m.get("reseller_id") == reseller_id]
        return {"ok": True, "enabled": bool(reseller_store.get("chat_enabled", True)), "messages": rows[-500:]}


@app.post("/api/admin/chat/messages")
def admin_send_chat_message(req: ChatMessageRequest, x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="الرسالة فارغة.")
    if not req.reseller_id:
        raise HTTPException(status_code=400, detail="حدد الوكيل المستهدف.")
    with reseller_lock:
        reseller = reseller_store.get("resellers", {}).get(req.reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="الوكيل غير موجود.")
        if not reseller_store.get("chat_enabled", True):
            raise HTTPException(status_code=403, detail="الشات مغلق من المالك.")
        item = {"id": uuid.uuid4().hex, "reseller_id": req.reseller_id, "sender_role": "owner",
                "sender_name": "المالك", "message": message, "created_at": datetime.now(timezone.utc).isoformat(), "read": False}
        reseller_store.setdefault("chat_messages", []).append(item)
        reseller_store["chat_messages"] = reseller_store["chat_messages"][-5000:]
        _save_reseller_store()
        return {"ok": True, "message": item}


@app.get("/api/reseller/chat/messages")
def reseller_chat_messages(x_reseller_token: Optional[str] = Header(default=None)):
    with reseller_lock:
        reseller = _get_reseller_session(x_reseller_token)
        if not reseller_store.get("chat_enabled", True):
            raise HTTPException(status_code=403, detail="الشات مغلق حاليًا من المالك.")
        rows = [m for m in reseller_store.get("chat_messages", []) if m.get("reseller_id") == reseller["id"]]
        for m in rows:
            if m.get("sender_role") == "owner": m["read"] = True
        _save_reseller_store()
        return {"ok": True, "enabled": True, "messages": rows[-200:]}


@app.post("/api/reseller/chat/messages")
def reseller_send_chat_message(req: ChatMessageRequest, x_reseller_token: Optional[str] = Header(default=None)):
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="الرسالة فارغة.")
    with reseller_lock:
        reseller = _get_reseller_session(x_reseller_token)
        if not reseller_store.get("chat_enabled", True):
            raise HTTPException(status_code=403, detail="الشات مغلق حاليًا من المالك.")
        item = {"id": uuid.uuid4().hex, "reseller_id": reseller["id"], "sender_role": "reseller",
                "sender_name": reseller.get("display_name") or reseller.get("username"), "message": message,
                "created_at": datetime.now(timezone.utc).isoformat(), "read": False}
        reseller_store.setdefault("chat_messages", []).append(item)
        reseller_store["chat_messages"] = reseller_store["chat_messages"][-5000:]
        _save_reseller_store()
        return {"ok": True, "message": item}


@app.get("/api/admin/backup")
def admin_backup(x_admin_token: Optional[str] = Header(default=None)):
    _require_admin(x_admin_token)
    with reseller_lock:
        snapshot = {"format": "phantom-elite-backup-v1", "created_at": datetime.now(timezone.utc).isoformat(),
                    "reseller_state": copy.deepcopy(reseller_store)}
    return snapshot


@app.post("/api/reseller/login")
def reseller_login(req: ResellerLoginRequest):
    username = req.username.strip().lower()
    with reseller_lock:
        row = next((r for r in reseller_store["resellers"].values() if r.get("username", "").lower() == username), None)
        if not row or not row.get("active", True):
            raise HTTPException(status_code=401, detail="بيانات الدخول غير صحيحة أو الحساب موقوف.")
        _, digest = _hash_reseller_password(req.password, row.get("password_salt"))
        if not hmac.compare_digest(digest, row.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="بيانات الدخول غير صحيحة أو الحساب موقوف.")
        token = secrets.token_urlsafe(36)
        reseller_sessions[token] = {"reseller_id": row["id"], "expires_at": time.time() + 12 * 3600}
        return {"ok": True, "token": token, "reseller": _public_reseller(row)}


@app.get("/api/reseller/me")
def reseller_me(x_reseller_token: Optional[str] = Header(default=None)):
    with reseller_lock:
        row = _get_reseller_session(x_reseller_token)
        own_keys = [k for k in reseller_store.get("key_history", []) if k.get("reseller_id") == row["id"]]
        return {"ok": True, "reseller": _public_reseller(row), "keys": own_keys[-200:][::-1],
                "ledger": row.get("ledger", [])[-100:][::-1]}


@app.post("/api/reseller/keys")
def reseller_create_key(req: ResellerKeyRequest, x_reseller_token: Optional[str] = Header(default=None)):
    code = req.key_code.strip().upper()
    if req.duration_minutes not in (60, 1440):
        raise HTTPException(status_code=400, detail="المدة المسموحة ساعة واحدة أو يوم كامل فقط.")
    if not code or any(ch.isspace() for ch in code):
        raise HTTPException(status_code=400, detail="الكود مطلوب ولا يجب أن يحتوي على مسافات.")
    cost = 1 if req.duration_minutes == 60 else 24
    with reseller_lock:
        row = _get_reseller_session(x_reseller_token)
        # افحص التكرار قبل خصم النقاط، ثم يظل قيد التفرد في قاعدة البيانات مهمًا لمنع السباق.
        encoded_code = urllib.parse.quote(code, safe="")
        check_status, existing_rows = supabase_server_request(
            f"licenses?key_code=eq.{encoded_code}&select=key_code&limit=1", "GET")
        if check_status != 200 or not isinstance(existing_rows, list):
            raise HTTPException(status_code=502, detail="تعذر التأكد من عدم تكرار الكود. لم يتم خصم نقاط.")
        if existing_rows:
            raise HTTPException(status_code=409, detail="هذا الكود موجود بالفعل. اختر كودًا مختلفًا.")
        if int(row.get("points", 0)) < cost:
            raise HTTPException(status_code=402, detail=f"رصيدك غير كافٍ. تحتاج {cost} نقطة والمتاح {row.get('points', 0)}.")
        # احجز النقاط واحفظ الخصم أولاً؛ لو فشل إدخال الكود نرجع الرصيد تلقائياً.
        old_balance = int(row.get("points", 0))
        old_keys_created = int(row.get("keys_created", 0))
        now_iso = datetime.now(timezone.utc).isoformat()
        ledger_entry = {"timestamp": now_iso, "type": "KEY_COST", "amount": -cost,
            "balance_after": old_balance - cost, "reason": f"إنشاء كود {code} ({'ساعة' if req.duration_minutes == 60 else '24 ساعة'})"}
        row["points"] = old_balance - cost
        row["keys_created"] = old_keys_created + 1
        row.setdefault("ledger", []).append(ledger_entry)
        row["ledger"] = row["ledger"][-500:]
        _save_reseller_store()
        payload = [{"key_code": code, "duration_minutes": req.duration_minutes, "status": "active",
                    "bound_device": None, "created_at": now_iso}]
        status_code, result = supabase_server_request("licenses", "POST", payload)
        if status_code not in (200, 201) or isinstance(result, dict) and result.get("error"):
            row["points"] = old_balance
            row["keys_created"] = old_keys_created
            if row.get("ledger") and row["ledger"][-1] is ledger_entry:
                row["ledger"].pop()
            _save_reseller_store()
            detail = result.get("error", "تعذر إنشاء الكود في قاعدة البيانات.") if isinstance(result, dict) else "تعذر إنشاء الكود في قاعدة البيانات."
            raise HTTPException(status_code=409 if status_code == 409 else 502, detail=str(detail)[:240])
        new_balance = int(row["points"])
        item = {"key_code": code, "duration_minutes": req.duration_minutes, "created_at": now_iso,
                "status": "active", "cost_points": cost, "reseller_id": row["id"], "reseller_username": row["username"]}
        reseller_store.setdefault("key_history", []).append(item)
        reseller_store["key_history"] = reseller_store["key_history"][-5000:]
        _record_owner_audit("RESELLER_KEY_CREATED", row, f"إنشاء الكود {code} وخصم {cost} نقطة")
        _save_reseller_store()
        return {"ok": True, "key": item, "points": new_balance, "message": f"تم إنشاء الكود وخصم {cost} نقطة."}


@app.post("/api/learning/outcome")
async def record_learning_outcome(req: LearningOutcomeRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = _check_learning_rate_limit(client_ip)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"تم تجاوز حد تسجيل النتائج. حاول بعد {retry_after} ثانية.")
    game = req.game_type.upper().strip()
    if game not in {"DRAGON", "FARM", "POKER"}:
        raise HTTPException(status_code=400, detail="نوع اللعبة غير مدعوم للتعلم.")
    actual = normalize_item_name(req.actual)
    predicted = [normalize_item_name(x) for x in req.predicted if x and x.strip()]
    if not predicted:
        raise HTTPException(status_code=400, detail="التوقع المسجل غير صالح.")
    if game == "DRAGON":
        actual = normalize_dt_outcome(actual)
        predicted = [normalize_dt_outcome(x) for x in predicted]
    elif game == "POKER":
        actual = normalize_poker_outcome(actual)
        predicted = [normalize_poker_outcome(x) for x in predicted]
    else:
        allowed = set(FARM_CATALOG)
        if actual not in allowed or any(x not in allowed for x in predicted):
            raise HTTPException(status_code=400, detail="نتيجة المزرعة غير معروفة.")
    if not actual or any(not x for x in predicted):
        raise HTTPException(status_code=400, detail="تعذر توحيد النتيجة أو التوقع.")

    record = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "game": game,
        "room_id": (req.room_id or "default")[:100],
        "actual": actual,
        "predicted": predicted[:5],
        "hit": actual in predicted,
        "history_size": len(req.history_before),
        "source": "client_reported",
        "verified": False,
        "verification_status": "unverified_client_report",
        "probabilities": {str(k)[:80]: max(0.0, min(100.0, float(v))) for k, v in list(req.probabilities.items())[:20] if isinstance(v, (int, float)) and math.isfinite(float(v))},
        "client_ip_hash": hashlib.sha256((request.client.host if request.client else "unknown").encode()).hexdigest()[:16],
    }
    with _learning_lock:
        learning_store.setdefault("records", []).append(record)
        learning_store["records"] = learning_store["records"][-10000:]
        db_saved = _persist_learning_record(record)
        # Keep a local fallback/cache too; without a volume this cache may reset on redeploy.
        _save_learning_store(learning_store)
    return {"ok": True, "recorded": True, "hit": record["hit"], "game": game,
            "storage": "supabase" if db_saved else "local_fallback",
            "durable_storage": bool(db_saved)}


@app.get("/api/learning/stats")
def learning_stats():
    # Read the durable source on demand so records written by another worker/deployment are visible.
    db_store = _load_learning_from_supabase()
    with _learning_lock:
        if db_store is not None:
            learning_store["records"] = db_store.get("records", [])[-10000:]
        records = list(learning_store.get("records", []))
    games: Dict[str, Any] = {}
    for game in ("DRAGON", "FARM", "POKER"):
        all_rows = [r for r in records if r.get("game") == game]
        verified_rows = [r for r in all_rows if r.get("verified") is True and r.get("source") in {"admin_verified", "official_result_feed"}]
        out = {}
        for window in (50, 100, 500):
            recent = verified_rows[-window:]
            out[str(window)] = {
                "trials": len(recent),
                "hits": sum(1 for r in recent if r.get("hit")),
                "accuracy": round(sum(1 for r in recent if r.get("hit")) / len(recent) * 100, 2) if recent else None,
            }
        metric = {"DRAGON": "إصابة توقع واحد", "FARM": "تغطية أفضل 5 عناصر", "POKER": "تغطية الخانتين الأولى والثانية"}[game]
        games[game] = {"total_reports": len(all_rows), "verified_trials": len(verified_rows), "metric": metric,
                       "windows": out, "unverified_reports": len(all_rows) - len(verified_rows)}
    durable = bool(SUPABASE_SERVICE_ROLE_KEY and db_store is not None)
    return {"ok": True, "total_records": len(records), "games": games,
            "storage": "supabase" if durable else "local_fallback",
            "durable_storage": durable,
            "accuracy_basis": "verified_only",
            "note": "النتائج التي يرسلها العميل تُسجّل كتقارير غير مؤكدة ولا تدخل في حساب الدقة. لا تُحتسب الدقة إلا بعد التحقق الإداري أو عبر مصدر نتائج رسمي موثوق. لا تمثل الدقة ضمانًا للنتيجة التالية."}


def normalize_dt_outcome(raw: str) -> str:
    s = str(raw or "").strip()
    upper = s.upper()
    if "تعادل" in s or "TIE" in upper or "🤝" in s:
        return "🤝 تعادل"
    if "تنين" in s or "DRAGON" in upper or "🐉" in s:
        return "🐉 تنين"
    if "تايجر" in s or "TIGER" in upper or "🐅" in s:
        return "🐅 تايجر"
    return ""


def normalize_poker_outcome(raw: str) -> str:
    """Normalize explicit chair labels without matching letters inside other words.

    The previous substring check treated ``Seat B`` as chair A because the word
    ``SEAT`` contains the letter A. Only standalone chair tokens are accepted.
    """
    original = str(raw or "").strip()
    upper = original.upper()
    # Prefer explicit Arabic chair labels and emoji labels.
    if "كرسي A" in upper or "🟢" in original:
        return "🟢 كرسي A"
    if "كرسي B" in upper or "🔵" in original:
        return "🔵 كرسي B"
    if "كرسي C" in upper or "🔴" in original:
        return "🔴 كرسي C"
    # Accept common English forms (A / Seat A / Chair A), but not words like SEAT as A.
    match = re.search(r"(?:^|[^A-Z])(?:SEAT|CHAIR)?\s*([ABC])(?:$|[^A-Z])", upper)
    if not match:
        return ""
    return {"A": "🟢 كرسي A", "B": "🔵 كرسي B", "C": "🔴 كرسي C"}[match.group(1)]


@app.post("/api/predict")
async def predict_endpoint(req: PredictionRequest, authorization: Optional[str] = Header(default=None)):
    _platform_find_session(authorization)
    lifecycle.total_requests += 1
    state_meta = lifecycle.evaluate_lifecycle_state(is_incoming_prediction=True)

    game = req.game_type.upper().strip()
    if "FARM" not in game:
        raise HTTPException(status_code=410, detail="تم إيقاف محركات Dragon Tiger والبوكر. PHANTOM ELITE يدعم توقع المزرعة فقط.")
    game = "FARM"
    clean_hist = [normalize_item_name(h) for h in req.history if h and h.strip()]
    room_key = f"{game}:{req.room_id or 'default'}"

    if clean_hist:
        lifecycle.room_histories[room_key] = clean_hist[-100:]
    elif room_key in lifecycle.room_histories:
        clean_hist = lifecycle.room_histories[room_key]

    payload_signature = hashlib.sha256(
        f"{game}|{room_key}|{'|'.join(clean_hist)}|{req.coins}|{req.poker_primary_ratio}|{req.poker_alt_ratio}".encode("utf-8")
    ).hexdigest()

    now = time.time()
    if not req.force_refresh and payload_signature in lifecycle.dedup_cache:
        cached_time, cached_resp = lifecycle.dedup_cache[payload_signature]
        if now - cached_time <= lifecycle.config.dedup_window_seconds:
            cached_copy = dict(cached_resp)
            cached_copy["deduplicated"] = True
            return cached_copy

    async with lifecycle.semaphore:
        lifecycle.active_tasks += 1
        try:
            min_s = lifecycle.config.min_sample_for_pattern
            poker_details = None
            dt_details = None
            farm_details = None

            if "FARM" in game:
                lifecycle.game_request_counts["FARM"] = lifecycle.game_request_counts.get("FARM", 0) + 1
                res = run_farm_engine(clean_hist, min_s)
                farm_details = res.get("farm_details")
            elif "POKER" in game:
                lifecycle.game_request_counts["POKER"] = lifecycle.game_request_counts.get("POKER", 0) + 1
                res = run_poker_engine(
                    clean_hist,
                    min_s,
                    coins=req.coins,
                    primary_ratio=req.poker_primary_ratio or 0.65,
                    alt_ratio=req.poker_alt_ratio or 0.35,
                )
                poker_details = res.get("poker_details")
            elif "DRAGON" in game:
                lifecycle.game_request_counts["DRAGON"] = lifecycle.game_request_counts.get("DRAGON", 0) + 1
                res = run_dragon_tiger_engine(clean_hist, min_s)
                dt_details = res.get("dt_details")
            else:
                raise HTTPException(status_code=400, detail="نوع اللعبة غير مدعوم.")

            is_warn, warn_text = detect_streak_anomaly(clean_hist)
            pred_html = res["prediction_html"]

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
                "poker_details": poker_details,
                "dt_details": dt_details,
                "farm_details": farm_details,
                "deduplicated": False,
            }

            lifecycle.dedup_cache[payload_signature] = (now, response_payload)
            if lifecycle.total_requests % 10 == 0:
                lifecycle.save_persisted_state()

            return response_payload

        except Exception as exc:
            lifecycle.record_error_event(f"PREDICT_{game}", str(exc))
            logger.exception(f"Prediction error in {game}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))
        finally:
            lifecycle.active_tasks -= 1


# =====================================================================
# PHANTOM ELITE PLATFORM API — accounts, social feed, chat, VIP and admin
# =====================================================================
PLATFORM_TABLE = os.environ.get("PHANTOM_PLATFORM_TABLE", "phantom_platform_state").strip()
PLATFORM_FILE_PATH = _storage_path("PHANTOM_PLATFORM_FILE", "phantom_platform_state.json")
_platform_lock = threading.RLock()


def _platform_default_state() -> Dict[str, Any]:
    return {
        "users": {}, "sessions": {}, "moments": [], "messages": [], "reports": [], "predictions": [],
        "announcements": [], "featured_user_ids": [], "audit": [],
        "apps": [
            {"id": "toptop", "name": "TopTop", "active": True, "engine": "FARM"},
            {"id": "xena", "name": "Xena", "active": True, "engine": "FARM"},
            {"id": "hifami", "name": "Hi fami", "active": True, "engine": "FARM"},
            {"id": "mico", "name": "Mico", "active": True, "engine": "FARM"},
            {"id": "helloyo", "name": "Hello Yo", "active": True, "engine": "FARM"},
            {"id": "taka", "name": "Taka", "active": True, "engine": "FARM"},
            {"id": "soyo", "name": "SOYO", "active": True, "engine": "FARM"},
            {"id": "sugu", "name": "SUGU", "active": True, "engine": "FARM"},
            {"id": "4funlite", "name": "4Fun Lite", "active": True, "engine": "FARM"},
        ],
        "settings": {"app_name": "BOOM - PREMIUM QUILT EDITION", "sound_enabled": True, "theme": "royal-dark", "vip_prices": {"30": 150, "150": 500, "365": 1000}, "chat_enabled": True, "moments_enabled": True},
    }


def _platform_load_state() -> Dict[str, Any]:
    if SUPABASE_SERVICE_ROLE_KEY:
        status, rows = _learning_db_request(f"{PLATFORM_TABLE}?id=eq.primary&select=state&limit=1")
        if status == 200 and isinstance(rows, list):
            if rows and isinstance(rows[0].get("state"), dict):
                return rows[0]["state"]
            return _platform_default_state()
        logger.error("Platform state table unavailable (HTTP %s); platform writes will be blocked until SQL setup", status)
    try:
        if os.path.exists(PLATFORM_FILE_PATH):
            with open(PLATFORM_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                default = _platform_default_state()
                default.update(data)
                return default
    except Exception as exc:
        logger.warning("Platform state file could not be loaded: %s", exc)
    return _platform_default_state()


platform_state = _platform_load_state()
_platform_last_saved_state = copy.deepcopy(platform_state)


def _platform_save_state() -> None:
    global _platform_last_saved_state
    snapshot = copy.deepcopy(platform_state)
    if SUPABASE_SERVICE_ROLE_KEY:
        url = f"{SUPABASE_URL}/rest/v1/{PLATFORM_TABLE}?on_conflict=id"
        headers = {"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                   "Content-Type": "application/json", "Accept": "application/json",
                   "Prefer": "resolution=merge-duplicates,return=minimal"}
        payload = [{"id": "primary", "state": snapshot, "updated_at": datetime.now(timezone.utc).isoformat()}]
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201, 204):
                    raise RuntimeError(f"HTTP {resp.status}")
            _platform_last_saved_state = copy.deepcopy(snapshot)
            return
        except Exception as exc:
            logger.error("Platform durable save failed: %s", exc)
            platform_state.clear(); platform_state.update(copy.deepcopy(_platform_last_saved_state))
            raise HTTPException(status_code=503, detail="تعذر حفظ التغيير في قاعدة البيانات الدائمة. راجع جدول المنصة وإعدادات Supabase.")
    try:
        parent = os.path.dirname(os.path.abspath(PLATFORM_FILE_PATH))
        os.makedirs(parent, exist_ok=True)
        tmp = PLATFORM_FILE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, PLATFORM_FILE_PATH)
        _platform_last_saved_state = copy.deepcopy(snapshot)
    except Exception as exc:
        logger.error("Platform local save failed: %s", exc)
        platform_state.clear(); platform_state.update(copy.deepcopy(_platform_last_saved_state))
        raise HTTPException(status_code=500, detail="تعذر حفظ بيانات المنصة محليًا.")


def _platform_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _platform_user_public(user: Dict[str, Any]) -> Dict[str, Any]:
    vip = user.get("vip") or {}
    active_vip = bool(vip.get("active") and (not vip.get("expires_at") or str(vip.get("expires_at")) > _platform_now()))
    return {"id": user.get("id"), "display_id": user.get("display_id", user.get("id")), "username": user.get("username"), "role": user.get("role", "USER"),
            "profile": user.get("profile", {}), "badges": user.get("badges", []), "featured": user.get("id") in platform_state.get("featured_user_ids", []), "vip": {**vip, "active": active_vip},
            "created_at": user.get("created_at"), "last_active": user.get("last_active"), "banned": bool(user.get("banned"))}


def _platform_password_hash(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 310000).hex()
    return salt, digest


def _platform_find_session(authorization: Optional[str]) -> Dict[str, Any]:
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="سجّل الدخول أولًا.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _platform_lock:
        session = platform_state.get("sessions", {}).get(token_hash)
        if not session or float(session.get("expires", 0)) <= time.time():
            platform_state.get("sessions", {}).pop(token_hash, None)
            raise HTTPException(status_code=401, detail="انتهت الجلسة؛ سجّل الدخول مرة أخرى.")
        user = platform_state.get("users", {}).get(session.get("user_key"))
        if not user or user.get("banned"):
            raise HTTPException(status_code=403, detail="الحساب غير متاح أو موقوف.")
        user["last_active"] = _platform_now()
        return user


def _platform_require_admin(authorization: Optional[str], allow_moderator: bool = False) -> Dict[str, Any]:
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    # The existing server-authenticated owner session is also allowed to administer platform data.
    if token:
        try:
            _require_admin(token)
            return {"id": "SERVER-OWNER", "username": "PHANTOM OWNER", "role": "OWNER"}
        except HTTPException:
            pass
    user = _platform_find_session(authorization)
    allowed = {"OWNER", "ADMIN"} | ({"MODERATOR"} if allow_moderator else set())
    if user.get("role") not in allowed:
        raise HTTPException(status_code=403, detail="هذه العملية تتطلب صلاحية إدارية.")
    return user


def _platform_audit(actor: Dict[str, Any], action: str, target: str = "", detail: str = "") -> None:
    platform_state.setdefault("audit", []).append({"id": secrets.token_hex(8), "actor_id": actor.get("id"),
        "actor_username": actor.get("username"), "action": action, "target": target, "detail": detail[:500], "at": _platform_now()})
    platform_state["audit"] = platform_state["audit"][-2000:]


@app.get("/api/platform/release")
def platform_release_info():
    return {"app": "BOOM - PREMIUM QUILT EDITION", "release": "v123.2", "prediction_engine": "FARM_ONLY", "retired_engines": ["DRAGON_TIGER", "POKER"], "auth_system": "platform_state"}


@app.post("/api/platform/auth/signup")
def platform_signup(payload: Dict[str, Any]):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    confirm = str(payload.get("confirm_password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,24}", username):
        raise HTTPException(status_code=400, detail="اسم المستخدم يجب أن يكون 3–24 حرفًا إنجليزيًا أو رقمًا أو . _ -")
    if len(password) < 8 or len(password) > 128:
        raise HTTPException(status_code=400, detail="كلمة المرور يجب ألا تقل عن 8 أحرف.")
    if password != confirm:
        raise HTTPException(status_code=400, detail="كلمتا المرور غير متطابقتين.")
    key = username.casefold()
    with _platform_lock:
        if key in platform_state["users"]:
            raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم بالفعل.")
        salt, digest = _platform_password_hash(password)
        user_id = "PH-" + secrets.token_hex(4).upper()
        while any(u.get("id") == user_id for u in platform_state["users"].values()):
            user_id = "PH-" + secrets.token_hex(4).upper()
        owner_name = os.environ.get("PHANTOM_OWNER_USERNAME", "").strip().casefold()
        role = "OWNER" if owner_name and key == owner_name else "USER"
        user = {"id": user_id, "display_id": user_id, "username": username, "password_salt": salt, "password_hash": digest, "role": role,
                "profile": {"display_name": username, "avatar": "", "banner": "", "bio": "", "city": "", "governorate": "", "relationship": ""},
                "badges": ([{"name": "App Owner", "rarity": "legendary"}] if role == "OWNER" else []),
                "vip": {"active": False, "plan_days": 0, "started_at": None, "expires_at": None},
                "created_at": _platform_now(), "last_active": _platform_now(), "banned": False, "blocked_users": []}
        platform_state["users"][key] = user
        token = secrets.token_urlsafe(40)
        platform_state.setdefault("sessions", {})[hashlib.sha256(token.encode()).hexdigest()] = {"user_key": key, "expires": time.time() + 7 * 86400, "created_at": _platform_now()}
        _platform_audit(user, "USER_SIGNUP", user_id)
        _platform_save_state()
    return {"ok": True, "token": token, "user": _platform_user_public(user), "expires_in": 604800}


@app.post("/api/platform/auth/login")
def platform_login(payload: Dict[str, Any]):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    key = username.casefold()
    with _platform_lock:
        user = platform_state.get("users", {}).get(key)
        if not user:
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة.")
        _, digest = _platform_password_hash(password, user.get("password_salt", ""))
        if not hmac.compare_digest(digest, str(user.get("password_hash", ""))):
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة.")
        if user.get("banned"):
            raise HTTPException(status_code=403, detail="الحساب موقوف؛ تواصل مع الدعم.")
        token = secrets.token_urlsafe(40)
        platform_state.setdefault("sessions", {})[hashlib.sha256(token.encode()).hexdigest()] = {"user_key": key, "expires": time.time() + 7 * 86400, "created_at": _platform_now()}
        user["last_active"] = _platform_now()
        _platform_save_state()
    return {"ok": True, "token": token, "user": _platform_user_public(user), "expires_in": 604800}


@app.post("/api/platform/auth/logout")
def platform_logout(authorization: Optional[str] = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token:
        with _platform_lock:
            platform_state.get("sessions", {}).pop(hashlib.sha256(token.encode()).hexdigest(), None)
            _platform_save_state()
    return {"ok": True}


@app.get("/api/platform/me")
def platform_me(authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    return {"ok": True, "user": _platform_user_public(user), "settings": platform_state.get("settings", {})}


@app.post("/api/platform/media/upload")
def platform_media_upload(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    data_url = str(payload.get("data_url", ""))
    match = re.fullmatch(r"data:(image/(?:jpeg|png|webp|gif));base64,([A-Za-z0-9+/=]+)", data_url)
    if not match: raise HTTPException(status_code=400, detail="صيغة الصورة غير مدعومة. استخدم JPG أو PNG أو WEBP أو GIF.")
    mime, encoded = match.groups()
    try: raw = base64.b64decode(encoded, validate=True)
    except Exception: raise HTTPException(status_code=400, detail="بيانات الصورة غير صالحة.")
    if not raw or len(raw) > 950 * 1024: raise HTTPException(status_code=413, detail="حجم الصورة يجب ألا يتجاوز 950 كيلوبايت.")
    if not SUPABASE_SERVICE_ROLE_KEY:
        # Development fallback only; configure Supabase Storage for durable hosted media.
        return {"ok": True, "url": data_url, "storage": "inline_local_fallback"}
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}[mime]
    object_path = f"users/{user.get('id')}/{secrets.token_hex(12)}.{extension}"
    url = f"{SUPABASE_URL}/storage/v1/object/phantom-media/{object_path}"
    headers = {"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
               "Content-Type": mime, "x-upsert": "false"}
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 201): raise RuntimeError(f"Storage HTTP {resp.status}")
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/phantom-media/{object_path}"
        return {"ok": True, "url": public_url, "storage": "supabase_storage"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:400]
        logger.error("Platform media upload failed: %s", detail)
        raise HTTPException(status_code=502, detail="تعذر رفع الصورة إلى Supabase Storage. تأكد من إنشاء bucket باسم phantom-media.")
    except Exception as exc:
        logger.error("Platform media upload failed: %s", exc)
        raise HTTPException(status_code=502, detail="فشل رفع الصورة إلى التخزين الدائم.")


@app.patch("/api/platform/profile")
def platform_update_profile(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    allowed = {"display_name", "bio", "avatar", "banner", "city", "governorate", "relationship"}
    profile = user.setdefault("profile", {})
    for field in allowed:
        if field in payload:
            value = str(payload[field] or "").strip()
            if field in {"avatar", "banner"} and len(value) > 1_500_000:
                raise HTTPException(status_code=413, detail="الصورة كبيرة جدًا؛ الحد الأقصى 1.5 ميجابايت.")
            if field not in {"avatar", "banner"} and len(value) > 500:
                raise HTTPException(status_code=400, detail=f"القيمة طويلة جدًا للحقل {field}.")
            profile[field] = value
    with _platform_lock:
        _platform_save_state()
    return {"ok": True, "user": _platform_user_public(user)}


@app.get("/api/platform/users")
def platform_users(q: str = "", authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    q = q.strip().casefold()
    with _platform_lock:
        values = [_platform_user_public(u) for u in platform_state.get("users", {}).values()
                  if (not q or q in str(u.get("username", "")).casefold() or q in str(u.get("id", "")).casefold()) and not u.get("banned")]
        values.sort(key=lambda u: (u.get("id") != next((x.get("id") for x in platform_state["users"].values() if x.get("role") == "OWNER"), None), u.get("username", "").casefold()))
        featured = platform_state.get("featured_user_ids", [])
        values.sort(key=lambda u: (u.get("role") != "OWNER", u.get("id") not in featured, u.get("username", "").casefold()))
        return {"ok": True, "users": values[:100], "featured_user_ids": featured}


@app.get("/api/platform/apps")
def platform_apps(authorization: Optional[str] = Header(default=None)):
    _platform_find_session(authorization)
    return {"ok": True, "apps": platform_state.get("apps", []), "engine": "FARM"}


@app.get("/api/platform/moments")
def platform_moments(authorization: Optional[str] = Header(default=None)):
    _platform_find_session(authorization)
    with _platform_lock:
        moments = list(reversed(platform_state.get("moments", [])[-200:]))
        users = platform_state.get("users", {})
        for moment in moments:
            owner = next((u for u in users.values() if u.get("id") == moment.get("user_id")), None)
            moment["author"] = _platform_user_public(owner) if owner else {"id": moment.get("user_id"), "username": "محذوف", "profile": {}}
        return {"ok": True, "moments": moments, "enabled": platform_state.get("settings", {}).get("moments_enabled", True)}


@app.post("/api/platform/moments")
def platform_create_moment(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    if not platform_state.get("settings", {}).get("moments_enabled", True):
        raise HTTPException(status_code=403, detail="قسم اللحظات متوقف حاليًا.")
    content = str(payload.get("content", "")).strip()
    media = str(payload.get("media", "")).strip()
    if not content and not media:
        raise HTTPException(status_code=400, detail="اكتب منشورًا أو أضف صورة.")
    if len(content) > 3000 or len(media) > 1_500_000:
        raise HTTPException(status_code=413, detail="حجم المنشور أو الصورة أكبر من الحد المسموح.")
    with _platform_lock:
        moment = {"id": secrets.token_hex(8), "user_id": user["id"], "content": content, "media": media,
                  "likes": [], "comments": [], "created_at": _platform_now()}
        platform_state.setdefault("moments", []).append(moment)
        platform_state["moments"] = platform_state["moments"][-1000:]
        _platform_save_state()
    return {"ok": True, "moment": moment}


@app.post("/api/platform/moments/{moment_id}/like")
def platform_like_moment(moment_id: str, authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    with _platform_lock:
        moment = next((m for m in platform_state.get("moments", []) if m.get("id") == moment_id), None)
        if not moment: raise HTTPException(status_code=404, detail="المنشور غير موجود.")
        likes = moment.setdefault("likes", [])
        if user["id"] in likes: likes.remove(user["id"])
        else: likes.append(user["id"])
        _platform_save_state()
        return {"ok": True, "liked": user["id"] in likes, "likes": len(likes)}


@app.post("/api/platform/moments/{moment_id}/comments")
def platform_comment_moment(moment_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    content = str(payload.get("content", "")).strip()
    if not content or len(content) > 1000: raise HTTPException(status_code=400, detail="اكتب تعليقًا من 1 إلى 1000 حرف.")
    with _platform_lock:
        moment = next((m for m in platform_state.get("moments", []) if m.get("id") == moment_id), None)
        if not moment: raise HTTPException(status_code=404, detail="المنشور غير موجود.")
        moment.setdefault("comments", []).append({"id": secrets.token_hex(6), "user_id": user["id"], "username": user["username"], "content": content, "created_at": _platform_now()})
        moment["comments"] = moment["comments"][-300:]
        _platform_save_state()
        return {"ok": True, "comments": moment["comments"]}


@app.get("/api/platform/messages")
def platform_messages(with_user: str = "", authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    with _platform_lock:
        messages = [m for m in platform_state.get("messages", []) if user.get("id") in (m.get("from_id"), m.get("to_id"))]
        if with_user:
            messages = [m for m in messages if with_user in (m.get("from_id"), m.get("to_id"))]
        messages = messages[-300:]
        messages.sort(key=lambda m: (not bool(m.get("pinned")), str(m.get("created_at", ""))))
        users = {u.get("id"): _platform_user_public(u) for u in platform_state.get("users", {}).values()}
        return {"ok": True, "messages": messages, "users": users, "chat_enabled": platform_state.get("settings", {}).get("chat_enabled", True)}


@app.post("/api/platform/messages")
def platform_send_message(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    if not platform_state.get("settings", {}).get("chat_enabled", True): raise HTTPException(status_code=403, detail="الدردشة متوقفة حاليًا.")
    to_id = str(payload.get("to_id", "")).strip()
    content = str(payload.get("content", "")).strip()
    if not to_id or not content or len(content) > 3000: raise HTTPException(status_code=400, detail="المستلم والرسالة مطلوبان، والحد الأقصى 3000 حرف.")
    with _platform_lock:
        target = next((u for u in platform_state.get("users", {}).values() if u.get("id") == to_id), None)
        if not target: raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
        if user["id"] in target.get("blocked_users", []) or to_id in user.get("blocked_users", []): raise HTTPException(status_code=403, detail="لا يمكن إرسال الرسالة بسبب إعدادات الحظر.")
        message = {"id": secrets.token_hex(8), "from_id": user["id"], "from_username": user["username"], "to_id": to_id,
                   "to_username": target["username"], "content": content, "created_at": _platform_now(), "pinned": False, "read": False}
        platform_state.setdefault("messages", []).append(message)
        platform_state["messages"] = platform_state["messages"][-5000:]
        _platform_save_state()
        return {"ok": True, "message": message}


@app.post("/api/platform/password")
def platform_change_password(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    current = str(payload.get("current_password", "")); new = str(payload.get("new_password", "")); confirm = str(payload.get("confirm_password", ""))
    _, current_hash = _platform_password_hash(current, user.get("password_salt", ""))
    if not hmac.compare_digest(current_hash, str(user.get("password_hash", ""))): raise HTTPException(status_code=401, detail="كلمة المرور الحالية غير صحيحة.")
    if len(new) < 8 or len(new) > 128: raise HTTPException(status_code=400, detail="كلمة المرور الجديدة يجب أن تكون من 8 إلى 128 حرفًا.")
    if new != confirm: raise HTTPException(status_code=400, detail="تأكيد كلمة المرور غير مطابق.")
    salt, digest = _platform_password_hash(new)
    raw_token = (authorization or "").strip()
    if raw_token.lower().startswith("bearer "): raw_token = raw_token[7:].strip()
    current_session_hash = hashlib.sha256(raw_token.encode()).hexdigest() if raw_token else ""
    with _platform_lock:
        user["password_salt"] = salt; user["password_hash"] = digest
        for session_hash, session in list(platform_state.get("sessions", {}).items()):
            if session.get("user_key") == str(user.get("username", "")).casefold() and session_hash != current_session_hash:
                platform_state["sessions"].pop(session_hash, None)
        _platform_audit(user, "PASSWORD_CHANGED", user.get("id", ""))
        _platform_save_state()
    return {"ok": True, "message": "تم تغيير كلمة المرور بنجاح."}


@app.post("/api/platform/block/{user_id}")
def platform_block_user(user_id: str, authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    if user_id == user.get("id"): raise HTTPException(status_code=400, detail="لا يمكنك حظر حسابك.")
    target = next((u for u in platform_state.get("users", {}).values() if u.get("id") == user_id), None)
    if not target: raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
    with _platform_lock:
        blocked = user.setdefault("blocked_users", [])
        if user_id in blocked: blocked.remove(user_id)
        else: blocked.append(user_id)
        _platform_save_state()
    return {"ok": True, "blocked": user_id in user.get("blocked_users", [])}


@app.post("/api/platform/messages/{message_id}/report")
def platform_report_message(message_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    reason = str(payload.get("reason", "")).strip()[:500]
    with _platform_lock:
        message = next((m for m in platform_state.get("messages", []) if m.get("id") == message_id), None)
        if not message or user.get("id") not in (message.get("from_id"), message.get("to_id")): raise HTTPException(status_code=404, detail="الرسالة غير موجودة أو ليست ضمن محادثاتك.")
        report = {"id": secrets.token_hex(8), "message_id": message_id, "reporter_id": user.get("id"), "reason": reason or "بلاغ بدون تفاصيل", "status": "open", "created_at": _platform_now()}
        platform_state.setdefault("reports", []).append(report); platform_state["reports"] = platform_state["reports"][-1000:]
        _platform_save_state()
    return {"ok": True, "report": report}


@app.post("/api/platform/vip/request")
def platform_vip_request(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    days = int(payload.get("days", 30))
    if days not in (30, 150, 365): raise HTTPException(status_code=400, detail="خطة الاشتراك غير مدعومة.")
    with _platform_lock:
        platform_state.setdefault("audit", []).append({"id": secrets.token_hex(8), "action": "VIP_PAYMENT_REQUEST", "actor_id": user["id"], "target": user["id"], "detail": f"طلب VIP {days} يوم - مراجعة يدوية", "at": _platform_now()})
        platform_state.setdefault("vip_requests", []).append({"id": secrets.token_hex(8), "user_id": user["id"], "username": user["username"], "days": days, "status": "pending", "created_at": _platform_now(), "method": str(payload.get("method", "agent"))[:40]})
        _platform_save_state()
    return {"ok": True, "message": "تم تسجيل طلب الاشتراك للمراجعة اليدوية. لم يتم تفعيل VIP أو تأكيد الدفع تلقائيًا."}


@app.get("/api/platform/admin/summary")
def platform_admin_summary(authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization, allow_moderator=True)
    users = list(platform_state.get("users", {}).values())
    active_vip = sum(1 for u in users if _platform_user_public(u).get("vip", {}).get("active"))
    return {"ok": True, "total_users": len(users), "vip_users": active_vip, "moments": len(platform_state.get("moments", [])),
            "messages": len(platform_state.get("messages", [])), "pending_vip": sum(1 for r in platform_state.get("vip_requests", []) if r.get("status") == "pending"),
            "audit": list(reversed(platform_state.get("audit", [])[-100:])), "role": actor.get("role"), "storage": "supabase" if SUPABASE_SERVICE_ROLE_KEY else "local_file"}


@app.get("/api/platform/admin/users")
def platform_admin_users(q: str = "", authorization: Optional[str] = Header(default=None)):
    _platform_require_admin(authorization, allow_moderator=True)
    q = q.casefold().strip()
    users = [_platform_user_public(u) for u in platform_state.get("users", {}).values() if not q or q in u.get("username", "").casefold() or q in u.get("id", "").casefold()]
    return {"ok": True, "users": users[:500]}


@app.patch("/api/platform/admin/users/{user_id}")
def platform_admin_update_user(user_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    with _platform_lock:
        target = next((u for u in platform_state.get("users", {}).values() if u.get("id") == user_id), None)
        if not target: raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
        if target.get("role") == "OWNER" and actor.get("role") != "OWNER": raise HTTPException(status_code=403, detail="لا يمكن تعديل حساب المالك إلا بواسطة المالك.")
        if "banned" in payload: target["banned"] = bool(payload["banned"])
        if "role" in payload:
            role = str(payload["role"]).upper()
            if role not in {"USER", "MODERATOR", "ADMIN", "OWNER"}: raise HTTPException(status_code=400, detail="الدور غير مسموح.")
            if actor.get("role") != "OWNER": raise HTTPException(status_code=403, detail="تعيين الأدوار الحساسة للمالك فقط.")
            target["role"] = role
        if "featured" in payload:
            featured = platform_state.setdefault("featured_user_ids", [])
            if bool(payload["featured"]) and user_id not in featured: featured.append(user_id)
            if not bool(payload["featured"]) and user_id in featured: featured.remove(user_id)
        if "badges" in payload and actor.get("role") != "OWNER": raise HTTPException(status_code=403, detail="إدارة الشارات للمالك فقط.")
        if "badges" in payload and actor.get("role") == "OWNER":
            badges = payload["badges"] if isinstance(payload["badges"], list) else []
            target["badges"] = [{"name": str(b.get("name", ""))[:60], "rarity": str(b.get("rarity", "rare"))[:30]} for b in badges[:50] if isinstance(b, dict) and str(b.get("name", "")).strip()]
        if "display_id" in payload:
            if actor.get("role") != "OWNER": raise HTTPException(status_code=403, detail="تخصيص الآيدي المميز للمالك فقط.")
            custom_id = str(payload.get("display_id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", custom_id): raise HTTPException(status_code=400, detail="الآيدي المميز يجب أن يكون 3–20 حرفًا/رقمًا أو - _")
            if any(u.get("id") != user_id and u.get("display_id", u.get("id")) == custom_id for u in platform_state["users"].values()): raise HTTPException(status_code=409, detail="الآيدي المميز مستخدم بالفعل.")
            target["display_id"] = custom_id
        _platform_audit(actor, "USER_UPDATE", user_id, json.dumps({k: v for k, v in payload.items() if k != "password"}, ensure_ascii=False))
        _platform_save_state()
        return {"ok": True, "user": _platform_user_public(target)}


@app.post("/api/platform/admin/vip")
def platform_admin_vip(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    user_id = str(payload.get("user_id", "")).strip()
    days = int(payload.get("days", 30))
    active = bool(payload.get("active", True))
    if days not in (30, 150, 365, 0): raise HTTPException(status_code=400, detail="المدة يجب أن تكون 30 أو 150 أو 365 يومًا أو 0 للإلغاء.")
    with _platform_lock:
        target = next((u for u in platform_state.get("users", {}).values() if u.get("id") == user_id or u.get("username", "").casefold() == user_id.casefold()), None)
        if not target: raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
        now = datetime.now(timezone.utc)
        if not active or days == 0:
            target["vip"] = {"active": False, "plan_days": 0, "started_at": None, "expires_at": None}
        else:
            prior = target.get("vip", {})
            try: old_exp = datetime.fromisoformat(str(prior.get("expires_at", "")).replace("Z", "+00:00"))
            except Exception: old_exp = now
            start = old_exp if prior.get("active") and old_exp > now else now
            expiry = datetime.fromtimestamp(start.timestamp() + days * 86400, timezone.utc)
            target["vip"] = {"active": True, "plan_days": days, "started_at": now.isoformat().replace("+00:00", "Z"), "expires_at": expiry.isoformat().replace("+00:00", "Z")}
        _platform_audit(actor, "VIP_UPDATE", user_id, json.dumps(target["vip"], ensure_ascii=False))
        _platform_save_state()
        return {"ok": True, "user": _platform_user_public(target)}


@app.get("/api/platform/admin/vip-requests")
def platform_admin_vip_requests(authorization: Optional[str] = Header(default=None)):
    _platform_require_admin(authorization)
    return {"ok": True, "requests": list(reversed(platform_state.get("vip_requests", [])[-500:]))}


@app.patch("/api/platform/admin/vip-requests/{request_id}")
def platform_admin_review_vip_request(request_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    status = str(payload.get("status", "")).lower()
    if status not in {"approved", "rejected"}: raise HTTPException(status_code=400, detail="حالة المراجعة غير صحيحة.")
    with _platform_lock:
        req = next((r for r in platform_state.get("vip_requests", []) if r.get("id") == request_id), None)
        if not req: raise HTTPException(status_code=404, detail="طلب الاشتراك غير موجود.")
        req["status"] = status; req["reviewed_at"] = _platform_now(); req["reviewed_by"] = actor.get("id")
        if status == "approved":
            target = next((u for u in platform_state.get("users", {}).values() if u.get("id") == req.get("user_id")), None)
            if target:
                now = datetime.now(timezone.utc); days = int(req.get("days", 30))
                target["vip"] = {"active": True, "plan_days": days, "started_at": now.isoformat().replace("+00:00", "Z"), "expires_at": datetime.fromtimestamp(now.timestamp()+days*86400, timezone.utc).isoformat().replace("+00:00", "Z")}
        _platform_audit(actor, "VIP_REQUEST_REVIEW", request_id, status)
        _platform_save_state()
    return {"ok": True, "request": req}


@app.get("/api/platform/admin/messages")
def platform_admin_messages(authorization: Optional[str] = Header(default=None)):
    _platform_require_admin(authorization, allow_moderator=True)
    return {"ok": True, "messages": list(reversed(platform_state.get("messages", [])[-200:])), "reports": list(reversed(platform_state.get("reports", [])[-200:]))}


@app.patch("/api/platform/admin/messages/{message_id}")
def platform_admin_message_update(message_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization, allow_moderator=True)
    with _platform_lock:
        message = next((m for m in platform_state.get("messages", []) if m.get("id") == message_id), None)
        if not message: raise HTTPException(status_code=404, detail="الرسالة غير موجودة.")
        if "pinned" in payload: message["pinned"] = bool(payload["pinned"])
        if payload.get("delete") is True: platform_state["messages"] = [m for m in platform_state.get("messages", []) if m.get("id") != message_id]
        _platform_audit(actor, "MESSAGE_MODERATION", message_id, json.dumps(payload, ensure_ascii=False))
        _platform_save_state()
    return {"ok": True}


@app.patch("/api/platform/admin/reports/{report_id}")
def platform_admin_report_update(report_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization, allow_moderator=True)
    status = str(payload.get("status", "reviewed"))
    if status not in {"reviewed", "dismissed", "actioned"}: raise HTTPException(status_code=400, detail="حالة البلاغ غير صحيحة.")
    with _platform_lock:
        report = next((r for r in platform_state.get("reports", []) if r.get("id") == report_id), None)
        if not report: raise HTTPException(status_code=404, detail="البلاغ غير موجود.")
        report["status"] = status; report["reviewed_at"] = _platform_now(); report["reviewed_by"] = actor.get("id")
        _platform_audit(actor, "REPORT_REVIEW", report_id, status)
        _platform_save_state()
    return {"ok": True, "report": report}


@app.delete("/api/platform/admin/moments/{moment_id}")
def platform_admin_delete_moment(moment_id: str, authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization, allow_moderator=True)
    with _platform_lock:
        before = len(platform_state.get("moments", []))
        platform_state["moments"] = [m for m in platform_state.get("moments", []) if m.get("id") != moment_id]
        if len(platform_state["moments"]) == before: raise HTTPException(status_code=404, detail="المنشور غير موجود.")
        _platform_audit(actor, "MOMENT_DELETE", moment_id)
        _platform_save_state()
    return {"ok": True}


@app.post("/api/platform/admin/apps")
def platform_admin_add_app(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    name = str(payload.get("name", "")).strip()
    app_id = re.sub(r"[^a-z0-9_-]+", "", str(payload.get("id", name.casefold().replace(" ", "-"))).casefold())[:40]
    if len(app_id) < 2: app_id = "app-" + secrets.token_hex(4)
    if len(name) < 2 or len(name) > 60: raise HTTPException(status_code=400, detail="اسم التطبيق غير صالح.")
    with _platform_lock:
        if any(a.get("id") == app_id or a.get("name", "").casefold() == name.casefold() for a in platform_state.get("apps", [])):
            raise HTTPException(status_code=409, detail="التطبيق أو المعرّف موجود بالفعل.")
        item = {"id": app_id, "name": name, "active": True, "engine": "FARM"}
        platform_state.setdefault("apps", []).append(item)
        _platform_audit(actor, "APP_ADD", app_id, name)
        _platform_save_state()
    return {"ok": True, "app": item}


@app.patch("/api/platform/admin/apps/{app_id}")
def platform_admin_app_update(app_id: str, payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    with _platform_lock:
        app_item = next((a for a in platform_state.get("apps", []) if a.get("id") == app_id), None)
        if not app_item: raise HTTPException(status_code=404, detail="التطبيق غير موجود.")
        if "active" in payload: app_item["active"] = bool(payload["active"])
        # All supported integrations are deliberately constrained to the Farm engine.
        app_item["engine"] = "FARM"
        _platform_audit(actor, "APP_UPDATE", app_id, json.dumps(payload, ensure_ascii=False))
        _platform_save_state()
        return {"ok": True, "app": app_item}


@app.patch("/api/platform/admin/settings")
def platform_admin_settings(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    allowed = {"chat_enabled", "moments_enabled", "sound_enabled", "theme"}
    with _platform_lock:
        for key in allowed:
            if key in payload: platform_state.setdefault("settings", {})[key] = payload[key]
        _platform_audit(actor, "SETTINGS_UPDATE", "settings", json.dumps(payload, ensure_ascii=False))
        _platform_save_state()
    return {"ok": True, "settings": platform_state["settings"]}


@app.post("/api/platform/admin/announcements")
def platform_admin_announcement(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    actor = _platform_require_admin(authorization)
    text_value = str(payload.get("text", "")).strip()
    if not text_value or len(text_value) > 500: raise HTTPException(status_code=400, detail="الإعلان مطلوب وبحد أقصى 500 حرف.")
    with _platform_lock:
        item = {"id": secrets.token_hex(6), "text": text_value, "pinned": bool(payload.get("pinned", True)), "created_at": _platform_now(), "created_by": actor.get("id")}
        platform_state.setdefault("announcements", []).append(item); platform_state["announcements"] = platform_state["announcements"][-100:]
        _platform_audit(actor, "ANNOUNCEMENT_CREATE", item["id"], text_value)
        _platform_save_state()
    return {"ok": True, "announcement": item}


@app.get("/api/platform/announcements")
def platform_announcements(authorization: Optional[str] = Header(default=None)):
    _platform_find_session(authorization)
    return {"ok": True, "announcements": list(reversed(platform_state.get("announcements", [])[-50:]))}


@app.post("/api/platform/predict")
async def platform_predict(payload: Dict[str, Any], authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    history = payload.get("history", [])
    if not isinstance(history, list): raise HTTPException(status_code=400, detail="سجل المزرعة يجب أن يكون قائمة.")
    is_vip = bool(_platform_user_public(user).get("vip", {}).get("active"))
    max_history = 100 if is_vip else 8
    clean_history = [str(x) for x in history][-max_history:]
    result = await predict_endpoint(PredictionRequest(game_type="FARM", history=clean_history, room_id="platform-farm", coins=0), authorization=authorization)
    record = {"id": secrets.token_hex(8), "user_id": user["id"], "mode": "VIP" if is_vip else "FREE",
              "history_count": len(clean_history), "prediction": result.get("prediction", ""), "confidence": result.get("confidence"),
              "created_at": _platform_now(), "source": "history_analysis_not_live_game_feed"}
    with _platform_lock:
        platform_state.setdefault("predictions", []).append(record); platform_state["predictions"] = platform_state["predictions"][-5000:]
        _platform_save_state()
    return {"ok": True, "record": record, "analysis": result, "notice": ("تحليل إحصائي للسجل المدخل فقط؛ لا يوجد اتصال مباشر مؤكد بنتائج اللعبة ولا ضمان للنتيجة التالية." if is_vip else "الوضع المجاني يستخدم آخر 8 نتائج فقط. التحليل إحصائي وليس اتصالًا مباشرًا بسيرفر اللعبة ولا ضمانًا للنتيجة التالية.")}


@app.get("/api/platform/predictions/history")
def platform_prediction_history(authorization: Optional[str] = Header(default=None)):
    user = _platform_find_session(authorization)
    rows = [r for r in platform_state.get("predictions", []) if r.get("user_id") == user.get("id")]
    return {"ok": True, "history": list(reversed(rows[-100:]))}


@app.get("/")
def root():
    return {
        "status": "PHANTOM ELITE Platform & Farm Engine v122.0 Online",
        "endpoints": [
            "/api/platform/auth/signup", "/api/platform/auth/login", "/api/platform/me",
            "/api/platform/apps", "/api/platform/moments", "/api/platform/messages",
            "/api/platform/vip/request", "/api/platform/predict", "/api/platform/admin/summary",
            "/api/predict",
            "/api/health",
            "/api/config",
            "/api/admin/login",
            "/api/admin/telemetry",
            "/api/license/verify",
        ],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
