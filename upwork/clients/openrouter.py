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
        # Allow base URL override via env when needed.
        self.base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    def summarize(self, job: Dict[str, str]) -> str:
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        system_prompt = (
            "You are a job analysis assistant for freelancers.\n"
            "Summarize an Upwork job in Vietnamese to help decide whether it is worth applying.\n"
            "Reply BRIEFLY and ONLY in this exact format:\n\n"
            "- Summary: ...\n"
            "- Main requirements: ...\n"
            "- Budget/Rate: ...\n"
            "- Fit score (0-10): ...\n"
            "- Risks to note: ...\n\n"
        )

        job_type = job.get("job_type", "")
        experience_level = job.get("experience_level", "")
        budget = job.get("budget", "")

        extra_lines = []
        if job_type:
            extra_lines.append(f"- Job type: {job_type}")
        if experience_level:
            extra_lines.append(f"- Desired experience level: {experience_level}")
        if budget:
            extra_lines.append(f"- Displayed budget: {budget}")

        extra_text = ""
        if extra_lines:
            extra_text = "Additional job information:\n" + "\n".join(extra_lines) + "\n"

        user_content = (
            f"Title: {job.get('title', '')}\n"
            f"Description: {job.get('description', '')}\n"
            f"Link: {job.get('link', '')}\n"
            f"Posted date: {job.get('published', '')}\n"
            f"{extra_text}"
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Recommended by OpenRouter docs.
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
            # OpenRouter uses an OpenAI-compatible response format.
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            LOGGER.exception("Unexpected OpenRouter response: %s", data)
            return "Cannot summarize this job automatically (OpenRouter returned an invalid format)."

