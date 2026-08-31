r"""Matter Clerk desktop entry point.

This is the script PyInstaller freezes into MatterClerk.exe. Running it from a
source checkout does the same thing, which is the point -- the packaged path
and the development path differ only in where files are found, never in what
happens.

Sequence:
  1. Point TIKTOKEN_CACHE_DIR at the bundled BPE ranks. This must happen
     BEFORE anything imports matter_clerk.ingest, which builds its tiktoken
     encoder at module import time and would otherwise reach out to
     openaipublic.blob.core.windows.net -- an import-time crash on a machine
     with no internet, not a degraded feature.
  2. Create the data directory (%LOCALAPPDATA%\MatterClerk when frozen).
  3. Load .env from the data directory first, then the repo root. First value
     wins, so an installed .env is authoritative and a developer checkout still
     picks up its own.
  4. Start Flask on 127.0.0.1:5050 and open a browser once /healthz answers.

Usage:
    MatterClerk.exe [--no-browser]
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

DEFAULT_PORT = 5050
HOST = "127.0.0.1"

READY_TIMEOUT_SECONDS = 30.0
READY_POLL_SECONDS = 0.15


def _bootstrap_sys_path() -> None:
    """In a source checkout, put <repo>/src on sys.path. No-op when frozen."""
    if getattr(sys, "frozen", False):
        return
    from pathlib import Path

    src = Path(__file__).resolve().parent / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on host:port.

    An explicit connect probe, not a try-to-bind, because bind failure is not
    a reliable in-use signal on Windows: werkzeug sets SO_REUSEADDR, and where
    Linux rejects the second bind, Windows ACCEPTS it. Two Matter Clerk
    processes then hold the same port, connections are delivered to one of
    them arbitrarily, and -- the part that actually matters -- both open the
    same embedded ChromaDB directory for writing, which vectorstore.py is
    explicit about not supporting. Verified empirically on Windows 11 during
    Phase 3 Session 3: a second launcher bound an occupied 5050 and served
    without raising.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _wait_until_ready(url: str, timeout: float = READY_TIMEOUT_SECONDS) -> bool:
    """Poll /healthz until it identifies itself as Matter Clerk.

    Polling rather than sleeping a fixed interval: make_server() has already
    bound the socket by the time this thread starts, so this normally succeeds
    on the first attempt, and on a slow machine it simply waits longer instead
    of opening a browser at a connection-refused page.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if json.loads(r.read().decode("utf-8")).get("app") == "matter-clerk":
                    return True
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(READY_POLL_SECONDS)
    return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    open_browser = "--no-browser" not in argv

    _bootstrap_sys_path()

    # --- 1. tiktoken cache, before any matter_clerk.ingest import ------------
    from matter_clerk import paths

    cache = paths.tiktoken_cache_dir()
    if cache is not None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache))

    # --- 2. data directory ---------------------------------------------------
    data_dir = paths.ensure_data_dir()

    # --- 3. environment ------------------------------------------------------
    from dotenv import load_dotenv

    # load_dotenv does not override already-set variables, so the first file to
    # define a key wins. Installed config beats checked-out config.
    load_dotenv(data_dir / ".env")
    if not paths.is_frozen():
        load_dotenv(paths.repo_root() / ".env")

    # --- 4. server -----------------------------------------------------------
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)

    from werkzeug.serving import make_server

    from matter_clerk.web import create_app

    port = int(os.environ.get("MATTER_CLERK_PORT", str(DEFAULT_PORT)))
    url = f"http://{HOST}:{port}"

    print("Matter Clerk", flush=True)
    print(f"  data directory : {data_dir}", flush=True)
    print(f"  resources      : {'bundle' if paths.is_frozen() else paths.repo_root()}", flush=True)

    if _port_in_use(HOST, port):
        print(
            f"\nERROR: {HOST}:{port} is already in use.\n"
            "\n"
            "Matter Clerk is probably already running -- check your taskbar\n"
            f"for another Matter Clerk window, or open {url} in your browser.\n"
            "\n"
            "If something else is using the port, start Matter Clerk on a\n"
            "different one by setting MATTER_CLERK_PORT, e.g.\n"
            "    set MATTER_CLERK_PORT=5051\n",
            file=sys.stderr,
        )
        return 1

    app = create_app()

    try:
        server = make_server(HOST, port, app, threaded=True)
    except OSError as e:
        # Bind failure is almost always a Matter Clerk already running, since
        # the port is fixed. Say so in the terms the user can act on rather
        # than printing a WinError traceback into a console they cannot read.
        print(
            f"\nERROR: cannot listen on {HOST}:{port} ({e.strerror or e}).\n"
            "\n"
            "Matter Clerk is probably already running -- check your taskbar for\n"
            f"another Matter Clerk window, or open {url} in your browser.\n"
            "\n"
            "If something else is using the port, start Matter Clerk on a\n"
            "different one by setting MATTER_CLERK_PORT, e.g.\n"
            "    set MATTER_CLERK_PORT=5051\n",
            file=sys.stderr,
        )
        return 1

    # Preserves the Day-2 guarantee that in-flight requests unwind their
    # `finally` clauses on Ctrl+C, so upload tmp files are not leaked.
    server.daemon_threads = False

    if open_browser:
        def _open() -> None:
            if _wait_until_ready(f"{url}/healthz"):
                webbrowser.open(url)
            else:
                print(f"Server did not become ready in {READY_TIMEOUT_SECONDS:.0f}s; "
                      f"open {url} manually.", file=sys.stderr)

        threading.Thread(target=_open, daemon=True).start()

    print(f"\nListening at {url}", flush=True)
    print("Close this window or press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Shutting down (waiting for any in-flight requests to finish)...", flush=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
