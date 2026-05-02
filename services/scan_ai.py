import base64
import json
import re
from typing import Any, Callable, Dict, List, Optional

import httpx
from fastapi import HTTPException


def extract_text_content_from_openai_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        extracted_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                extracted_parts.append(str(item["text"]))
        return "\n".join(extracted_parts)
    return ""


async def ocr_business_card_with_openai(
    image_bytes: bytes,
    content_type: str,
    *,
    api_key: str,
    api_base_url: str,
    model: str,
    normalize_text: Callable[[str], str],
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> str:
    if not api_key:
        raise HTTPException(status_code=503, detail="ORG_OPENAI_API_KEY is not configured")

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "Extract all People, Organizations, and Events visible in this image. "
        "Return JSON only as a list of dictionaries. Do not include markdown or commentary.\n\n"
        "Expected format:\n"
        "[\n"
        "  {\n"
        "    \"type\": \"person\",\n"
        "    \"name\": \"...\",\n"
        "    \"title\": \"...\",\n"
        "    \"company\": \"...\",\n"
        "    \"email\": \"...\",\n"
        "    \"phone\": \"...\",\n"
        "    \"website\": \"...\",\n"
        "    \"address\": \"...\"\n"
        "  },\n"
        "  {\n"
        "    \"type\": \"organization\",\n"
        "    \"name\": \"...\",\n"
        "    \"website\": \"...\",\n"
        "    \"description\": \"...\"\n"
        "  },\n"
        "  {\n"
        "    \"type\": \"event\",\n"
        "    \"title\": \"...\",\n"
        "    \"starts_at\": \"ISO-8601 datetime or null\",\n"
        "    \"location\": \"...\",\n"
        "    \"website\": \"...\",\n"
        "    \"description\": \"...\"\n"
        "  }\n"
        "]\n\n"
        "Rules:\n"
        "- Include every detected entity as a separate dictionary.\n"
        "- Use null for unknown fields.\n"
        "- Preserve exact emails/phones/URLs when present.\n"
        "- Output must be valid JSON list."
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{image_b64}"}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=30.0)
    if audit_hook:
        audit_hook(
            "scan.ai.ocr.requested",
            {
                "provider": "openai",
                "model": model,
                "image_content_type": content_type,
                "image_size_bytes": len(image_bytes),
            },
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{api_base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
    if not response.is_success:
        if audit_hook:
            audit_hook(
                "scan.ai.ocr.failed",
                {
                    "provider": "openai",
                    "model": model,
                    "status_code": response.status_code,
                },
            )
        detail = response.text.strip() or f"OCR request failed ({response.status_code})"
        raise HTTPException(status_code=502, detail=detail)

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        raise HTTPException(status_code=502, detail="OCR response missing choices")
    message = (choices[0] or {}).get("message") or {}
    extracted = extract_text_content_from_openai_message_content(message.get("content"))
    extracted = normalize_text(extracted)
    if not extracted:
        raise HTTPException(status_code=422, detail="Unable to extract text from business card image")
    if audit_hook:
        audit_hook(
            "scan.ai.ocr.completed",
            {
                "provider": "openai",
                "model": model,
                "status_code": response.status_code,
                "output_chars": len(extracted),
            },
        )
    return extracted


async def summarize_scan_targets_with_openai(
    *,
    ocr_text: str,
    created_targets: List[Dict[str, Optional[str]]],
    summary_enabled: bool,
    api_key: str,
    api_base_url: str,
    model: str,
    audit_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> List[Dict[str, Optional[str]]]:
    if not created_targets:
        return created_targets
    if not summary_enabled or not api_key:
        return created_targets

    summarized_targets: List[Dict[str, Optional[str]]] = [dict(item) for item in created_targets]
    summarized_targets_payload = [
        {
            "type": str(item.get("type") or "").strip() or "resource",
            "id": str(item.get("id") or "").strip() or None,
            "slug": str(item.get("slug") or "").strip() or None,
            "name": str(item.get("name") or "").strip() or None,
        }
        for item in summarized_targets
    ]
    prompt_payload = {
        "ocr_text": (ocr_text or "")[:12000],
        "targets": summarized_targets_payload,
    }
    prompt = (
        "You summarize records created from OCR. For each target in `targets`, generate a concise factual "
        "summary (max 140 chars) using only OCR content. Do not invent. "
        "Return strict JSON: {\"items\":[{\"index\":0,\"summary\":\"...\"}, ...]}"
    )
    request_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return valid JSON only."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": json.dumps(prompt_payload, ensure_ascii=True)},
                ],
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=30.0)
    if audit_hook:
        audit_hook(
            "scan.ai.summary.requested",
            {
                "provider": "openai",
                "model": model,
                "targets_count": len(summarized_targets),
            },
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{api_base_url}/chat/completions",
            json=request_payload,
            headers=headers,
        )
    if not response.is_success:
        if audit_hook:
            audit_hook(
                "scan.ai.summary.failed",
                {
                    "provider": "openai",
                    "model": model,
                    "status_code": response.status_code,
                },
            )
        return summarized_targets

    body = response.json()
    choices = body.get("choices") or []
    if not choices:
        return summarized_targets
    message = (choices[0] or {}).get("message") or {}
    content_text = extract_text_content_from_openai_message_content(message.get("content")).strip()
    if not content_text:
        return summarized_targets

    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(content_text)
    except Exception:
        if audit_hook:
            audit_hook(
                "scan.ai.summary.failed",
                {
                    "provider": "openai",
                    "model": model,
                    "reason": "invalid_json",
                },
            )
        return summarized_targets

    items = parsed.get("items")
    summaries_applied = 0
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except Exception:
                continue
            if index < 0 or index >= len(summarized_targets):
                continue
            summary = re.sub(r"\s+", " ", str(item.get("summary") or "").strip())
            if not summary:
                continue
            summarized_targets[index]["summary"] = summary[:140]
            summaries_applied += 1

    if audit_hook:
        audit_hook(
            "scan.ai.summary.completed",
            {
                "provider": "openai",
                "model": model,
                "targets_count": len(summarized_targets),
                "summaries_applied": summaries_applied,
            },
        )
    return summarized_targets
