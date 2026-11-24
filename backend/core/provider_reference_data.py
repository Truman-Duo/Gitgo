"""Reviewed, endpoint-scoped reference data; never deployment verification.

Exact model IDs only. Additions require an authoritative source and review
date. Runtime does not scrape documentation or send credentials to catalogues.
"""

VERSION = "2026-11-07.1"
PROVIDERS = {
    "openai-official": {
        "api": "https://api.openai.com/v1",
        "source": "https://developers.openai.com/api/docs/models/gpt-6.1-terra",
        "version": VERSION,
        "reviewed_at": "2026-11-07",
        "models": {
            "gpt-6.1-terra": {
                "limit": {"context": 1_050_000, "output": 128_000},
                "reasoning": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "reasoning_options": [{"type": "effort", "values": ["none", "low", "medium", "high", "xhigh", "max"], "default": "medium"}],
                "tool_protocols": ["openai_responses"],
            },
            "gpt-6.1-sol": {
                "source": "https://developers.openai.com/api/docs/models/gpt-6.1-sol",
                "version": "2026-11-18.1",
                "reviewed_at": "2026-11-18",
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["low", "medium", "high", "xhigh", "max"], "default": "medium"},
                ],
            },
            "gpt-6-sol": {
                "source": "https://developers.openai.com/api/docs/models/gpt-6-sol",
                "version": "2026-11-18.1",
                "reviewed_at": "2026-11-18",
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["none", "low", "medium", "high", "xhigh", "max"], "default": "medium"},
                ],
            },
            "gpt-6-luna": {
                "source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
                "version": "2026-11-18.1",
                "reviewed_at": "2026-11-18",
                "reasoning": True,
                "reasoning_options": [
                    {"type": "effort", "values": ["none", "low", "medium", "high", "xhigh", "max"], "default": "medium"},
                ],
            },
        },
    },
}
