"""OpenRouter API client for job summarization (fallback when Gemini fails)."""
import logging
from typing import Dict

import os
import requests

LOGGER = logging.getLogger("upwork.clients.openrouter")


class OpenRouterClient:
    """Calls OpenRouter (OpenAI-compatible) API to summarize a job."""

    def __init__(self, api_key: str, model: str) -> None:
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("OpenRouterClient requires a non-empty API key")
        model = (model or "").strip()
        if not model:
            raise ValueError("OpenRouterClient requires a model name")

        self.api_key = api_key
        self.model = model
        # Cho phép override base URL qua env nếu cần
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    def summarize(self, job: Dict[str, str]) -> str:
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        system_prompt = (
            "Bạn là trợ lý phân tích job cho freelancer.\n"
            "Hãy tóm tắt nhanh một job trên Upwork bằng tiếng Việt để giúp quyết định có nên apply hay không.\n"
            "Trả lời NGẮN GỌN và CHỈ theo đúng định dạng sau:\n\n"
            "- Tóm tắt: ...\n"
            "- Yêu cầu chính: ...\n"
            "- Ngân sách/Rate: ...\n"
            "- Độ phù hợp (0-10): ...\n"
            "- Rủi ro cần lưu ý: ...\n\n"
        )

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
            extra_text = "Thông tin thêm về job:\n" + "\n".join(extra_lines) + "\n"

        user_content = (
            f"Tiêu đề: {job.get('title', '')}\n"
            f"Mô tả: {job.get('description', '')}\n"
            f"Link: {job.get('link', '')}\n"
            f"Ngày đăng: {job.get('published', '')}\n"
            f"{extra_text}"
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Khuyen nghi theo docs OpenRouter
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://github.com"),
            "X-Title": os.getenv("OPENROUTER_APP_NAME", "upwork-scanner"),
        }

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": 350,
        }

        response = requests.post(url, headers=headers, json=body, timeout=30)
        response.raise_for_status()
        data = response.json()
        try:
            # OpenRouter dùng format OpenAI-compatible
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            LOGGER.exception("Unexpected OpenRouter response: %s", data)
            return "Không thể tóm tắt tự động cho job này (OpenRouter trả về định dạng là)."

