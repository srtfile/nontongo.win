#!/usr/bin/env python3
"""
Stream URL resolver – extracts all streaming URLs (m3u8, mp4, mpd, iframes, embeds)
from a given embed/page URL using plain HTTP requests only.

CLI / GitHub Actions:
  python stream_resolver.py --url https://www.nontongo.win/embed/movie/254
  python stream_resolver.py --url https://www.nontongo.win/embed/movie/254 --json

Web server (Render):
  python stream_resolver.py --serve --port 10000
  (or set PORT env var — Render does this automatically)
"""

from __future__ import annotations

import argparse
import base64
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MEDIA_EXTENSIONS = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".mov", ".ts", ".vtt")

URL_RE = re.compile(r"""(?ix)\bhttps?://[^\s"'<>\\\])}]+""")
IFRAME_RE = re.compile(r"""(?is)<iframe\b[^>]*\bsrc\s*=\s*(['"])(?P<src>.*?)\1""")
ATTR_URL_RE = re.compile(
    r"""(?is)\b(?:src|href|data-link|data-src|poster|file)\s*=\s*(['"])(?P<url>.*?)\1"""
)
WINDOW_LOCATION_RE = re.compile(
    r"""(?is)(?:window\.)?location(?:\.href)?\s*=\s*(['"])(?P<url>https?://.*?|/.*?)\1"""
)
ATOB_RE = re.compile(
    r"""(?is)\batob\(\s*(['"])(?P<data>[A-Za-z0-9+/=_-]{16,})\1\s*\)"""
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def unique_keep_order(items: Iterable[Any]) -> List[Any]:
    seen: Set[str] = set()
    out: List[Any] = []
    for item in items:
        key = (
            json.dumps(item, sort_keys=True, default=str)
            if not isinstance(item, str)
            else item
        )
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def normalize_url(raw: str, base: Optional[str] = None) -> Optional[str]:
    if not raw:
        return None
    raw = html.unescape(raw).strip().replace("\\/", "/").rstrip(".,;")
    if raw.startswith("//"):
        raw = "https:" + raw
    if base:
        raw = urllib.parse.urljoin(base, raw)
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urllib.parse.urlunparse(parsed)


def extract_urls_from_text(text: str, base: Optional[str] = None) -> List[str]:
    urls: List[str] = []
    decoded = html.unescape(text).replace("\\/", "/")

    for match in URL_RE.finditer(decoded):
        url = normalize_url(match.group(0), base)
        if url:
            urls.append(url)

    for regex in (IFRAME_RE, ATTR_URL_RE, WINDOW_LOCATION_RE):
        for match in regex.finditer(decoded):
            raw = match.groupdict().get("src") or match.groupdict().get("url") or ""
            url = normalize_url(raw, base)
            if url:
                urls.append(url)

    for match in ATOB_RE.finditer(decoded):
        blob = match.group("data")
        padded = blob + ("=" * (-len(blob) % 4))
        for alt_blob in (padded, padded.replace("-", "+").replace("_", "/")):
            try:
                inner = base64.b64decode(alt_blob).decode("utf-8", errors="ignore")
            except Exception:
                continue
            urls.extend(extract_urls_from_text(inner, base))
            break

    return unique_keep_order(urls)


def media_kind(url: str) -> Optional[str]:
    parsed = urllib.parse.urlparse(url)
    path = (parsed.path + "#" + parsed.fragment).lower()
    query = urllib.parse.parse_qs(parsed.query)

    for ext in MEDIA_EXTENSIONS:
        if path.endswith(ext):
            return ext.lstrip(".")

    for mime in query.get("mime", []) + query.get("type", []):
        mime_l = urllib.parse.unquote_plus(mime).lower()
        if "mpegurl" in mime_l or "m3u8" in mime_l or "hls" in mime_l:
            return "m3u8"
        if "dash" in mime_l or "mpd" in mime_l:
            return "mpd"
        if "video/mp4" in mime_l or "audio/mp4" in mime_l or mime_l.endswith("/mp4"):
            return "mp4"
        if "webm" in mime_l:
            return "webm"

    if "videoplayback" in path and any(
        "mp4" in urllib.parse.unquote_plus(v).lower()
        for values in query.values()
        for v in values
    ):
        return "mp4"

    for values in query.values():
        for value in values:
            value_path = urllib.parse.urlparse(value).path.lower()
            for ext in MEDIA_EXTENSIONS:
                if value_path.endswith(ext):
                    return ext.lstrip(".")
    return None


def classify_url(url: str) -> str:
    kind = media_kind(url)
    if kind:
        return "media"
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    if "/embed/" in path or "/e/" in path or "iframe" in path:
        return "iframe_or_embed"
    if any(
        part in path
        for part in ("/api/", "/ajax", "/xhr", "/getplay", "/load", "/dl")
    ):
        return "api_or_xhr"
    if path.endswith(".js"):
        return "javascript"
    if path.endswith(
        (".css", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".woff", ".woff2")
    ):
        return "asset"
    return "page_or_other"


def is_stream_url(url: str) -> bool:
    """Return True if the URL looks like a streaming/embed/media resource."""
    if media_kind(url):
        return True
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    query = parsed.query.lower()
    cat = classify_url(url)
    if cat == "iframe_or_embed":
        return True
    if "/embed/" in path or "/e/" in path or "embed" in host:
        return True
    if (
        "/stream/" in path
        or "stream" in host
        or "stream" in query
        or "file_code=" in query
        or "op=view" in query
        or path.endswith(("/dl", "/load"))
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# HTTP Resolver
# ---------------------------------------------------------------------------

class Resolver:
    def __init__(self, timeout: int = 15, max_pages: int = 10):
        self.timeout = timeout
        self.max_pages = max_pages
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar),
            urllib.request.HTTPRedirectHandler(),
        )

    def resolve(self, input_url: str) -> Dict[str, Any]:
        started = time.time()
        queue: deque[Tuple[str, str]] = deque([(input_url, "")])
        seen: Set[str] = set()
        all_found: List[str] = []
        errors: List[str] = []
        blocked = False

        while queue and len(seen) < self.max_pages:
            url, referrer = queue.popleft()
            if url in seen:
                continue
            seen.add(url)

            try:
                response_url, status, content_type, body = self._fetch(url, referrer)
            except urllib.error.HTTPError as exc:
                body = ""
                status = exc.code
                response_url = url
                content_type = ""
                errors.append(f"HTTP {exc.code} on {url}: {exc.reason}")
            except Exception as exc:
                errors.append(f"Error on {url}: {exc}")
                continue

            if self._looks_blocked(status, body, content_type):
                blocked = True
                errors.append(f"Blocked/challenged on {url} (status={status})")
                continue

            found = extract_urls_from_text(body, response_url)
            all_found.extend(found)

            for next_url in found:
                if next_url in seen:
                    continue
                cat = classify_url(next_url)
                if cat in {"iframe_or_embed", "api_or_xhr"}:
                    if len(seen) + len(queue) < self.max_pages:
                        queue.append((next_url, response_url))

        # Deduplicate and collect all streaming-related URLs
        unique_all = unique_keep_order(all_found)
        stream_urls = [u for u in unique_all if is_stream_url(u)]

        # Categorize
        m3u8 = [u for u in stream_urls if media_kind(u) == "m3u8"]
        mp4  = [u for u in stream_urls if media_kind(u) == "mp4"]
        mpd  = [u for u in stream_urls if media_kind(u) == "mpd"]
        other_media = [
            u for u in stream_urls
            if media_kind(u) and media_kind(u) not in {"m3u8", "mp4", "mpd"}
        ]
        iframes = [u for u in stream_urls if classify_url(u) == "iframe_or_embed"]
        embeds  = [
            u for u in stream_urls
            if "/embed/" in urllib.parse.urlparse(u).path.lower() or "embed" in urllib.parse.urlparse(u).netloc.lower()
        ]

        elapsed = int((time.time() - started) * 1000)
        return {
            "input_url": input_url,
            "blocked": blocked,
            "elapsed_ms": elapsed,
            "errors": errors,
            "streaming_urls": {
                "m3u8": unique_keep_order(m3u8),
                "mp4": unique_keep_order(mp4),
                "mpd": unique_keep_order(mpd),
                "other_media": unique_keep_order(other_media),
                "iframes_embeds": unique_keep_order(iframes + embeds),
                "all_flat": unique_keep_order(stream_urls),
            },
            "counts": {
                "m3u8": len(unique_keep_order(m3u8)),
                "mp4": len(unique_keep_order(mp4)),
                "mpd": len(unique_keep_order(mpd)),
                "other_media": len(unique_keep_order(other_media)),
                "iframes_embeds": len(unique_keep_order(iframes + embeds)),
                "total": len(unique_keep_order(stream_urls)),
            },
        }

    def _fetch(self, url: str, referrer: str = "") -> Tuple[str, int, str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        if referrer:
            headers["Referer"] = referrer
        req = urllib.request.Request(url, headers=headers, method="GET")
        with self.opener.open(req, timeout=self.timeout) as response:
            raw = response.read(2_000_000)
            content_type = response.headers.get("content-type", "")
            charset = response.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="ignore")
            return response.geturl(), response.status, content_type, body

    @staticmethod
    def _looks_blocked(status: Optional[int], body: str, content_type: str) -> bool:
        if status in {401, 403, 429, 503}:
            return True
        lowered = (body[:50_000] + " " + content_type).lower()
        return any(
            kw in lowered
            for kw in ("cf-chl", "cloudflare", "captcha", "turnstile", "verify you are human", "access denied")
        )


# ---------------------------------------------------------------------------
# Web server (Render deployment)
# ---------------------------------------------------------------------------

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stream URL Resolver</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f0f13; color: #e0e0e0; min-height: 100vh; padding: 24px; }
  h1 { color: #7c6aff; font-size: 1.6rem; margin-bottom: 4px; }
  .sub { color: #888; font-size: 0.85rem; margin-bottom: 28px; }
  .card { background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 10px; padding: 20px; margin-bottom: 18px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  input[type=text] { flex: 1; min-width: 280px; background: #0f0f13; border: 1px solid #3a3a50; border-radius: 6px; padding: 10px 14px; color: #e0e0e0; font-size: 0.95rem; outline: none; }
  input[type=text]:focus { border-color: #7c6aff; }
  button { background: #7c6aff; color: #fff; border: none; border-radius: 6px; padding: 10px 22px; font-size: 0.95rem; cursor: pointer; white-space: nowrap; }
  button:hover { background: #6a58ee; }
  button:disabled { background: #3a3a50; cursor: default; }
  .badge { display: inline-block; font-size: 0.72rem; font-weight: 600; padding: 2px 8px; border-radius: 4px; margin-left: 6px; vertical-align: middle; }
  .b-m3u8 { background: #1a3a2a; color: #4caf80; }
  .b-mp4  { background: #2a2a1a; color: #c9a84c; }
  .b-mpd  { background: #1a2a3a; color: #4c8acf; }
  .b-other{ background: #2a1a2a; color: #c94cc9; }
  .b-embed{ background: #1a1a3a; color: #888cff; }
  .section-title { font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: #7c6aff; margin: 14px 0 8px; }
  .url-list { list-style: none; }
  .url-list li { margin: 4px 0; }
  .url-list a { color: #a0c8ff; font-size: 0.82rem; word-break: break-all; text-decoration: none; }
  .url-list a:hover { text-decoration: underline; }
  .copy-btn { font-size: 0.72rem; background: #2a2a3a; color: #aaa; border: none; border-radius: 4px; padding: 2px 8px; cursor: pointer; margin-left: 6px; }
  .copy-btn:hover { background: #3a3a50; color: #fff; }
  .empty { color: #555; font-size: 0.85rem; }
  .error-box { background: #2a1a1a; border: 1px solid #5a2a2a; border-radius: 6px; padding: 10px 14px; color: #e07070; font-size: 0.82rem; margin-top: 10px; }
  .stats { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  .stat { background: #0f0f13; border: 1px solid #2a2a3a; border-radius: 6px; padding: 6px 14px; font-size: 0.82rem; }
  .stat strong { color: #7c6aff; }
  #spinner { display: none; color: #7c6aff; font-size: 0.9rem; margin-left: 12px; }
  #results { display: none; }
</style>
</head>
<body>
<h1>🎬 Stream URL Resolver</h1>
<p class="sub">Extracts m3u8 / mp4 / mpd / iframe / embed streaming URLs from any embed page.</p>

<div class="card">
  <div class="row">
    <input type="text" id="urlInput" placeholder="https://www.nontongo.win/embed/movie/254" />
    <button id="resolveBtn" onclick="resolve()">Resolve</button>
    <span id="spinner">⏳ Resolving…</span>
  </div>
</div>

<div id="results" class="card">
  <div id="statsDiv" class="stats"></div>
  <div id="sectionsDiv"></div>
  <div id="errorsDiv"></div>
</div>

<script>
async function resolve() {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) return;
  const btn = document.getElementById('resolveBtn');
  btn.disabled = true;
  document.getElementById('spinner').style.display = 'inline';
  document.getElementById('results').style.display = 'none';

  try {
    const res = await fetch('/resolve?url=' + encodeURIComponent(url));
    const data = await res.json();
    renderResults(data);
  } catch(e) {
    renderResults({ error: String(e), streaming_urls: {}, counts: {}, errors: [String(e)] });
  }
  btn.disabled = false;
  document.getElementById('spinner').style.display = 'none';
}

function renderResults(data) {
  const c = data.counts || {};
  document.getElementById('statsDiv').innerHTML = `
    <div class="stat">Total <strong>${c.total || 0}</strong></div>
    <div class="stat">m3u8 <strong>${c.m3u8 || 0}</strong></div>
    <div class="stat">mp4 <strong>${c.mp4 || 0}</strong></div>
    <div class="stat">mpd <strong>${c.mpd || 0}</strong></div>
    <div class="stat">iframes/embeds <strong>${c.iframes_embeds || 0}</strong></div>
    <div class="stat">⏱ ${data.elapsed_ms || 0}ms</div>
    ${data.blocked ? '<div class="stat" style="color:#e07070">⚠ Blocked/CF</div>' : ''}
  `;

  const su = data.streaming_urls || {};
  const sections = [
    { key: 'm3u8',         label: 'HLS / m3u8',     badge: 'b-m3u8'  },
    { key: 'mp4',          label: 'MP4',             badge: 'b-mp4'   },
    { key: 'mpd',          label: 'DASH / mpd',      badge: 'b-mpd'   },
    { key: 'other_media',  label: 'Other Media',     badge: 'b-other' },
    { key: 'iframes_embeds',label: 'Iframes / Embeds',badge: 'b-embed'},
  ];

  let html = '';
  for (const s of sections) {
    const urls = su[s.key] || [];
    html += `<div class="section-title">${s.label} <span class="badge ${s.badge}">${urls.length}</span></div>`;
    if (!urls.length) { html += '<p class="empty">None found</p>'; continue; }
    html += '<ul class="url-list">';
    for (const u of urls) {
      html += `<li><a href="${u}" target="_blank" rel="noopener">${u}</a><button class="copy-btn" onclick="copyUrl('${u.replace(/'/g,"\\'")}')">copy</button></li>`;
    }
    html += '</ul>';
  }
  document.getElementById('sectionsDiv').innerHTML = html;

  const errs = data.errors || [];
  document.getElementById('errorsDiv').innerHTML = errs.length
    ? `<div class="error-box"><strong>Errors / Warnings:</strong><br>${errs.join('<br>')}</div>` : '';

  document.getElementById('results').style.display = 'block';
}

function copyUrl(url) {
  navigator.clipboard.writeText(url).catch(() => {});
}

document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') resolve();
});
</script>
</body>
</html>
"""


class ResolveHandler(BaseHTTPRequestHandler):
    server_version = "StreamResolver/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/resolve":
            query = urllib.parse.parse_qs(parsed.query)
            url = (query.get("url") or [None])[0]
            if not url:
                self._json({"error": "missing ?url= parameter"}, 400)
                return
            try:
                result = Resolver().resolve(url)
            except Exception as exc:
                result = {
                    "input_url": url,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "streaming_urls": {},
                    "counts": {},
                    "errors": [str(exc)],
                }
            self._json(result)
            return

        self._json({"error": "not found", "paths": ["/", "/resolve?url=..."]}, 404)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), ResolveHandler)
    print(f"Stream Resolver running on http://{host}:{port}", flush=True)
    print(f"Web UI:  http://{host}:{port}/", flush=True)
    print(f"API:     http://{host}:{port}/resolve?url=<encoded_url>", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract all streaming URLs from an embed/page URL."
    )
    parser.add_argument("--url", help="Input embed/page URL to resolve.")
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Output full JSON instead of plain URL list."
    )
    parser.add_argument("--serve", action="store_true", help="Start web server.")
    parser.add_argument("--host", default="0.0.0.0", help="Server bind host.")
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Server bind port (or set PORT env var)."
    )
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds.")
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages to follow.")
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.host, args.port)
        return 0

    if not args.url:
        parser.print_help()
        return 1

    result = Resolver(timeout=args.timeout, max_pages=args.max_pages).resolve(args.url)

    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    # Plain output: just the streaming URLs
    su = result.get("streaming_urls", {})
    sections = [
        ("HLS/m3u8",       su.get("m3u8", [])),
        ("MP4",            su.get("mp4", [])),
        ("DASH/mpd",       su.get("mpd", [])),
        ("Other Media",    su.get("other_media", [])),
        ("Iframes/Embeds", su.get("iframes_embeds", [])),
    ]

    total = result.get("counts", {}).get("total", 0)
    print(f"\n=== Stream URL Resolver | {result['input_url']} ===")
    print(f"Found {total} streaming URLs in {result.get('elapsed_ms', 0)}ms")
    if result.get("blocked"):
        print("⚠  Site returned a block/challenge — results may be incomplete.")

    for label, urls in sections:
        if not urls:
            continue
        print(f"\n--- {label} ({len(urls)}) ---")
        for u in urls:
            print(u)

    if result.get("errors"):
        print("\n--- Errors/Warnings ---")
        for e in result["errors"]:
            print(e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())