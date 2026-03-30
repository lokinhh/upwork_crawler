"""Gemini API client for job summarization with API key rotation."""
import logging
from typing import Dict, List

import requests

LOGGER = logging.getLogger("upwork.clients.gemini")


class GeminiClient:
    """Calls Gemini API to summarize a job (title, description, link, published)."""

    def __init__(self, api_keys: List[str], model: str) -> None:
        if not api_keys:
            raise ValueError("GeminiClient requires at least one API key")

        # Loại bỏ key trống và trùng lặp, giữ nguyên thứ tự
        seen = set()
        cleaned: List[str] = []
        for k in api_keys:
            k = (k or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            cleaned.append(k)

        if not cleaned:
            raise ValueError("GeminiClient received only empty API keys")

        self.api_keys = cleaned
        self.model = model
        self._key_index = 0

    def _current_key(self) -> str:
        return self.api_keys[self._key_index]

    def _rotate_key(self) -> None:
        self._key_index = (self._key_index + 1) % len(self.api_keys)

    def summarize(self, job: Dict[str, str]) -> str:
        job_type = job.get("job_type", "")
        experience_level = job.get("experience_level", "")
        budget = job.get("budget", "")

        extra_lines = []
        if job_type:
            extra_lines.append(f"- Loại job: {job_type}")
        if experience_level:
            extra_lines.append(f"- Kinh nghiệm mong muốn: {experience_level}")
        if budget:
            extra_lines.append(f"- Ngân sách hiển thị: {budget}")

        extra_text = ""
        if extra_lines:
            extra_text = "Thông tin thêm về job:\n" + "\n".join(extra_lines) + "\n\n"

        prompt = (
            "Bạn là trợ lý tư vấn freelancer.\n"
            "Hãy tóm tắt nhanh một job trên Upwork bằng tiếng Việt, ngắn gọn để giúp quyết định có nên apply hay không.\n\n"
            "Trả về đúng định dạng sau:\n"
            "- Tóm tắt: ...\n"
            "- Yêu cầu chính: ...\n"
            "- Ngân sách/Rate: ...\n"
            "- Độ phù hợp (0-10): ...\n"
            "- Rủi ro cần lưu ý: ...\n\n"
            f"Tiêu đề: {job.get('title', '')}\n"
            f"Mô tả: {job.get('description', '')}\n"
            f"Link: {job.get('link', '')}\n"
            f"Ngày đăng: {job.get('published', '')}\n"
            f"{extra_text}"
        )

        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 350},
        }

        max_attempts = 5
        last_error: str | None = None

        for attempt in range(max_attempts):
            api_key = self._current_key()
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent?key={api_key}"
            )

            try:
                response = requests.post(url, json=body, timeout=30)
            except requests.RequestException as exc:
                last_error = f"request error: {exc}"
                LOGGER.warning(
                    "Gemini request failed with current key (attempt %s/%s): %s",
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                self._rotate_key()
                continue

            if response.status_code == 429:
                last_error = "429 Too Many Requests"
                LOGGER.warning(
                    "Gemini 429 (rate limit) with current key (attempt %s/%s), rotating key",
                    attempt + 1,
                    max_attempts,
                )
                self._rotate_key()
                continue

            if not response.ok:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    last_error = f"HTTP {response.status_code}: {exc}"
                    LOGGER.error(
                        "Gemini HTTP error (attempt %s/%s): %s",
                        attempt + 1,
                        max_attempts,
                        exc,
                    )
                    break

            data = response.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception:
                last_error = "unexpected response format"
                LOGGER.exception("Unexpected Gemini response: %s", data)
                self._rotate_key()

        LOGGER.error("Gemini summarize failed after %s attempts: %s", max_attempts, last_error)
        return "Khong the tom tat tu dong cho job nay (loi Gemini hoac het quota API key)."
