import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PUBLIC_URL_FILE = os.path.join(os.path.dirname(__file__), "public_url.txt")
CUSTOM_DOMAIN_URL = os.environ.get("DZETVA_CUSTOM_DOMAIN", "https://dzetva.by").strip().rstrip("/")
TUNNEL_SYNC_SECRET = os.environ.get("TUNNEL_SYNC_SECRET", "dzetva_sync_2026_secret").strip()

_CURRENT_PUBLIC_URL: Optional[str] = None
_CUSTOM_DOMAIN_ACTIVE: bool = False
_LOCK = threading.Lock()
_STARTED = False

_TUNNEL_URL_RE = re.compile(
    r"https://[a-zA-Z0-9.-]+\.(?:lhr\.life|trycloudflare\.com)"
)


def get_raw_tunnel_url() -> str:
    """Return direct HTTPS tunnel URL (*.lhr.life / *.trycloudflare.com) from memory or file."""
    global _CURRENT_PUBLIC_URL

    with _LOCK:
        if _CURRENT_PUBLIC_URL:
            match = _TUNNEL_URL_RE.search(_CURRENT_PUBLIC_URL)
            if match:
                return match.group(0)

    if os.path.exists(PUBLIC_URL_FILE):
        try:
            with open(PUBLIC_URL_FILE, "r", encoding="utf-8") as f:
                for raw_line in f:
                    match = _TUNNEL_URL_RE.search(raw_line.strip())
                    if match:
                        found = match.group(0)
                        with _LOCK:
                            if not _CURRENT_PUBLIC_URL:
                                _CURRENT_PUBLIC_URL = found
                        return found
        except OSError as e:
            logger.warning(f"Failed to read {PUBLIC_URL_FILE}: {e}")

    return ""


def get_public_webapp_url(default_local: str = "http://192.168.1.35:8080") -> str:
    """Return active HTTPS WebApp URL (env -> custom domain -> raw tunnel) or local fallback."""
    env_url = os.environ.get("WEBAPP_PUBLIC_URL", "").strip()
    if env_url.startswith("https://"):
        return env_url

    with _LOCK:
        custom_active = _CUSTOM_DOMAIN_ACTIVE

    if custom_active and CUSTOM_DOMAIN_URL.startswith("https://"):
        return f"{CUSTOM_DOMAIN_URL}/app"

    raw_url = get_raw_tunnel_url()
    if raw_url:
        return raw_url

    return env_url or default_local


def _sync_to_custom_domain(raw_url: str) -> bool:
    """Push the active direct tunnel URL to the custom domain sync endpoint."""
    global _CUSTOM_DOMAIN_ACTIVE

    if not raw_url or not CUSTOM_DOMAIN_URL.startswith("https://"):
        return False
    if raw_url.rstrip("/") == CUSTOM_DOMAIN_URL:
        return False

    endpoint = f"{CUSTOM_DOMAIN_URL}/__dzetva_tunnel_sync"
    payload = json.dumps({"url": raw_url}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Tunnel-Secret": TUNNEL_SYNC_SECRET,
            "User-Agent": "DzetvaTunnelSync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
        if status == 200:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("ok"):
                with _LOCK:
                    _CUSTOM_DOMAIN_ACTIVE = True
                return True
    except Exception as e:
        logger.debug(f"Custom domain sync failed ({endpoint}): {e}")

    return False


def _sync_and_check_activation(raw_url: str) -> bool:
    """Sync raw_url to custom domain; return True if domain just switched False -> True."""
    with _LOCK:
        was_active = _CUSTOM_DOMAIN_ACTIVE
    if _sync_to_custom_domain(raw_url) and not was_active:
        logger.info(f"Custom domain active and synced: {CUSTOM_DOMAIN_URL}")
        return True
    return False


def _handle_detected_url(
    url: str,
    on_new_url: Optional[Callable[[str], None]] = None,
) -> None:
    """Persist newly detected tunnel URL, sync custom domain, and notify callback."""
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

    _sync_and_check_activation(url)
    effective_url = get_public_webapp_url()

    if on_new_url is not None:
        try:
            on_new_url(effective_url)
        except Exception as e:
            logger.warning(f"on_new_url callback failed for {effective_url}: {e}")


def _heartbeat_sync_loop(
    on_new_url: Optional[Callable[[str], None]] = None,
) -> None:
    """Periodically sync direct tunnel URL to custom domain and promote it once active."""
    while True:
        time.sleep(60)
        try:
            raw_url = get_raw_tunnel_url()
            if not raw_url:
                continue
            became_active = _sync_and_check_activation(raw_url)
            if became_active and on_new_url is not None:
                effective_url = get_public_webapp_url()
                try:
                    on_new_url(effective_url)
                except Exception as e:
                    logger.warning(f"on_new_url callback failed for {effective_url}: {e}")
        except Exception as e:
            logger.debug(f"Custom domain heartbeat error: {e}")


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
    """Start the background daemon threads for the SSH tunnel and domain sync (idempotent)."""
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

    sync_t = threading.Thread(
        target=_heartbeat_sync_loop,
        args=(on_new_url,),
        daemon=True,
        name="tunnel_domain_sync",
    )
    sync_t.start()
