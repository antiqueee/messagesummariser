"""
Proxy manager for Telegram connections with auto-selection.
Handles MTProto and SOCKS5 proxies with automatic failover.
"""

import asyncio
import time
import random
from dataclasses import dataclass
from typing import Optional, Literal
from urllib.parse import urlparse, parse_qs, unquote


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


# Embedded proxy list - parsed from user-provided URLs
EMBEDDED_PROXIES: list[ProxyConfig] = [
    # MTProto proxies
    ProxyConfig(type='mtproto', host='ppl.vpnpplvpn.top', port=8443, secret='ddbd7949ea22934a3af773233bd1e6cd87'),
    ProxyConfig(type='mtproto', host='87.242.100.25', port=443, secret='7vpB16tZIxq3FLeGXJAWoTVhZHMueDUucnU'),
    ProxyConfig(type='mtproto', host='95.174.92.184', port=443, secret='7oXkkzKLeg0bKvbEsP2UilBhZHMueDUucnU'),
    ProxyConfig(type='mtproto', host='176.109.105.129', port=443, secret='7iSMQ7eu6Xxv6IcxFFKjIwJhZHMueDUucnU'),
    ProxyConfig(type='mtproto', host='84.201.179.235', port=443, secret='7iSMQ7eu6Xxv6IcxFFKjIwJhZHMueDUucnU'),
    ProxyConfig(type='mtproto', host='51.250.89.205', port=443, secret='7iSMQ7eu6Xxv6IcxFFKjIwJhZHMueDUucnU'),
    ProxyConfig(type='mtproto', host='tgrmx.ru', port=443, secret='dd6575860df1848335b7110ca37d01f14a'),
    ProxyConfig(type='mtproto', host='185.130.113.138', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='proxy99.madapp.cc', port=443, secret='dd4482437e89f9af8929514eee7faaf61f'),
    ProxyConfig(type='mtproto', host='90.156.213.122', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='92.118.234.215', port=443, secret='dddb9b51b5ce9a2820ff62d348cb23f1b9'),
    ProxyConfig(type='mtproto', host='94.228.210.66', port=443, secret='7gr2vS88j7CxwUq8weBpfTd2ay5ydQ'),
    ProxyConfig(type='mtproto', host='7b5b3.blancproxy.link', port=443, secret='dd97c420070f5eb3ba3c22b9635576d9f1'),
    ProxyConfig(type='mtproto', host='cccp.jobinvest.ru', port=443, secret='7iCEx+WNghMpajIG2nA1bIFqb2JpbnZlc3QucnU'),
    ProxyConfig(type='mtproto', host='quackton.life', port=443, secret='7mX8dVOh9cqLULccAVs4ciR5YW5kZXgucnU'),
    ProxyConfig(type='mtproto', host='tg.vpnspacev.com', port=443, secret='bc184fc14b62b9b1dc5f34edf9476421'),
    ProxyConfig(type='mtproto', host='mt.flashgatevpn.ru', port=1337, secret='7p5K6Y/F/MbsztYRq2V/nBx3d3cuZ29vZ2xlLmNvbQ'),
    ProxyConfig(type='mtproto', host='mttg.2nevo4hosts.pro', port=8443, secret='7i/ak0PTd7FciYy9i4BOPPVtdHRnLjJuZXZvNGhvc3RzLnBybw'),
    ProxyConfig(type='mtproto', host='83.166.254.255', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='83.166.254.200', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='83.166.253.198', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='83.166.254.85', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='83.166.254.78', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='83.166.254.26', port=443, secret='dd6b3fb02424dbac55fef2da67c8c949'),
    ProxyConfig(type='mtproto', host='tg.pepewtf.top', port=443, secret='7ss8Rzx7vqAIVQA6txVmjFB0Zy5wZXBld3RmLnRvcA'),
    ProxyConfig(type='mtproto', host='telegram-proxy-v2.sssrvpn.pro', port=443, secret='2084c7e58d8213296a3206da70356c81'),
    ProxyConfig(type='mtproto', host='213.165.58.172', port=443, secret='ea2b5e9e884893637299f4053fc9aa30'),
    ProxyConfig(type='mtproto', host='213.176.77.46', port=443, secret='e2e672f206391b49befaa68252f750bc'),
    ProxyConfig(type='mtproto', host='84.201.175.61', port=443, secret='eead24ce88888cf6231e908ca911628e'),
    ProxyConfig(type='mtproto', host='telegram-proxy.sssrvpn.pro', port=443, secret='2084c7e58d8213296a3206da70356c81'),
    # SOCKS5 proxies
    ProxyConfig(type='socks5', host='109.120.189.122', port=1080, username='tgproxy', password='VKRecaXEinjq3M9U'),
    ProxyConfig(type='socks5', host='109.120.191.248', port=1080, username='tgproxy', password='xVGavfDim6nxSvby'),
    ProxyConfig(type='socks5', host='nether.kosmojoy.ru', port=48557, username='vpn_kosmo_bot', password='vpn_kosmo_bot'),
]


class ProxyManager:
    """Manages proxy selection and health checking for Telegram connections"""

    def __init__(self, proxies: list[ProxyConfig] = None):
        self.proxies = proxies or EMBEDDED_PROXIES.copy()
        self._current_proxy: Optional[ProxyConfig] = None
        self._check_lock = asyncio.Lock()
        self._last_full_check: float = 0
        self._check_interval = 300  # 5 minutes between full checks

    @property
    def current_proxy(self) -> Optional[ProxyConfig]:
        return self._current_proxy

    async def check_proxy(self, proxy: ProxyConfig, timeout: float = 5.0) -> bool:
        """
        Check if a proxy is working by attempting a TCP connection.
        Returns True if proxy responds, False otherwise.
        """
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

    def get_telethon_proxy_args(self, proxy: ProxyConfig) -> dict:
        """
        Convert ProxyConfig to Telethon client arguments.
        Returns dict with 'proxy' and optionally 'connection' keys.
        """
        if proxy.type == 'mtproto':
            from telethon.network import connection
            return {
                'connection': connection.ConnectionTcpMTProxyRandomizedIntermediate,
                'proxy': (proxy.host, proxy.port, proxy.secret)
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
