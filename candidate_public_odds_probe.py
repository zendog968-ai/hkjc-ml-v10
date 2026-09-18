from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

TARGET = "https://bet.hkjc.com/ch/racing/wp"
GRAPH = "consvc.hkjc.com/JCBW/api/graph"
OUT = Path("/tmp/candidate_public_odds_probe.json")


def walk(obj, path="$"):
    if isinstance(obj, dict):
        yield path, obj
        for key, value in obj.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj[:200]):
            yield from walk(value, f"{path}[{index}]")


def main():
    observations = []
    sanitized = {"target": TARGET, "graph_post_count": 0, "responses": [], "public_odds_nodes": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/home/ubuntu/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome", headless=True, args=["--no-sandbox"])
        page = browser.new_page(locale="zh-HK", user_agent="Mozilla/5.0 (V10 candidate public odds observer)")

        def on_response(response):
            url = response.url
            parsed = urlsplit(url)
            if "consvc.hkjc.com/JCBW/api/graph" not in url or response.request.method.upper() != "POST":
                return
            sanitized["graph_post_count"] += 1
            item = {"path": f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "status": response.status, "content_type": response.headers.get("content-type", "")}
            try:
                payload = response.json()
                item["json"] = True
                root_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
                item["root_keys"] = root_keys[:40]
                for path, node in walk(payload):
                    if not isinstance(node, dict):
                        continue
                    keys = {str(k).lower() for k in node.keys()}
                    if "pmpools" in keys or ("odds" in keys and ("win" in keys or "pla" in keys)):
                        sanitized["public_odds_nodes"].append({"path": path, "keys": sorted(map(str, node.keys()))[:40], "list_lengths": {str(k): len(v) for k, v in node.items() if isinstance(v, list)}})
                item["pmPools_node_count"] = sum(1 for path, node in walk(payload) if isinstance(node, dict) and "pmpools" in {str(k).lower() for k in node.keys()})
                shapes = []
                for shape_path, shape_node in walk(payload):
                    if not isinstance(shape_node, dict):
                        continue
                    shape_keys = sorted(map(str, shape_node.keys()))
                    if shape_keys:
                        shapes.append({"path": shape_path, "keys": shape_keys[:30], "list_lengths": {str(k): len(v) for k, v in shape_node.items() if isinstance(v, list)}})
                    if len(shapes) >= 120:
                        break
                item["data_shapes"] = shapes
            except Exception as exc:
                item["json"] = False
                item["parse_error_type"] = type(exc).__name__
            sanitized["responses"].append(item)

        page.on("response", on_response)
        try:
            response = page.goto(TARGET, wait_until="domcontentloaded", timeout=20_000)
            sanitized["page_status"] = response.status if response is not None else None
            page.wait_for_timeout(15_000)
        except Exception as exc:
            sanitized["page_error_type"] = type(exc).__name__
        finally:
            browser.close()
    OUT.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(sanitized, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

# Safety boundary: this probe deliberately does not read request headers, cookies,
# Authorization, sc_apikey values, or persist complete GraphQL response bodies.
# It is a one-page candidate observation only and never writes production artifacts.

# end

# Candidate-only public response probe.
