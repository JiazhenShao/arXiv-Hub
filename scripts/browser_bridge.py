from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


@dataclass(frozen=True)
class BridgeDestination:
    generation: int
    url: str


def _validated_destination(
    generation: object,
    url: object,
) -> BridgeDestination:
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise ValueError("Bridge generation must be a non-negative integer")
    if generation < 0:
        raise ValueError("Bridge generation must be a non-negative integer")
    if not isinstance(url, str):
        raise ValueError("Bridge destination must be a URL")
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
    ):
        raise ValueError("Bridge destination must be a loopback HTTP URL")
    return BridgeDestination(generation=generation, url=url)


def atomic_write_destination(
    path: Path,
    *,
    generation: int,
    url: str,
) -> None:
    destination = _validated_destination(generation, url)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(
        {
            "generation": destination.generation,
            "url": destination.url,
        },
        sort_keys=True,
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_destination(path: Path) -> BridgeDestination | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return _validated_destination(
            payload.get("generation"),
            payload.get("url"),
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None


def handoff_url(port: int, token: str, *, after: int) -> str:
    return (
        f"http://127.0.0.1:{port}/"
        f"?token={quote(token)}&after={after}"
    )


def _waiting_page(token: str, after: int) -> bytes:
    config = json.dumps(
        {"token": token, "after": after},
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Switching arXiv Hub</title>
  <style>
    :root {{ color-scheme:light; }}
    body {{ min-height:100vh; margin:0; display:grid; place-items:center;
      color:#17211d; background:#f7f3e7; font-family:Georgia,serif; }}
    main {{ text-align:center; padding:2rem; }}
    h1 {{ margin:0 0 .6rem; font-size:clamp(2rem,7vw,4.5rem); }}
    p {{ color:#64726a; }}
  </style>
</head>
<body>
<main>
  <h1>Switching arXiv Hub</h1>
  <p>This tab will continue automatically.</p>
</main>
<script>
  const bridge = {config};
  async function continueInThisTab() {{
    try {{
      const query = new URLSearchParams({{
        token: bridge.token,
        after: String(bridge.after)
      }});
      const response = await fetch(`/api/destination?${{query}}`, {{
        cache: "no-store"
      }});
      if (response.ok) {{
        const result = await response.json();
        if (result.ready) {{
          window.location.replace(result.url);
          return;
        }}
      }}
    }} catch (error) {{
      // The supervisor may be between child processes; keep waiting.
    }}
    window.setTimeout(continueInThisTab, 120);
  }}
  continueInThisTab();
</script>
</body>
</html>""".encode("utf-8")


def create_bridge_server(
    *,
    token: str,
    state_path: Path,
    port: int = 0,
) -> ThreadingHTTPServer:
    class BridgeHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _query(self) -> dict[str, list[str]]:
            return parse_qs(urlparse(self.path).query)

        def _authorized(self, query: dict[str, list[str]]) -> bool:
            return secrets.compare_digest(
                query.get("token", [""])[0],
                token,
            )

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorized(query):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                after = int(query.get("after", ["0"])[0])
                if after < 0:
                    raise ValueError
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if parsed.path == "/":
                self._send(
                    HTTPStatus.OK,
                    _waiting_page(token, after),
                    "text/html; charset=utf-8",
                )
                return
            if parsed.path == "/api/destination":
                destination = read_destination(state_path)
                payload: dict[str, object] = {"ready": False}
                if (
                    destination is not None
                    and destination.generation > after
                ):
                    payload = {
                        "ready": True,
                        "generation": destination.generation,
                        "url": destination.url,
                    }
                self._send(
                    HTTPStatus.OK,
                    json.dumps(payload).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

    return ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
