#!/usr/bin/env python3
"""
Extract SKU-like identifiers from:
1) HAR/JSON captures, or
2) Live browser session capture (manual login) using Playwright, or
3) Direct API response from a curl command exported from browser/app.

Examples:
  python main.py --input cart.har --cart-only --output skuids.txt
  python main.py --live-url https://www.thewellnesscorner.com/ --cart-only
  python main.py --curl-file curl_cmd.txt --output skuids.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

SKU_KEY_PATTERNS = (
    r"^sku$",
    r"sku[_-]?id",
    r"product[_-]?sku",
    r"item[_-]?sku",
    r"variant[_-]?sku",
    r"product[_-]?id",
    r"item[_-]?id",
)

CART_URL_HINTS = (
    "cart",
    "basket",
    "checkout",
    "wellness",
    "pharmacy",
    "store",
)

ITEM_SKU_KEYS = ("skuid", "sku_id", "sku", "productsku", "itemsku")
ITEM_QTY_KEYS = ("quantity", "qty", "count")
ITEM_NAME_KEYS = ("name", "productname", "title", "itemname")
ITEM_PRICE_KEYS = ("price", "mrp", "sellingprice", "saleprice", "amount")
JWT_RX = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")

API_BASE = "https://api.thewellnesscorner.com"
PROBE_CART_ENDPOINTS = (
    "/store/tata-1mg/cart",
    "/store/pharmacy/cart",
    "/store/cart",
    "/pharmacy/cart",
    "/wellness/cart",
    "/store/tata-1mg/user/cart",
)

CART_KIND_LABELS = {
    "pharmacy": "Pharmacy Cart",
    "wellness_store": "Wellness Store Cart",
}


def build_key_regex() -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in SKU_KEY_PATTERNS), re.IGNORECASE)


def looks_like_sku(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return value > 0
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    if re.fullmatch(r"[A-Za-z0-9._:-]{3,64}", s):
        return True
    return bool(re.fullmatch(r"[0-9a-fA-F-]{8,64}", s))


def safe_json_loads(text: str) -> Any:
    text = text.strip().lstrip("\ufeff")
    if not text:
        return None
    # Strip common anti-XSSI prefix: )]}',
    if text.startswith(")]}',"):
        parts = text.split("\n", 1)
        text = parts[1].strip() if len(parts) > 1 else ""
        if not text:
            return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fallback: parse first JSON-looking segment in mixed text.
        start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
        if start == -1:
            return None
        candidate = text[start:].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None


def walk_collect(data: Any, key_rx: re.Pattern[str], found: set[str]) -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k)
            if key_rx.search(key) and looks_like_sku(v):
                found.add(str(v).strip())
            walk_collect(v, key_rx, found)
    elif isinstance(data, list):
        for item in data:
            walk_collect(item, key_rx, found)


def iter_har_entries(payload: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    log = payload.get("log", {})
    entries = log.get("entries", []) if isinstance(log, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        req = entry.get("request", {})
        res = entry.get("response", {})
        if not isinstance(req, dict) or not isinstance(res, dict):
            continue
        url = str(req.get("url", ""))
        content = res.get("content", {})
        if not isinstance(content, dict):
            continue
        text = content.get("text", "")
        parsed = safe_json_loads(text) if isinstance(text, str) else None
        if parsed is not None:
            yield url, parsed


def iter_capture_pairs_json(payload: Any) -> Iterable[tuple[str, Any]]:
    if not isinstance(payload, list):
        return
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if "url" not in entry or "body" not in entry:
            continue
        yield str(entry.get("url", "")), entry.get("body")


def _unwrap_body(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    if "body" in node and any(k in node for k in ("url", "status", "method", "requestPostData")):
        return node.get("body")
    return node


def classify_cart_kind(url: str) -> str | None:
    u = url.lower()
    if "cart" not in u and "basket" not in u and "checkout" not in u:
        return None
    if "pharmacy" in u:
        return "pharmacy"
    if "tata-1mg" in u or "/store/cart" in u or "wellness" in u:
        return "wellness_store"
    return None


def _coerce_quantity(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d+", s):
            return int(s)
        if re.fullmatch(r"\d+\.\d+", s):
            return float(s)
    return value


def _is_probable_qty(value: Any) -> bool:
    v = _coerce_quantity(value)
    return isinstance(v, (int, float)) and v >= 0


def _pick_sku_id(node: dict[str, Any], kind: str) -> Any:
    normalized = {normalize_key(str(k)): v for k, v in node.items()}
    if kind == "pharmacy":
        # Pharmacy cart uses id/skuId as the line-item identifier.
        for key in ("skuid", "sku", "id", "itemid", "productid"):
            if key in normalized and looks_like_sku(normalized[key]):
                return normalized[key]
    else:
        for key in ("skuid", "sku", "itemid", "productid", "id"):
            if key in normalized and looks_like_sku(normalized[key]):
                return normalized[key]
    return None


def _extract_cart_items_from_node(node: Any, kind: str, out: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if kind == "wellness_store":
            # Wellness store cart often returns { "<skuId>": "<qty>" }.
            if node and all(looks_like_sku(k) and _is_probable_qty(v) for k, v in node.items()):
                for sku, qty in node.items():
                    out.append(
                        {
                            "skuId": str(sku).strip(),
                            "quantity": _coerce_quantity(qty),
                            "name": None,
                            "price": None,
                        }
                    )
                return
        sku = _pick_sku_id(node, kind)
        qty = _get_first_value_by_keys(node, ITEM_QTY_KEYS)
        name = _get_first_value_by_keys(node, ITEM_NAME_KEYS)
        price = _get_first_value_by_keys(node, ITEM_PRICE_KEYS)
        if looks_like_sku(sku) and (_is_probable_qty(qty) or name is not None):
            out.append(
                {
                    "skuId": str(sku).strip(),
                    "quantity": _coerce_quantity(qty),
                    "name": name,
                    "price": price,
                }
            )
        for v in node.values():
            _extract_cart_items_from_node(v, kind, out)
        return
    if isinstance(node, list):
        for it in node:
            _extract_cart_items_from_node(it, kind, out)


def extract_cart_items(payload: Any, kind: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    _extract_cart_items_from_node(_unwrap_body(payload), kind, items)
    dedup: dict[tuple[str, Any, Any, Any], dict[str, Any]] = {}
    for it in items:
        key = (str(it.get("skuId")), it.get("quantity"), it.get("name"), it.get("price"))
        dedup[key] = it
    return list(dedup.values())


def extract_carts_from_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        "pharmacy": [],
        "wellness_store": [],
    }
    for url, body in pairs:
        kind = classify_cart_kind(url)
        if not kind:
            continue
        items = extract_cart_items(body, kind=kind)
        if items:
            # Keep the most recent cart snapshot for each cart type.
            grouped[kind] = items
    for kind, items in grouped.items():
        dedup: dict[str, dict[str, Any]] = {}
        for it in items:
            key = str(it.get("skuId", "")).strip()
            if not key:
                continue
            dedup[key] = it
        grouped[kind] = list(dedup.values())
    return grouped


def _maybe_title_from_slug(value: str, sku: str) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.search(r"/(?:drugs|otc)/([a-z0-9-]+)-" + re.escape(str(sku)) + r"(?:\b|/|$)", value.lower())
    if not m:
        return None
    slug = m.group(1).strip("-")
    if not slug:
        return None
    return slug.replace("-", " ").title()


def _best_name_from_payload(payload: Any, sku: str | None = None) -> str | None:
    best: tuple[int, str] | None = None

    def consider(value: Any, score: int) -> None:
        nonlocal best
        if not isinstance(value, str):
            return
        s = value.strip()
        if not s:
            return
        candidate = s
        if sku:
            from_slug = _maybe_title_from_slug(s, sku)
            if from_slug:
                candidate = from_slug
                score += 60
        if best is None or score > best[0]:
            best = (score, candidate)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                nk = normalize_key(str(k))
                if nk in ("name", "title", "productname", "itemname"):
                    consider(v, 80)
                elif nk in ("headerplaceholdervalue", "displayname", "producttitle"):
                    consider(v, 120)
                elif nk in ("slug",):
                    consider(v, 70)
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(payload)
    return None if best is None else best[1]


def build_name_index_from_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for _, body in pairs:
        items: list[dict[str, Any]] = []
        _extract_cart_items_from_node(_unwrap_body(body), "pharmacy", items)
        _extract_cart_items_from_node(_unwrap_body(body), "wellness_store", items)
        for it in items:
            sku = str(it.get("skuId", "")).strip()
            name = it.get("name")
            if sku and isinstance(name, str) and name.strip():
                index[sku] = name.strip()
    return index


def apply_name_index(carts: dict[str, list[dict[str, Any]]], name_index: dict[str, str]) -> None:
    for items in carts.values():
        for it in items:
            if it.get("name"):
                continue
            sku = str(it.get("skuId", "")).strip()
            if sku and sku in name_index:
                it["name"] = name_index[sku]


def infer_city_from_pairs(pairs: Iterable[tuple[str, Any]]) -> str:
    for url, _ in pairs:
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        city = q.get("city", [None])[0]
        if isinstance(city, str) and city.strip():
            return city.strip()
    return "Bengaluru"


def extract_from_pairs(pairs: Iterable[tuple[str, Any]], cart_only: bool) -> list[str]:
    key_rx = build_key_regex()
    found: set[str] = set()
    for url, body in pairs:
        if cart_only and not any(h in url.lower() for h in CART_URL_HINTS):
            continue
        walk_collect(body, key_rx, found)
        if isinstance(body, dict):
            post_data = body.get("requestPostData")
            if isinstance(post_data, str):
                parsed_post = safe_json_loads(post_data)
                if parsed_post is not None:
                    walk_collect(parsed_post, key_rx, found)
    return sorted(found)


def extract_from_har(payload: dict[str, Any], cart_only: bool) -> list[str]:
    return extract_from_pairs(iter_har_entries(payload), cart_only=cart_only)


def extract_from_json(payload: Any) -> list[str]:
    key_rx = build_key_regex()
    found: set[str] = set()
    walk_collect(payload, key_rx, found)
    return sorted(found)


def normalize_cmd_curl_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\^\s*\n", " ", text)
    text = re.sub(r"\^([\"^&|<>])", r"\1", text)
    return text.strip()


def parse_curl_command(text: str) -> tuple[str, str, dict[str, str], str | None]:
    normalized = normalize_cmd_curl_text(text)
    parts = shlex.split(normalized, posix=True)
    if not parts or parts[0].lower() != "curl":
        raise ValueError("Command must start with curl")

    method = None
    url = None
    headers: dict[str, str] = {}
    data = None

    i = 1
    while i < len(parts):
        token = parts[i]
        nxt = parts[i + 1] if i + 1 < len(parts) else None

        if token in ("-X", "--request") and nxt is not None:
            method = nxt.upper()
            i += 2
            continue
        if token in ("-H", "--header") and nxt is not None:
            hv = nxt
            if ":" in hv:
                hk, hvv = hv.split(":", 1)
                headers[hk.strip()] = hvv.strip()
            i += 2
            continue
        if token in ("--data", "--data-raw", "--data-binary") and nxt is not None:
            data = nxt
            i += 2
            continue
        if token.startswith("http://") or token.startswith("https://"):
            url = token
            i += 1
            continue
        i += 1

    if not url:
        raise ValueError("Could not find URL in curl command")
    if method is None:
        method = "POST" if data is not None else "GET"
    return method, url, headers, data


def run_curl_request(curl_text: str) -> tuple[str, Any]:
    method, url, headers, data = parse_curl_command(curl_text)
    body = data.encode("utf-8") if data is not None else None
    req = urllib.request.Request(url=url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"HTTP {exc.code} from API: {error_body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network error: {exc}") from exc

    parsed = safe_json_loads(raw)
    if parsed is None:
        raise SystemExit(f"API did not return JSON. First 500 chars:\n{raw[:500]}")
    return url, parsed


def http_json_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    data: Any = None,
) -> tuple[int, Any | None, str]:
    if data is None:
        body = None
    elif isinstance(data, str):
        body = data.encode("utf-8")
    else:
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url=url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            parsed = safe_json_loads(raw)
            return int(resp.status), parsed, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        parsed = safe_json_loads(raw)
        return int(exc.code), parsed, raw
    except urllib.error.URLError as exc:
        return 0, None, str(exc)


def _looks_like_token(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    if JWT_RX.fullmatch(v):
        return True
    return len(v) >= 24 and re.fullmatch(r"[A-Za-z0-9._~-]+", v) is not None


def _mask_token(token: str) -> str:
    if len(token) <= 10:
        return token
    return token[:6] + "..." + token[-4:]


def extract_tokens_from_storage_state(state: dict[str, Any]) -> list[str]:
    tokens: set[str] = set()

    for c in state.get("cookies", []) if isinstance(state, dict) else []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).lower()
        val = str(c.get("value", ""))
        if ("token" in name or "auth" in name or "jwt" in name) and _looks_like_token(val):
            tokens.add(val)

    origins = state.get("origins", []) if isinstance(state, dict) else []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        for ls in origin.get("localStorage", []):
            if not isinstance(ls, dict):
                continue
            key = str(ls.get("name", "")).lower()
            val = str(ls.get("value", ""))
            if ("token" in key or "auth" in key or "jwt" in key) and _looks_like_token(val):
                tokens.add(val)
            # Some apps store JSON containing token fields.
            maybe_obj = safe_json_loads(val)
            if isinstance(maybe_obj, dict):
                for mk, mv in maybe_obj.items():
                    if (
                        isinstance(mv, str)
                        and ("token" in str(mk).lower() or "auth" in str(mk).lower())
                        and _looks_like_token(mv)
                    ):
                        tokens.add(mv)

    return sorted(tokens, key=len, reverse=True)


def extract_tokens_from_request_headers(headers: dict[str, Any]) -> list[str]:
    tokens: set[str] = set()
    for k, v in headers.items():
        key = str(k).lower()
        val = str(v).strip()
        if not val:
            continue
        if key == "x-access-token" and _looks_like_token(val):
            tokens.add(val)
        if key == "authorization":
            # Handles: "Bearer <token>" and raw token values.
            parts = val.split()
            if len(parts) >= 2 and parts[0].lower() == "bearer":
                cand = parts[1].strip()
                if _looks_like_token(cand):
                    tokens.add(cand)
            elif _looks_like_token(val):
                tokens.add(val)
        if key == "cookie":
            # Very loose fallback: look for token/auth-like cookie values.
            for chunk in val.split(";"):
                if "=" not in chunk:
                    continue
                ck, cv = chunk.split("=", 1)
                ckl = ck.strip().lower()
                cv = cv.strip()
                if ("token" in ckl or "auth" in ckl or "jwt" in ckl) and _looks_like_token(cv):
                    tokens.add(cv)
    return sorted(tokens, key=len, reverse=True)


def extract_tokens_from_captured_pairs(pairs: Iterable[tuple[str, Any]]) -> list[str]:
    tokens: set[str] = set()
    for _, body in pairs:
        if not isinstance(body, dict):
            continue
        hdrs = body.get("requestHeaders")
        if isinstance(hdrs, dict):
            for t in extract_tokens_from_request_headers(hdrs):
                tokens.add(t)
    return sorted(tokens, key=len, reverse=True)


def normalize_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _get_first_value_by_keys(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k, v in obj.items():
        if normalize_key(str(k)) in keys:
            return v
    return None


def print_items(label: str, items: list[dict[str, Any]]) -> None:
    if not items:
        print(f"\n{label}: no items found.")
        return
    print(f"\n{label}:")
    for it in items:
        print(
            f"- skuId={it.get('skuId')} quantity={it.get('quantity')} "
            f"name={it.get('name')} price={it.get('price')}"
        )


def print_grouped_carts(carts: dict[str, list[dict[str, Any]]]) -> None:
    for kind in ("pharmacy", "wellness_store"):
        label = CART_KIND_LABELS.get(kind, kind)
        print_items(label, carts.get(kind, []))


def collect_grouped_skus(carts: dict[str, list[dict[str, Any]]]) -> list[str]:
    skus: set[str] = set()
    for items in carts.values():
        for it in items:
            sku = str(it.get("skuId", "")).strip()
            if sku:
                skus.add(sku)
    return sorted(skus)


def capture_live_json(
    live_url: str,
    profile_dir: str | None = None,
    attach_cdp_url: str | None = None,
) -> tuple[list[tuple[str, Any]], list[str]]:
    # Playwright's Node driver can print noisy deprecation warnings to stderr.
    os.environ.setdefault("NODE_NO_WARNINGS", "1")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(
            "Playwright is required for --live-url.\n"
            "Install with:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    captured: list[tuple[str, Any]] = []
    tokens: list[str] = []
    seen_entries: set[tuple[str, str, str, str]] = set()

    with sync_playwright() as p:
        context = None
        browser = None
        page = None
        attached_mode = bool(attach_cdp_url)
        contexts_to_watch: list[Any] = []
        header_tokens: set[str] = set()
        seen_requests = 0
        seen_responses = 0

        if attach_cdp_url:
            try:
                browser = p.chromium.connect_over_cdp(attach_cdp_url)
            except Exception as exc:
                raise SystemExit(
                    "Failed to attach to Chrome CDP.\n"
                    "Start Chrome first with remote debugging enabled, for example:\n"
                    '  chrome.exe --remote-debugging-port=9222\n'
                    "Then run with:\n"
                    "  --attach-cdp-url http://127.0.0.1:9222"
                ) from exc
            contexts = list(browser.contexts)
            if not contexts:
                raise SystemExit(
                    "Connected to CDP but no browser context found. "
                    "Open at least one tab in that Chrome window and retry."
                )
            contexts_to_watch = contexts
            # Avoid enumerating pages in CDP attach mode (can trigger frame-detach races).
            context = contexts[0]
            print(
                f"Attached to existing Chrome via CDP: {attach_cdp_url} "
                f"(contexts={len(contexts_to_watch)})"
            )
        else:
            # Persistent context improves compatibility with federated logins (Google).
            user_data_dir = str(Path(profile_dir or ".playwright-wc-profile").resolve())
            launch_kwargs: dict[str, Any] = {
                "headless": False,
                "no_viewport": True,
                "ignore_default_args": ["--enable-automation"],
                "args": [
                    "--start-maximized",
                    "--window-size=1600,1000",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-popup-blocking",
                ],
            }
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel="chrome",
                    **launch_kwargs,
                )
                print("Using system Chrome with persistent profile for login.")
            except Exception:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    **launch_kwargs,
                )
                print("System Chrome channel unavailable, using Chromium persistent profile.")
            contexts_to_watch = [context]

        def _response_json(response: Any) -> Any:
            # Some CDP-attached pages intermittently fail on response.text().
            try:
                text = response.text()
                parsed = safe_json_loads(text)
                if parsed is not None:
                    return parsed
            except Exception:
                pass
            try:
                raw = response.body()
                text = raw.decode("utf-8", errors="ignore")
                return safe_json_loads(text)
            except Exception:
                return None

        def on_response(response: Any) -> None:
            nonlocal seen_responses
            try:
                seen_responses += 1
                req = response.request
                rtype = (req.resource_type or "").lower()
                # In CDP attach mode, resource types can be inconsistent.
                # Capture any response that can be parsed as JSON.
                try:
                    req_headers = req.headers
                except Exception:
                    req_headers = {}
                for tok in extract_tokens_from_request_headers(req_headers):
                    header_tokens.add(tok)
                parsed = _response_json(response)
                if parsed is not None:
                    dedup_key = (
                        response.url,
                        req.method or "",
                        str(response.status),
                        str(req.post_data or ""),
                    )
                    if dedup_key in seen_entries:
                        return
                    seen_entries.add(dedup_key)
                    entry = {
                        "url": response.url,
                        "status": response.status,
                        "method": req.method,
                        "resourceType": rtype,
                        "requestHeaders": req_headers,
                        "requestPostData": req.post_data,
                        "body": parsed,
                    }
                    captured.append((response.url, entry))
            except Exception:
                return

        def on_request(request: Any) -> None:
            nonlocal seen_requests
            try:
                seen_requests += 1
                for tok in extract_tokens_from_request_headers(request.headers):
                    header_tokens.add(tok)
            except Exception:
                return

        def on_request_finished(request: Any) -> None:
            # Fallback path: some CDP sessions are more reliable on requestfinished.
            try:
                response = request.response()
                if response is None:
                    return
                try:
                    req_headers = request.headers
                except Exception:
                    req_headers = {}
                for tok in extract_tokens_from_request_headers(req_headers):
                    header_tokens.add(tok)
                parsed = _response_json(response)
                if parsed is None:
                    return
                dedup_key = (
                    response.url,
                    request.method or "",
                    str(response.status),
                    str(request.post_data or ""),
                )
                if dedup_key in seen_entries:
                    return
                seen_entries.add(dedup_key)
                entry = {
                    "url": response.url,
                    "status": response.status,
                    "method": request.method,
                    "resourceType": (request.resource_type or "").lower(),
                    "requestHeaders": req_headers,
                    "requestPostData": request.post_data,
                    "body": parsed,
                }
                captured.append((response.url, entry))
            except Exception:
                return

        assert context is not None
        if attached_mode:
            print("\nAttach mode active.")
            print("Opening dedicated capture tab in attached Chrome session...")
            page = context.new_page()
            page.bring_to_front()
            try:
                page.goto(live_url, wait_until="domcontentloaded", timeout=120000)
            except Exception:
                print(f"Could not auto-open {live_url}. Open it manually in this attached Chrome.")
            # In CDP attach mode, page-level listeners are more stable than context-level listeners.
            page.on("response", on_response)
            page.on("request", on_request)
            page.on("requestfinished", on_request_finished)
        else:
            for ctx in contexts_to_watch:
                # Context-level listeners observe traffic across pages/popups in launched mode.
                ctx.on("response", on_response)
                ctx.on("request", on_request)
                ctx.on("requestfinished", on_request_finished)
            # Use a dedicated tab in launched mode to avoid reusing broken tab state.
            page = context.new_page()
            page.bring_to_front()
            page.goto(live_url, wait_until="domcontentloaded", timeout=120000)

        print("\nBrowser opened.")
        print("1) Log in with your own account")
        print("2) Use the tab opened by this script, then open Pharmacy/Wellness cart pages")
        print("3) Click + or - once in cart to force a cart API call")
        print("   Tip: If Google login opens a popup, complete it there and return.")
        print("4) Press Enter here when done capturing\n")
        input()
        # Give pending network events a moment to flush to listeners.
        time.sleep(2.0)
        token_set: set[str] = set()
        for ctx in contexts_to_watch:
            try:
                storage_state = ctx.storage_state()
                for t in extract_tokens_from_storage_state(storage_state):
                    token_set.add(t)
            except Exception:
                # CDP attach can fail on storage_state for some contexts.
                continue
        for t in extract_tokens_from_captured_pairs(captured):
            token_set.add(t)
        for t in header_tokens:
            token_set.add(t)
        tokens = sorted(token_set, key=len, reverse=True)
        print(f"Observed browser events: requests={seen_requests}, responses={seen_responses}")
        if attached_mode:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
        else:
            context.close()

    return captured, tokens


def probe_cart_with_tokens(tokens: list[str], referer_url: str) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    if not tokens:
        return pairs

    common_headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.thewellnesscorner.com",
        "referer": referer_url,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
    }

    for token in tokens[:3]:
        print(f"Trying token {_mask_token(token)} against cart endpoints...")
        success_kinds: set[str] = set()
        seen: set[tuple[str, str]] = set()
        for path in PROBE_CART_ENDPOINTS:
            url = API_BASE + path
            for method, data in (("GET", None), ("POST", {})):
                key = (url, method)
                if key in seen:
                    continue
                seen.add(key)
                headers = dict(common_headers)
                headers["x-access-token"] = token
                status, parsed, raw = http_json_request(method, url, headers=headers, data=data)
                if parsed is None:
                    continue
                entry = {
                    "url": url,
                    "status": status,
                    "method": method,
                    "requestPostData": None if data is None else json.dumps(data),
                    "body": parsed,
                }
                pairs.append((url, entry))
                if status in (200, 201):
                    kind = classify_cart_kind(url)
                    if kind:
                        success_kinds.add(kind)
                    # We only need one successful payload for both carts.
                    if "pharmacy" in success_kinds and "wellness_store" in success_kinds:
                        return pairs
    return pairs


def _token_headers(token: str, referer_url: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.thewellnesscorner.com",
        "referer": referer_url,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        "x-access-token": token,
    }


def probe_store_name_with_tokens(
    sku: str,
    tokens: list[str],
    referer_url: str,
    city: str,
) -> str | None:
    for token in tokens[:3]:
        headers = _token_headers(token, referer_url)
        endpoints = (
            f"/store/1mg-skus/drug/{sku}/static?city={city}",
            f"/store/1mg-skus/drug/{sku}/dynamic?city={city}",
            f"/store/1mg-skus/otc/{sku}/otc-details?city={city}",
            f"/store/1mg-skus/otc/{sku}/dynamic?city={city}",
            f"/store/1mg-skus/otc/{sku}/static?city={city}",
        )
        for path in endpoints:
            url = API_BASE + path
            status, parsed, raw = http_json_request("GET", url, headers=headers, data=None)
            if status not in (200, 201) or parsed is None:
                continue
            guessed = _best_name_from_payload(parsed, sku=sku)
            if guessed:
                return guessed
    return None


def enrich_wellness_names_with_probe(
    carts: dict[str, list[dict[str, Any]]],
    tokens: list[str],
    referer_url: str,
    city: str,
) -> None:
    items = carts.get("wellness_store", [])
    if not items or not tokens:
        return
    for it in items:
        if it.get("name"):
            continue
        sku = str(it.get("skuId", "")).strip()
        if not sku:
            continue
        resolved = probe_store_name_with_tokens(sku, tokens=tokens, referer_url=referer_url, city=city)
        if resolved:
            it["name"] = resolved


def print_and_save(skus: list[str], output: str | None) -> int:
    if not skus:
        print("No SKU-like identifiers found.")
        return 1
    print("Found SKU IDs:")
    for sku in skus:
        print(sku)
    if output:
        Path(output).write_text("\n".join(skus) + "\n", encoding="utf-8")
        print(f"\nSaved {len(skus)} SKU IDs to: {output}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract SKU IDs from HAR/JSON or live browser capture")
    ap.add_argument("--input", help="Path to .har or .json file")
    ap.add_argument("--live-url", help="Open URL for live capture (manual login in browser)")
    ap.add_argument(
        "--profile-dir",
        help="Persistent browser profile directory for live mode (helps Google login)",
    )
    ap.add_argument(
        "--attach-cdp-url",
        help=(
            "Attach to an already running Chrome via CDP "
            "(example: http://127.0.0.1:9222)"
        ),
    )
    ap.add_argument("--curl-file", help="Path to text file containing a curl command")
    ap.add_argument("--curl-command", help="Raw curl command string (quote it)")
    ap.add_argument("--output", help="Optional output text file path")
    ap.add_argument("--save-response", help="Save API JSON response (curl mode)")
    ap.add_argument(
        "--save-captured-json",
        help="Optional path to save all captured live JSON responses",
    )
    ap.add_argument(
        "--cart-only",
        action="store_true",
        help="Only scan cart/wellness/pharmacy/store URLs (HAR/live mode)",
    )
    args = ap.parse_args()

    modes = [bool(args.input), bool(args.live_url), bool(args.curl_file or args.curl_command)]
    if sum(modes) != 1:
        raise SystemExit("Provide exactly one mode: --input OR --live-url OR --curl-file/--curl-command")

    if args.input:
        path = Path(args.input)
        if not path.exists():
            raise SystemExit(f"Input file not found: {path}")
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        pair_list: list[tuple[str, Any]] = []
        if path.suffix.lower() == ".har" and isinstance(payload, dict) and "log" in payload:
            pair_list = list(iter_har_entries(payload))
        elif isinstance(payload, list):
            pair_list = list(iter_capture_pairs_json(payload))

        if pair_list:
            carts = extract_carts_from_pairs(pair_list)
            name_index = build_name_index_from_pairs(pair_list)
            apply_name_index(carts, name_index)
            if any(carts.values()):
                print_grouped_carts(carts)
                skus = collect_grouped_skus(carts)
                return print_and_save(skus, args.output)
            skus = extract_from_pairs(pair_list, cart_only=args.cart_only)
        else:
            skus = extract_from_json(payload)
        return print_and_save(skus, args.output)

    if args.curl_file or args.curl_command:
        curl_text = args.curl_command
        if args.curl_file:
            curl_text = Path(args.curl_file).read_text(encoding="utf-8", errors="ignore")
        assert curl_text is not None
        url, payload = run_curl_request(curl_text)
        if args.save_response:
            Path(args.save_response).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Saved API response to: {args.save_response}")
        print(f"API URL: {url}")
        kind = classify_cart_kind(url)
        carts = {"pharmacy": [], "wellness_store": []}
        if kind:
            carts[kind] = extract_cart_items(payload, kind=kind)
            print_grouped_carts(carts)
            skus = collect_grouped_skus(carts)
        else:
            skus = []
        if not skus:
            skus = extract_from_json(payload)
        return print_and_save(skus, args.output)

    pairs, tokens = capture_live_json(
        args.live_url,
        profile_dir=args.profile_dir,
        attach_cdp_url=args.attach_cdp_url,
    )
    print(f"Captured JSON responses: {len(pairs)}")
    print(f"Discovered auth token candidates: {len(tokens)}")
    if args.attach_cdp_url and not pairs:
        print(
            "Attach mode captured no JSON responses. "
            "No fallback browser was launched."
        )
    probe_pairs = probe_cart_with_tokens(tokens, args.live_url)
    if probe_pairs:
        pairs.extend(probe_pairs)
        print(f"Probed cart APIs with {len(tokens)} discovered token(s).")
    elif tokens:
        print(f"Discovered {len(tokens)} token(s), but probe endpoints returned no JSON cart payload.")
    else:
        print("No auth token discovered from browser storage/cookies.")
    if args.save_captured_json:
        dump = [{"url": u, "body": b} for u, b in pairs]
        Path(args.save_captured_json).write_text(
            json.dumps(dump, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    carts = extract_carts_from_pairs(pairs)
    name_index = build_name_index_from_pairs(pairs)
    apply_name_index(carts, name_index)
    city = infer_city_from_pairs(pairs)
    enrich_wellness_names_with_probe(carts, tokens=tokens, referer_url=args.live_url, city=city)
    if any(carts.values()):
        print_grouped_carts(carts)
        skus = collect_grouped_skus(carts)
        return print_and_save(skus, args.output)
    skus = extract_from_pairs(pairs, cart_only=args.cart_only)
    return print_and_save(skus, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
