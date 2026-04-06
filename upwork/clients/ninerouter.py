"""9Router API client — OpenAI-compatible local gateway (see 9router/README.md)."""
import logging
import os
from typing import Dict

import requests

LOGGER = logging.getLogger("upwork.clients.ninerouter")


class NineRouterClient:
    """
    Calls 9Router POST {base}/v1/chat/completions (OpenAI-compatible).

    Default base: http://localhost:20128 (set NINEROUTER_BASE_URL to override).
    """

    def __init__(self, api_key: str, model: str) -> None:
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("NineRouterClient requires a non-empty API key")
        model = (model or "").strip()
        if not model:
            raise ValueError("NineRouterClient requires a model name")

        self.api_key = api_key
        self.model = model
        self.base_url = (
            os.getenv("NINEROUTER_BASE_URL", "http://localhost:20128").strip().rstrip("/")
        )

    def _chat_completions_url(self) -> str:
        """
        Next.js App Router exposes POST at /api/v1/chat/completions.
        README/CLI often use base .../v1 — accept both. Rewrite /v1 -> /api/v1 can fail in some
        standalone Docker builds, so default host-only base uses /api/v1/chat/completions.
        """
        base = self.base_url.rstrip("/")
        if base.endswith("/api/v1"):
            return f"{base}/chat/completions"
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/api/v1/chat/completions"

    def summarize(self, job: Dict[str, str]) -> str:
        url = self._chat_completions_url()

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
        }

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
            "max_tokens": 350,
            "stream": False,
        }

        response = requests.post(url, headers=headers, json=body, timeout=120)
        response.raise_for_status()
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            LOGGER.exception("Unexpected 9Router response: %s", data)
            return "Cannot summarize this job automatically (9Router returned an unexpected format)."
