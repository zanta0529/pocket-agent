"""DNS 解析轉接層（Adapter Pattern：dnspython 延遲載入與 socket 安全降級）。"""

import asyncio
import importlib
import socket
import time
from abc import ABC, abstractmethod
from typing import Any

from mcp_servers._shared import CONSTANTS
from mcp_servers.netdiag.models import DnsRecordItem, DnsStageData, ResolverQueryResult

ALLOWED_DNS_RECORD_TYPES = frozenset({"A", "AAAA", "CNAME", "NS", "SOA", "MX", "TXT", "CAA", "HTTPS", "SRV"})


class DnsResolverAdapter(ABC):
    """DNS 解析轉接器抽象介面。"""

    @abstractmethod
    async def resolve_host_ips(self, hostname: str, timeout_sec: float) -> DnsStageData:
        """解析主機名稱之 A／AAAA 位址與 CNAME 鏈。"""

    @abstractmethod
    async def query_records(
        self,
        domain: str,
        record_types: tuple[str, ...],
        nameserver: str | None,
        timeout_sec: float,
    ) -> ResolverQueryResult:
        """對指定解析器查詢多筆 DNS 紀錄型別。"""


class DnspythonResolverAdapter(DnsResolverAdapter):
    """使用 dnspython 進行完整紀錄、TTL、CNAME 鏈與自訂解析器查詢。"""

    def __init__(self, dns_resolver_mod: Any, dns_exception_mod: Any) -> None:
        self._resolver_mod = dns_resolver_mod
        self._exception_mod = dns_exception_mod

    def _build_resolver(self, nameserver: str | None, timeout_sec: float) -> Any:
        res = self._resolver_mod.Resolver(configure=(nameserver is None))
        if nameserver is not None:
            res.nameservers = [nameserver]
        res.lifetime = timeout_sec
        res.timeout = min(timeout_sec, 5.0)
        return res

    def _resolve_sync(self, hostname: str, timeout_sec: float) -> DnsStageData:
        start = time.perf_counter()
        resolver = self._build_resolver(None, timeout_sec)
        cname_chain: list[str] = []
        current = hostname
        seen: set[str] = {hostname.lower().rstrip(".")}
        for _ in range(CONSTANTS.netdiag_dns_max_cname_hops):
            try:
                ans = resolver.resolve(current, "CNAME")
                target = str(ans[0].target).rstrip(".")
                cname_chain.append(target)
                norm = target.lower()
                if norm in seen:
                    break
                seen.add(norm)
                current = target
            except self._exception_mod.DNSException:
                break

        ips: list[str] = []
        ttl_val: int | None = None
        last_err = ""
        for rtype in ("A", "AAAA"):
            try:
                ans = resolver.resolve(current, rtype)
                if ttl_val is None and ans.rrset is not None:
                    ttl_val = int(ans.rrset.ttl)
                for rdata in ans:
                    addr = str(rdata)
                    if addr not in ips:
                        ips.append(addr)
            except (self._resolver_mod.NoAnswer, self._resolver_mod.NXDOMAIN) as exc:
                last_err = type(exc).__name__
            except self._exception_mod.DNSException as exc:
                last_err = f"{type(exc).__name__}: {exc}"

        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        if not ips:
            return DnsStageData(
                status="failed",
                cname_chain=tuple(cname_chain),
                latency_ms=elapsed_ms,
                error=last_err or "No A/AAAA records found",
            )
        return DnsStageData(
            status="ok",
            ips=tuple(ips),
            pinned_ip=ips[0],
            cname_chain=tuple(cname_chain),
            ttl=ttl_val,
            latency_ms=elapsed_ms,
        )

    def _query_records_sync(
        self,
        domain: str,
        record_types: tuple[str, ...],
        nameserver: str | None,
        timeout_sec: float,
    ) -> ResolverQueryResult:
        start = time.perf_counter()
        label = nameserver if nameserver else "system_default"
        try:
            resolver = self._build_resolver(nameserver, timeout_sec)
        except self._exception_mod.DNSException as exc:
            return ResolverQueryResult(
                resolver_label=label,
                status="failed",
                latency_ms=round((time.perf_counter() - start) * 1000.0, 2),
                error=f"{type(exc).__name__}: {exc}",
            )

        records: list[DnsRecordItem] = []
        cname_chain: list[str] = []
        errors: list[str] = []

        for rtype in record_types:
            try:
                ans = resolver.resolve(domain, rtype)
                ttl = int(ans.rrset.ttl) if ans.rrset is not None else None
                for rdata in ans:
                    val = rdata.to_text() if hasattr(rdata, "to_text") else str(rdata)
                    records.append(DnsRecordItem(record_type=rtype, ttl=ttl, value=val))
                    if rtype == "CNAME":
                        cname_chain.append(val.rstrip("."))
            except self._resolver_mod.NoAnswer:
                continue
            except self._resolver_mod.NXDOMAIN:
                errors.append(f"{rtype}: NXDOMAIN")
            except self._exception_mod.DNSException as exc:
                errors.append(f"{rtype}: {type(exc).__name__}")

        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        status = "ok"
        if errors and not records:
            status = "failed"
        elif errors:
            status = "partial"
        return ResolverQueryResult(
            resolver_label=label,
            status=status,
            records=tuple(records),
            cname_chain=tuple(cname_chain),
            latency_ms=elapsed_ms,
            error="; ".join(errors),
        )

    async def resolve_host_ips(self, hostname: str, timeout_sec: float) -> DnsStageData:
        return await asyncio.to_thread(self._resolve_sync, hostname, timeout_sec)

    async def query_records(
        self,
        domain: str,
        record_types: tuple[str, ...],
        nameserver: str | None,
        timeout_sec: float,
    ) -> ResolverQueryResult:
        return await asyncio.to_thread(self._query_records_sync, domain, record_types, nameserver, timeout_sec)


class SocketFallbackResolverAdapter(DnsResolverAdapter):
    """未安裝 dnspython 時之標準庫 socket 降級轉接器（僅支援系統 A／AAAA）。"""

    async def resolve_host_ips(self, hostname: str, timeout_sec: float) -> DnsStageData:
        start = time.perf_counter()
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP),
                timeout=timeout_sec,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            return DnsStageData(
                status="failed",
                latency_ms=round((time.perf_counter() - start) * 1000.0, 2),
                fallback_used=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        ips: list[str] = []
        for info in infos:
            addr = str(info[4][0]).split("%", 1)[0]
            if addr not in ips:
                ips.append(addr)
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        if not ips:
            return DnsStageData(status="failed", latency_ms=elapsed_ms, fallback_used=True, error="No DNS results")
        return DnsStageData(
            status="ok",
            ips=tuple(ips),
            pinned_ip=ips[0],
            latency_ms=elapsed_ms,
            fallback_used=True,
        )

    async def query_records(
        self,
        domain: str,
        record_types: tuple[str, ...],
        nameserver: str | None,
        timeout_sec: float,
    ) -> ResolverQueryResult:
        if nameserver is not None:
            return ResolverQueryResult(
                resolver_label=nameserver,
                status="failed",
                error="dnspython required for custom nameserver queries",
            )
        res = await self.resolve_host_ips(domain, timeout_sec)
        if res.status != "ok":
            return ResolverQueryResult(
                resolver_label="system_default",
                status="failed",
                latency_ms=res.latency_ms,
                error=res.error,
            )
        items: list[DnsRecordItem] = []
        for ip in res.ips:
            rtype = "AAAA" if ":" in ip else "A"
            if rtype in record_types:
                items.append(DnsRecordItem(record_type=rtype, ttl=None, value=ip))
        return ResolverQueryResult(
            resolver_label="system_default",
            status="ok",
            records=tuple(items),
            latency_ms=res.latency_ms,
        )


def create_dns_adapter() -> DnsResolverAdapter:
    """延遲載入 dnspython；缺少套件時降級至標準庫 socket。"""
    try:
        dns_resolver = importlib.import_module("dns.resolver")
        dns_exception = importlib.import_module("dns.exception")
        return DnspythonResolverAdapter(dns_resolver, dns_exception)
    except ImportError:
        return SocketFallbackResolverAdapter()
