"""
Proxy manager for Telegram connections with auto-selection.
Handles MTProto and SOCKS5 proxies with automatic failover.
"""

import asyncio
import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import urlparse, parse_qs

DEFAULT_PROXY_FILE = Path(__file__).parent.parent / "data" / "telegram_proxies.txt"


@dataclass
class ProxyConfig:
    """Proxy configuration"""
    type: Literal['mtproto', 'socks5']
    host: str
    port: int
    secret: Optional[str] = None  # For MTProto
    username: Optional[str] = None  # For SOCKS5
    password: Optional[str] = None  # For SOCKS5
    latency: Optional[float] = None  # Last measured latency in seconds
    last_check: Optional[float] = None  # Timestamp of last check
    is_working: Optional[bool] = None  # Last known status

    def __str__(self):
        if self.type == 'mtproto':
            return f"MTProto({self.host}:{self.port})"
        return f"SOCKS5({self.host}:{self.port})"


def parse_proxy_url(proxy_url: str) -> ProxyConfig:
    """Parse MTProto or SOCKS5 proxy URL into ProxyConfig."""
    raw = proxy_url.strip()
    if not raw:
        raise ValueError("empty proxy url")

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    if scheme in {'socks5', 'socks5h'}:
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"invalid SOCKS5 proxy URL: {raw}")
        return ProxyConfig(
            type='socks5',
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )

    if scheme == 'mtproto':
        secret = parsed.password or parsed.username
        if not secret:
            query = parse_qs(parsed.query)
            secret = (query.get('secret') or [None])[0]
        if not parsed.hostname or not parsed.port or not secret:
            raise ValueError(f"invalid MTProto proxy URL: {raw}")
        return ProxyConfig(
            type='mtproto',
            host=parsed.hostname,
            port=parsed.port,
            secret=secret,
        )

    if scheme == 'tg':
        query = parse_qs(parsed.query)
        host = (query.get('server') or [None])[0]
        port = (query.get('port') or [None])[0]
        secret = (query.get('secret') or [None])[0]
        if not host or not port or not secret:
            raise ValueError(f"invalid tg:// proxy URL: {raw}")
        return ProxyConfig(type='mtproto', host=host, port=int(port), secret=secret)

    raise ValueError(f"unsupported proxy URL scheme: {scheme}")


def _iter_configured_proxy_urls() -> list[str]:
    """Load proxy URLs from env var or file."""
    urls: list[str] = []

    env_urls = os.getenv('TELEGRAM_PROXY_URLS', '').strip()
    if env_urls:
        urls.extend(part.strip() for part in re.split(r'[\n,;]+', env_urls) if part.strip())
        return urls

    proxy_file = os.getenv('TELEGRAM_PROXY_FILE', '').strip()
    candidate_paths: list[Path] = []
    if proxy_file:
        candidate_paths.append(Path(proxy_file).expanduser())
    candidate_paths.append(DEFAULT_PROXY_FILE)

    seen_paths: set[Path] = set()
    for path in candidate_paths:
        if path in seen_paths:
            continue
        seen_paths.add(path)

        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    urls.append(line)
            break

        if proxy_file and path == Path(proxy_file).expanduser():
            print(f"[ProxyManager] Proxy file not found: {path}", flush=True)

    return urls


def load_configured_proxies() -> list[ProxyConfig]:
    """Load proxies from configuration without embedding secrets in code."""
    proxies: list[ProxyConfig] = []
    for raw in _iter_configured_proxy_urls():
        try:
            proxies.append(parse_proxy_url(raw))
        except Exception as e:
            print(f"[ProxyManager] Skipping invalid proxy config '{raw}': {e}", flush=True)

    if proxies:
        print(f"[ProxyManager] Loaded {len(proxies)} proxies from configuration", flush=True)
    else:
        print("[ProxyManager] No configured proxies found", flush=True)

    return proxies


class ProxyManager:
    """Manages proxy selection and health checking for Telegram connections"""

    def __init__(self, proxies: list[ProxyConfig] = None):
        self.proxies = list(proxies) if proxies is not None else load_configured_proxies()
        self._current_proxy: Optional[ProxyConfig] = None
        self._check_lock = asyncio.Lock()
        self._last_full_check: float = 0
        self._check_interval = 300  # 5 minutes between full checks
        self._monitor_task: Optional[asyncio.Task] = None
        self._monitor_stop = asyncio.Event()

    @property
    def current_proxy(self) -> Optional[ProxyConfig]:
        return self._current_proxy

    def _normalize_mtproto_secret(self, secret: Optional[str]) -> Optional[str]:
        """
        Normalize MTProto secret to hex format supported by this Telethon version.
        Returns normalized hex string or None if the proxy is unsupported.
        """
        if not secret:
            return None

        if all(c in '0123456789abcdefABCDEF' for c in secret) and len(secret) % 2 == 0:
            return secret.lower()

        try:
            padded = secret + ('=' * ((4 - len(secret) % 4) % 4))
            decoded = base64.urlsafe_b64decode(padded)
        except Exception:
            return None

        # This Telethon build supports only classic 16-byte secrets and dd-secrets.
        if len(decoded) == 16:
            return decoded.hex()
        if len(decoded) == 17 and decoded[0] == 0xDD:
            return decoded.hex()
        return None

    def _is_supported_proxy(self, proxy: ProxyConfig) -> bool:
        """Check whether proxy config is usable by the current Telethon client."""
        if proxy.type != 'mtproto':
            return True
        return self._normalize_mtproto_secret(proxy.secret) is not None

    async def check_proxy(self, proxy: ProxyConfig, timeout: float = 5.0) -> bool:
        """
        Check if a proxy is working by attempting a TCP connection.
        Returns True if proxy responds, False otherwise.
        """
        if not self._is_supported_proxy(proxy):
            proxy.is_working = False
            proxy.latency = None
            proxy.last_check = time.time()
            print(f"[ProxyManager] Skipping unsupported proxy config: {proxy}", flush=True)
            return False

        start_time = time.time()
        try:
            # Simple TCP connect check
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(proxy.host, proxy.port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()

            proxy.latency = time.time() - start_time
            proxy.is_working = True
            proxy.last_check = time.time()
            return True

        except Exception as e:
            proxy.is_working = False
            proxy.latency = None
            proxy.last_check = time.time()
            return False

    async def check_all_proxies(self, timeout: float = 5.0) -> list[ProxyConfig]:
        """
        Check all proxies in parallel and return working ones sorted by latency.
        """
        async with self._check_lock:
            print(f"[ProxyManager] Checking {len(self.proxies)} proxies...", flush=True)

            tasks = [self.check_proxy(proxy, timeout) for proxy in self.proxies]
            await asyncio.gather(*tasks, return_exceptions=True)

            working = [p for p in self.proxies if p.is_working]
            working.sort(key=lambda p: p.latency or float('inf'))

            print(f"[ProxyManager] Found {len(working)} working proxies", flush=True)
            if working:
                print(f"[ProxyManager] Best: {working[0]} ({working[0].latency:.2f}s)", flush=True)

            self._last_full_check = time.time()
            return working

    async def get_best_proxy(self, force_recheck: bool = False) -> Optional[ProxyConfig]:
        """
        Get the best available proxy. Rechecks if needed or forced.
        """
        # Check if we need to recheck
        should_recheck = (
            force_recheck or
            self._current_proxy is None or
            (time.time() - self._last_full_check) > self._check_interval
        )

        if should_recheck:
            working = await self.check_all_proxies()
            if working:
                self._current_proxy = working[0]
            else:
                self._current_proxy = None

        return self._current_proxy

    async def ensure_current_proxy(self) -> Optional[ProxyConfig]:
        """Ensure we have a currently healthy proxy selected."""
        if self._current_proxy and self._current_proxy.is_working:
            if (
                self._current_proxy.last_check is not None and
                (time.time() - self._current_proxy.last_check) <= self._check_interval
            ):
                return self._current_proxy
        return await self.get_best_proxy(force_recheck=True)

    async def get_next_proxy(self) -> Optional[ProxyConfig]:
        """
        Get next working proxy (when current one fails).
        Marks current as not working and returns next best.
        """
        if self._current_proxy:
            self._current_proxy.is_working = False
            print(f"[ProxyManager] Marking {self._current_proxy} as failed", flush=True)

        # Get next working proxy without full recheck
        working = [p for p in self.proxies if p.is_working]
        if working:
            working.sort(key=lambda p: p.latency or float('inf'))
            self._current_proxy = working[0]
            print(f"[ProxyManager] Switching to {self._current_proxy}", flush=True)
            return self._current_proxy

        # No known working proxies - do full recheck
        print("[ProxyManager] No known working proxies, rechecking all...", flush=True)
        return await self.get_best_proxy(force_recheck=True)

    async def start_background_monitor(self, interval: float = 120.0):
        """Start background proxy health monitoring."""
        if self._monitor_task and not self._monitor_task.done():
            return

        self._monitor_stop = asyncio.Event()

        async def _monitor_loop():
            print(f"[ProxyManager] Background monitor started, interval={interval}s", flush=True)
            try:
                while not self._monitor_stop.is_set():
                    try:
                        await self.ensure_current_proxy()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"[ProxyManager] Background monitor error: {e}", flush=True)

                    try:
                        await asyncio.wait_for(self._monitor_stop.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        continue
            except asyncio.CancelledError:
                raise
            finally:
                print("[ProxyManager] Background monitor stopped", flush=True)

        self._monitor_task = asyncio.create_task(_monitor_loop())

    async def stop_background_monitor(self):
        """Stop background proxy monitoring."""
        if not self._monitor_task:
            return

        self._monitor_stop.set()
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        self._monitor_task = None

    def get_telethon_proxy_args(self, proxy: ProxyConfig) -> dict:
        """
        Convert ProxyConfig to Telethon client arguments.
        Returns dict with 'proxy' and optionally 'connection' keys.
        """
        if proxy.type == 'mtproto':
            secret = self._normalize_mtproto_secret(proxy.secret)
            if not secret:
                print(f"[ProxyManager] Unsupported MTProto secret format for {proxy}, skipping", flush=True)
                return {}
            from telethon.network import connection
            return {
                'connection': connection.ConnectionTcpMTProxyRandomizedIntermediate,
                'proxy': (proxy.host, proxy.port, secret)
            }
        else:  # socks5
            try:
                import socks
                return {
                    'proxy': (socks.SOCKS5, proxy.host, proxy.port, True, proxy.username, proxy.password)
                }
            except ImportError:
                print("[ProxyManager] WARNING: pysocks not installed, SOCKS proxy unavailable. Run: pip install pysocks", flush=True)
                return {}


# Global instance
_proxy_manager: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    """Get or create the global proxy manager instance"""
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager()
    return _proxy_manager


def init_proxy_manager(proxies: list[ProxyConfig] = None) -> ProxyManager:
    """Initialize the global proxy manager with optional custom proxy list"""
    global _proxy_manager
    _proxy_manager = ProxyManager(proxies)
    return _proxy_manager
