import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PUBLIC_URL_FILE = os.path.join(os.path.dirname(__file__), "public_url.txt")

_CURRENT_PUBLIC_URL: Optional[str] = None
_LOCK = threading.Lock()
_STARTED = False

_TUNNEL_URL_RE = re.compile(
    r"https://[a-zA-Z0-9.-]+\.(?:lhr\.life|trycloudflare\.com)"
)


def get_public_webapp_url(default_local: str = "http://192.168.1.35:8080") -> str:
    """Return active HTTPS WebApp URL (env -> memory -> file) or fall back to local URL."""
    global _CURRENT_PUBLIC_URL

    env_url = os.environ.get("WEBAPP_PUBLIC_URL", "").strip()
    if env_url.startswith("https://"):
        return env_url

    with _LOCK:
        if _CURRENT_PUBLIC_URL and _CURRENT_PUBLIC_URL.startswith("https://"):
            return _CURRENT_PUBLIC_URL

    if os.path.exists(PUBLIC_URL_FILE):
        try:
            with open(PUBLIC_URL_FILE, "r", encoding="utf-8") as f:
                for raw_line in f:
                    candidate = raw_line.strip()
                    if candidate.startswith("https://"):
                        with _LOCK:
                            _CURRENT_PUBLIC_URL = candidate
                        return candidate
        except OSError as e:
            logger.warning(f"Failed to read {PUBLIC_URL_FILE}: {e}")

    return env_url or default_local


def _handle_detected_url(
    url: str,
    on_new_url: Optional[Callable[[str], None]] = None,
) -> None:
    """Persist newly detected tunnel URL and notify callback if it changed."""
    global _CURRENT_PUBLIC_URL

    with _LOCK:
        if url == _CURRENT_PUBLIC_URL:
            return
        _CURRENT_PUBLIC_URL = url

    try:
        with open(PUBLIC_URL_FILE, "w", encoding="utf-8") as f:
            f.write(url + "\n")
    except OSError as e:
        logger.warning(f"Failed to write {PUBLIC_URL_FILE}: {e}")

    logger.info(f"Public HTTPS tunnel active: {url}")

    if on_new_url is not None:
        try:
            on_new_url(url)
        except Exception as e:
            logger.warning(f"on_new_url callback failed for {url}: {e}")


def _tunnel_loop(
    local_port: int,
    on_new_url: Optional[Callable[[str], None]] = None,
) -> None:
    """Keep an SSH reverse tunnel open and auto-restart it on exit or error."""
    cmd = [
        "ssh",
        "-T",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "ExitOnForwardFailure=yes",
        "-R", f"80:localhost:{local_port}",
        "nokey@localhost.run",
    ]

    while True:
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                bufsize=1,
            )
            if proc.stdout is not None:
                for line in proc.stdout:
                    match = _TUNNEL_URL_RE.search(line)
                    if match:
                        _handle_detected_url(match.group(0), on_new_url=on_new_url)
            rc = proc.wait()
            logger.warning(f"SSH tunnel exited with code {rc}; restarting in 5s...")
        except Exception as e:
            logger.warning(f"SSH tunnel error: {e}; restarting in 5s...")
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        finally:
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
        time.sleep(5)


def start_tunnel_thread(
    local_port: int = 8080,
    on_new_url: Optional[Callable[[str], None]] = None,
) -> None:
    """Start the background daemon thread managing the SSH tunnel (idempotent)."""
    global _STARTED

    with _LOCK:
        if _STARTED:
            return
        _STARTED = True

    t = threading.Thread(
        target=_tunnel_loop,
        args=(local_port, on_new_url),
        daemon=True,
        name="tunnel_manager",
    )
    t.start()
