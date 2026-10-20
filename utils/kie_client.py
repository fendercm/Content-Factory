"""
KIE.ai — единый платный провайдер сериала: LLM, картинки, видео, загрузка файлов.

Проверено 2026-10-19/20 живыми вызовами (см. scripts/model_bakeoff.py, scripts/animation_bakeoff.py).

Медиа:   POST /api/v1/jobs/createTask {model, input} → taskId
         GET  /api/v1/jobs/recordInfo?taskId=… → state, resultJson.resultUrls, creditsConsumed
LLM:     claude:<m>  → POST /claude/v1/messages                (Anthropic Messages)
         gemini:<m>  → POST /<m>-openai/v1/chat/completions    (OpenAI Chat, стрим: Gemini 4 Argon
                                                                думает > 100 с, без стрима Cloudflare рвёт 524)
         codex:<m>   → POST /codex/v1/responses                (OpenAI Responses — GPT)
         openai:<m>  → POST /openai/v1/responses               (OpenAI Responses — DeepSeek)
Файлы:   POST https://kieai.redpandaai.co/api/file-stream-upload (multipart) → data.downloadUrl,
         файл живёт 24 часа — референсы загружаем в каждом прогоне.

KIE отдаёт ошибки с HTTP 200 — всегда смотрим на code в теле. 200 кредитов = $1.
Платный провайдер: вызовы гейтятся utils.providers.is_enabled("kie_series") — отдельно от "kie",
которым гейтятся старые пути (Kling в visual_agent, ElevenLabs и Suno через KIE).
"""

import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE = "https://api.kie.ai"
UPLOAD_URLS = (
    "https://kieai.redpandaai.co/api/file-stream-upload",
    "https://api.kie.ai/api/file-stream-upload",
)
CREDITS_PER_USD = 200


class KieError(RuntimeError):
    pass


def _key() -> str:
    key = os.getenv("KIE_API_KEY", "")
    if not key:
        raise KieError("KIE_API_KEY не задан в .env")
    from utils.providers import is_enabled
    if not is_enabled("kie_series"):
        raise KieError("KIE выключен (BUDGET_MODE=free без BUDGET_ALLOW=kie_series, DISABLED_PROVIDERS "
                       "или кончились кредиты)")
    return key


def _headers() -> dict:
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


def _check(data: dict, what: str) -> dict:
    if data.get("type") == "error":
        raise KieError(f"{what}: {json.dumps(data.get('error'), ensure_ascii=False)[:300]}")
    code = data.get("code")
    if code not in (None, 200):
        from utils.providers import looks_like_no_credits, mark_dead
        msg = f"{what}: {code} {data.get('msg')}"
        if code == 402 or looks_like_no_credits(msg):
            mark_dead("kie_series", msg)
            mark_dead("kie", msg)
        raise KieError(msg)
    return data


def _track(operation: str, credits: float | None, model: str) -> None:
    if not credits:
        return
    try:
        from utils.cost_tracker import log_cost
        log_cost(f"kie:{model}", "kie", operation, 1, unit_cost_usd=credits / CREDITS_PER_USD)
    except Exception:
        pass


def balance() -> float:
    data = requests.get(f"{BASE}/api/v1/chat/credit", headers=_headers(), timeout=20).json()
    return float(_check(data, "balance").get("data") or 0)


# ── Файлы ─────────────────────────────────────────────────────────────────────

def upload_file(path: str | Path, upload_path: str = "content-factory") -> str:
    """Загружает локальный файл, возвращает URL (живёт 24 часа)."""
    path = Path(path)
    last: Exception | None = None
    for url in UPLOAD_URLS:
        try:
            with open(path, "rb") as f:
                resp = requests.post(url, headers={"Authorization": f"Bearer {_key()}"},
                                     files={"file": (path.name, f)},
                                     data={"uploadPath": upload_path, "fileName": path.name}, timeout=120)
            data = _check(resp.json(), "upload")
            return data["data"]["downloadUrl"]
        except (requests.RequestException, ValueError, KeyError, KieError) as exc:
            last = exc
    raise KieError(f"загрузка {path.name} не удалась: {last}")


# ── Медиа-задачи ──────────────────────────────────────────────────────────────

def create_task(model: str, input_: dict) -> str:
    data = requests.post(f"{BASE}/api/v1/jobs/createTask", headers=_headers(),
                         json={"model": model, "input": input_}, timeout=60).json()
    return _check(data, f"createTask {model}")["data"]["taskId"]


def wait_task(task_id: str, timeout_s: int = 1200, poll_s: int = 8, model: str = "") -> dict:
    """Ждёт задачу. → {"urls": [...], "credits": float, "seconds": int}"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data = requests.get(f"{BASE}/api/v1/jobs/recordInfo", headers=_headers(),
                                params={"taskId": task_id}, timeout=30).json()
        except (requests.RequestException, ValueError):
            time.sleep(poll_s)  # сетевой сбой при опросе — не терять оплаченную задачу
            continue
        info = _check(data, "recordInfo").get("data") or {}
        state = info.get("state")
        if state == "success":
            result = json.loads(info.get("resultJson") or "{}")
            urls = result.get("resultUrls") or (result.get("data") or {}).get("result_urls") or []
            credits = info.get("creditsConsumed")
            _track("task", credits, model or info.get("model", "?"))
            return {"urls": urls, "credits": credits, "seconds": info.get("costTime")}
        if state == "fail":
            raise KieError(f"задача {model or task_id}: {info.get('failCode')} {info.get('failMsg')}")
        time.sleep(poll_s)
    raise KieError(f"задача {model or task_id} не завершилась за {timeout_s} с")


def run_task(model: str, input_: dict, timeout_s: int = 1200) -> dict:
    return wait_task(create_task(model, input_), timeout_s, model=model)


def download(url: str, dest: str | Path) -> Path:
    dest = Path(dest)
    from utils.download import download_to
    download_to(url, str(dest), timeout=180, min_bytes=1_000)
    return dest


# ── LLM ───────────────────────────────────────────────────────────────────────

def _responses_text(data: dict) -> str:
    for item in data.get("output", []):
        if item.get("type") == "message":
            return "".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text")
    raise KieError("в ответе нет текста")


def _stream_chat(path: str, body: dict, timeout_s: int) -> tuple[str, float | None]:
    parts: list[str] = []
    credits = None
    with requests.post(f"{BASE}{path}", json=body, stream=True, timeout=(30, timeout_s),
                       headers=_headers()) as resp:
        for raw in resp.iter_lines():
            line = raw.decode("utf-8", "replace").strip()
            if line.lower().startswith("<!doctype html"):
                raise KieError(f"HTTP {resp.status_code}: таймаут шлюза KIE/Cloudflare")
            if not line.startswith("data:"):
                if line.startswith("{"):
                    _check(json.loads(line), "chat")
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            if "choices" not in chunk:
                _check(chunk, "chat")
            credits = chunk.get("credits_consumed", credits)
            for choice in chunk.get("choices", []):
                content = (choice.get("delta") or {}).get("content")
                if content:
                    parts.append(content)
    text = "".join(parts).strip()
    if not text:
        raise KieError("пустой потоковый ответ")
    return text, credits


def chat(model_spec: str, messages: list[dict], max_tokens: int = 4000, json_mode: bool = False,
         timeout_s: int = 600) -> tuple[str, float | None]:
    """
    model_spec: "<адаптер>:<модель>" — gemini:gemini-4-argon, codex:gpt-6-astra, claude:…, openai:…
    → (текст, кредиты)
    """
    adapter, model = model_spec.split(":", 1)
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user_msgs = [m for m in messages if m["role"] != "system"]

    if adapter == "gemini":
        body = {"messages": messages, "stream": True, "reasoning_effort": "low"}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        # Долгий стрим Argon (50-200 с) KIE иногда рвёт (ConnectionReset) — один повтор, потом резерв цепочки
        for attempt in range(2):
            try:
                text, credits = _stream_chat(f"/{model}-openai/v1/chat/completions", body, timeout_s)
                break
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                if attempt:
                    raise
                logger.warning("KIE %s: обрыв соединения (%s) — повтор", model, str(exc)[:120])
    elif adapter in ("codex", "openai"):
        path = "/codex/v1/responses" if adapter == "codex" else "/openai/v1/responses"
        body = {"model": model, "stream": False, "instructions": system,
                "input": [{"role": m["role"], "content": m["content"]} for m in user_msgs]}
        if adapter == "codex":
            body["reasoning"] = {"effort": "medium"}
        data = _check(requests.post(f"{BASE}{path}", headers=_headers(), json=body,
                                    timeout=timeout_s).json(), f"chat {model}")
        text, credits = _responses_text(data).strip(), data.get("credits_consumed")
    elif adapter == "claude":
        first = f"{system}\n\n" if system else ""
        msgs = [dict(m) for m in user_msgs]
        if msgs:
            msgs[0]["content"] = first + msgs[0]["content"]
        data = _check(requests.post(f"{BASE}/claude/v1/messages", headers=_headers(), timeout=timeout_s,
                                    json={"model": model, "stream": False, "max_tokens": max_tokens,
                                          "messages": msgs}).json(), f"chat {model}")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        credits = data.get("credits_consumed")
    else:
        raise ValueError(f"неизвестный адаптер KIE: {adapter}")

    _track("chat", credits, model)
    return text, credits
