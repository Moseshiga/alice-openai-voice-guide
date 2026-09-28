"""HTTPS-вебхук приватного навыка Алисы для Vercel.

Зависимостей, кроме стандартной библиотеки Python, нет. Все персональные
настройки передаются через переменные окружения Vercel.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


API_URL = "https://api.openai.com/v1/responses"
MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
MAX_ANSWER_CHARS = 900  # Лимит поля response.text у Алисы: 1024.
OPENAI_TIME_BUDGET_SECONDS = 3.6  # У Яндекс Диалогов всего 4,5 с, включая сеть.

INSTRUCTIONS = (
    "Ты голосовой помощник в приватном навыке Алисы. Отвечай на русском языке "
    "естественно и кратко: обычно одно-два предложения, до 40 слов. Пиши только текст для "
    "произнесения вслух, без Markdown, списков, ссылок и эмодзи. Если данных "
    "недостаточно, честно скажи об этом. Учитывай сказанное в этой беседе, "
    "но не обещай помнить его после выхода из навыка. Не заявляй, что выполнил "
    "действие во внешнем сервисе, если оно не было выполнено."
)
SEARCH_PROMPT = (
    "Для этого вопроса используй веб-поиск. Ответь на русском кратко, "
    "только по найденным данным. Назови источник обычными словами. "
    "Не произноси адреса сайтов. Если подтверждения нет, скажи об этом."
)
SEARCH_FOLLOWUP = ("что нашлось", "что нашёл", "ну что нашёл", "ну что нашлось")


class OpenAIError(Exception):
    """Ошибка обращения к OpenAI API."""


def _api_request(method, url, key, payload=None, timeout=2.5):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            error = detail.get("error", {})
            logging.warning(
                "OpenAI API HTTP %s: %s — %s",
                exc.code,
                str(error.get("code") or error.get("type") or "unknown")[:80],
                str(error.get("message") or "")[:300],
            )
        except (OSError, ValueError, AttributeError):
            logging.warning("OpenAI API HTTP %s", exc.code)
        if exc.code == 401:
            raise OpenAIError("Ключ OpenAI API не принят.") from exc
        if exc.code == 429:
            raise OpenAIError("Сейчас достигнут лимит OpenAI API.") from exc
        if exc.code == 404:
            raise OpenAIError("Предыдущий ответ больше недоступен.") from exc
        raise OpenAIError("Сервис ответов временно недоступен.") from exc
    except (OSError, ValueError) as exc:
        logging.warning("OpenAI API request failed: %s", type(exc).__name__)
        raise OpenAIError("Не удалось связаться с сервисом ответов.") from exc


def _needs_web_search(command):
    if re.search(r"\b(найди|поищи|ищи|загугли|погугли)\b", command):
        return True
    if re.search(r"\b(в интернете|в сети|по интернету)\b", command):
        return True
    if re.search(r"\b(новости|новостей|погода|погоду|прогноз погоды)\b", command):
        return True
    return command.startswith(("что нового", "что произошло сегодня", "какой сейчас курс"))


def _answer_text(data):
    parts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
    text = " ".join(parts)
    text = re.sub(r"【[^】]{1,160}】|\ue200cite\ue202.*?\ue201", "", text)
    text = re.sub(r"\[([^]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s*\((?:[\w-]+\.)+[\w-]{2,}(?:/[^)]*)?\)", "", text)
    text = " ".join(text.split()).strip()
    if len(text) > MAX_ANSWER_CHARS:
        cut = text[:MAX_ANSWER_CHARS]
        sentence_end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = cut[: sentence_end + 1] if sentence_end > 400 else cut.rstrip() + "…"
    return text or "Ответ пока не получился. Попробуйте спросить иначе."


def _source_buttons(data):
    buttons = []
    seen = set()
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            for annotation in part.get("annotations", []):
                if annotation.get("type") != "url_citation":
                    continue
                citation = annotation.get("url_citation") or annotation
                url = citation.get("url", "")
                title = " ".join(str(citation.get("title") or "Источник").split())
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or not parsed.netloc or len(url.encode("utf-8")) > 1024 or url in seen:
                    continue
                seen.add(url)
                buttons.append({"title": ("Источник: " + title)[:64], "url": url, "hide": True})
                if len(buttons) == 3:
                    return buttons
    return buttons


def _say(text, state=None, end_session=False):
    result = {
        "version": "1.0",
        "response": {"text": text[:1024], "end_session": end_session},
    }
    if state is not None and not end_session:
        result["session_state"] = state
    return result


def _finish(data):
    answer = _answer_text(data)
    result = _say(answer, {"previous_response_id": data["id"]})
    buttons = _source_buttons(data)
    if buttons:
        result["response"]["buttons"] = buttons
    return result


def handler_alice(event, context):
    """Обработчик события Яндекс Диалогов."""
    del context
    start = time.monotonic()
    if not isinstance(event, dict):
        return _say("Не удалось прочитать запрос. Повторите, пожалуйста.")

    req = event.get("request") or {}
    utterance = (req.get("command") or req.get("original_utterance") or "").strip()
    command = utterance.casefold().strip(" .,!?;:")
    state = (event.get("state") or {}).get("session") or {}
    if not isinstance(state, dict):
        state = {}
    previous_id = state.get("previous_response_id", "")
    if not isinstance(previous_id, str) or not previous_id.startswith("resp_"):
        previous_id = ""

    if command in ("хватит", "выход", "закончить", "стоп"):
        return _say("До встречи!", end_session=True)
    if command in ("сброс", "забудь разговор", "начать заново"):
        return _say("Начинаем новый разговор. Что хотите спросить?", {})
    if re.search(r"\b(поставь|установи|заведи|включи|создай)\b.*\b(будильник|таймер|напоминание)\b", command):
        return _say(
            "Системный будильник, таймер или напоминание я из навыка не устанавливаю. "
            "Скажите «выход», затем обратитесь с просьбой к Алисе.",
            state,
        )

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return _say("В навыке ещё не настроен ключ OpenAI API.")

    pending = state.get("pending_response_id", "")
    if pending:
        is_search = bool(state.get("pending_search"))
        waiting_text = (
            "Ещё ищу. Спросите «что нашлось?» через несколько секунд."
            if is_search else "Ещё думаю. Скажите «готово» через несколько секунд."
        )
        response_id = pending
        if not isinstance(response_id, str) or not response_id.startswith("resp_"):
            return _say("Не удалось продолжить ответ. Повторите вопрос.", {})
        url = API_URL + "/" + urllib.parse.quote(response_id, safe="")
        try:
            data = _api_request("GET", url, key, timeout=2.4)
        except OpenAIError as exc:
            if "больше недоступен" in str(exc):
                return _say(str(exc) + " Повторите вопрос.", {})
            return _say(str(exc) + " " + waiting_text, state)
        if data.get("status") == "completed":
            answer = _finish(data)
            if command not in ("", "готово", "ответ", "дальше", "ну что", *SEARCH_FOLLOWUP):
                answer["response"]["text"] += " Повторите ваш новый вопрос."
            return answer
        if data.get("status") in ("queued", "in_progress"):
            return _say(waiting_text, state)
        return _say("Получить ответ не удалось. Повторите вопрос.", {})

    if not utterance or command in ("готово", "ответ", "дальше", *SEARCH_FOLLOWUP):
        return _say("Здравствуйте! Задайте вопрос, и я постараюсь ответить.", state)

    question = utterance[:1000]
    search = _needs_web_search(command)
    payload = {
        "model": MODEL,
        "instructions": INSTRUCTIONS + (" " + SEARCH_PROMPT if search else ""),
        "input": [{"role": "user", "content": question}],
        "reasoning": {"effort": "none"},
        "max_output_tokens": 220 if search else 120,
        "background": search,
        "store": True,
    }
    if search:
        payload["tools"] = [{"type": "web_search", "search_context_size": "low"}]
        payload["tool_choice"] = "required"
    if previous_id:
        payload["previous_response_id"] = previous_id
    try:
        data = _api_request("POST", API_URL, key, payload=payload, timeout=2.4 if search else 3.6)
        if data.get("status") == "completed":
            return _finish(data)
        response_id = data.get("id", "")
        if data.get("status") not in ("queued", "in_progress") or not response_id:
            return _say("Получить ответ не удалось. Повторите вопрос.", state)

        # Пока есть время, проверяем готовность без выхода за лимит Алисы.
        url = API_URL + "/" + urllib.parse.quote(response_id, safe="")
        while (remaining := OPENAI_TIME_BUDGET_SECONDS - (time.monotonic() - start)) > 0.65:
            try:
                ready = _api_request("GET", url, key, timeout=min(remaining - 0.25, 0.8))
                if ready.get("status") == "completed":
                    return _finish(ready)
                if ready.get("status") not in ("queued", "in_progress"):
                    break
            except OpenAIError:
                break
            if (remaining := OPENAI_TIME_BUDGET_SECONDS - (time.monotonic() - start)) > 0.9:
                time.sleep(min(0.2, remaining - 0.7))
        return _say(
            "Ищу информацию. Спросите «что нашлось?» через несколько секунд."
            if search else "Думаю над ответом. Скажите «готово» через несколько секунд.",
            {"pending_response_id": response_id, "pending_search": search},
        )
    except OpenAIError as exc:
        if "больше недоступен" in str(exc):
            return _say("Контекст беседы устарел. Скажите «сброс» и повторите вопрос.", state)
        return _say(str(exc) + " Повторите вопрос позже.", state)


# HTTPS-вебхук Яндекс Диалогов для Vercel.
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
import hmac

SKILL_ID = os.environ.get("ALICE_SKILL_ID", "").strip()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        expected = os.environ.get("ALICE_WEBHOOK_TOKEN", "")
        supplied = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
        if not expected or not hmac.compare_digest(supplied, expected):
            return self._reply(404, {"error": "not found"})
        if not SKILL_ID:
            return self._reply(503, {"error": "ALICE_SKILL_ID is not configured"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16384:
                return self._reply(413, {"error": "invalid request size"})
            event = json.loads(self.rfile.read(length))
            if not isinstance(event, dict) or (event.get("session") or {}).get("skill_id") != SKILL_ID:
                return self._reply(403, {"error": "invalid skill"})
            return self._reply(200, handler_alice(event, None))
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, {"error": "invalid JSON"})

    def do_GET(self):
        self._reply(405, {"error": "method not allowed"})

    def _reply(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass  # Не записывать секрет вебхука из URL в журнал приложения.

