"""Collect desktop system specs for model debugging.

Returns a single dict with sections: os, cpu, mem, disk, gpu, processes, net.
Best-effort: any failing section returns an error string instead of raising.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
import subprocess
import time

import httpx
import psutil

log = logging.getLogger("deep-dive.system_info")


def _bytes_gb(n: int) -> float:
    return round(n / (1024**3), 2)


def _os_section() -> dict:
    try:
        boot = psutil.boot_time()
        uptime_s = int(time.time() - boot)
        days, rem = divmod(uptime_s, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        return {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "uptime": f"{days}d {hours}h {mins}m",
        }
    except Exception as e:
        return {"error": str(e)}


def _cpu_section() -> dict:
    try:
        # 0.5s sample for current load
        load = psutil.cpu_percent(interval=0.5)
        freq = psutil.cpu_freq()
        return {
            "model": platform.processor() or "unknown",
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "freq_mhz": int(freq.current) if freq else None,
            "load_pct": load,
        }
    except Exception as e:
        return {"error": str(e)}


def _mem_section() -> dict:
    try:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {
            "total_gb": _bytes_gb(vm.total),
            "used_gb": _bytes_gb(vm.used),
            "available_gb": _bytes_gb(vm.available),
            "used_pct": vm.percent,
            "swap_total_gb": _bytes_gb(sw.total),
            "swap_used_gb": _bytes_gb(sw.used),
        }
    except Exception as e:
        return {"error": str(e)}


def _disk_section() -> dict:
    try:
        out = []
        for part in psutil.disk_partitions(all=False):
            # Skip CD/DVD, removable not mounted
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
                out.append({
                    "mount": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": _bytes_gb(u.total),
                    "used_gb": _bytes_gb(u.used),
                    "free_gb": _bytes_gb(u.free),
                    "used_pct": u.percent,
                })
            except (PermissionError, OSError):
                continue
        return {"drives": out}
    except Exception as e:
        return {"error": str(e)}


def _gpu_section() -> dict:
    """Try nvidia-smi. Falls back to wmic on Windows for non-NVIDIA."""
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            gpus = []
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    gpus.append({
                        "name": parts[0],
                        "driver": parts[1],
                        "vram_total_mb": int(parts[2]),
                        "vram_used_mb": int(parts[3]),
                        "util_pct": int(parts[4]),
                    })
            # Also list processes using GPU memory
            r2 = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,process_name,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            procs = []
            if r2.returncode == 0 and r2.stdout.strip():
                for line in r2.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        procs.append({
                            "pid": int(parts[0]),
                            "name": parts[1],
                            "vram_mb": int(parts[2]),
                        })
            return {"gpus": gpus, "processes": procs}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as e:
        log.warning("nvidia-smi failed: %s", e)

    # Windows fallback: wmic for any GPU name
    if platform.system() == "Windows":
        try:
            r = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name,AdapterRAM", "/format:csv"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                lines = [l.strip() for l in r.stdout.splitlines() if l.strip() and "," in l]
                gpus = []
                for line in lines[1:]:  # skip header
                    parts = line.split(",")
                    if len(parts) >= 3:
                        try:
                            vram_mb = int(parts[1]) // (1024 * 1024) if parts[1].isdigit() else None
                        except Exception:
                            vram_mb = None
                        gpus.append({"name": parts[2], "vram_total_mb": vram_mb})
                return {"gpus": gpus, "note": "no nvidia-smi — limited info"}
        except Exception as e:
            return {"error": f"no gpu info: {e}"}

    return {"note": "no nvidia-smi available"}


def _processes_section() -> dict:
    try:
        # First call to cpu_percent primes the counter; need a sample interval
        procs = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                p.cpu_percent(None)  # prime
                procs.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        time.sleep(0.5)

        rows = []
        for p in procs:
            try:
                with p.oneshot():
                    rows.append({
                        "pid": p.pid,
                        "name": p.info["name"] or "?",
                        "cpu_pct": p.cpu_percent(None),
                        "mem_mb": round(p.memory_info().rss / (1024**2), 1),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        top_cpu = sorted(rows, key=lambda r: r["cpu_pct"], reverse=True)[:10]
        top_mem = sorted(rows, key=lambda r: r["mem_mb"], reverse=True)[:10]
        return {
            "total": len(rows),
            "top_cpu": top_cpu,
            "top_mem": top_mem,
        }
    except Exception as e:
        return {"error": str(e)}


async def _net_section() -> dict:
    """Quick reachability check: gateway, DNS, public IP."""
    out: dict = {}

    # Default gateway
    try:
        gateways = psutil.net_if_stats()
        # psutil doesn't expose default gateway directly — parse `ipconfig` on Windows
        if platform.system() == "Windows":
            r = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=3
            )
            gw = None
            for line in r.stdout.splitlines():
                if "Default Gateway" in line and ":" in line:
                    val = line.split(":", 1)[1].strip()
                    if val and val != "":
                        gw = val
                        break
            out["gateway"] = gw or "unknown"
            if gw:
                # ping once with 1s timeout
                pr = subprocess.run(
                    ["ping", "-n", "1", "-w", "1000", gw],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                out["gateway_reachable"] = pr.returncode == 0
    except Exception as e:
        out["gateway_error"] = str(e)

    # DNS
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, socket.gethostbyname, "google.com")
        out["dns_ok"] = True
    except Exception as e:
        out["dns_ok"] = False
        out["dns_error"] = str(e)

    # Public IP
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://api.ipify.org")
            out["public_ip"] = r.text.strip() if r.status_code == 200 else "unavailable"
    except Exception as e:
        out["public_ip"] = "unavailable"
        out["public_ip_error"] = str(e)

    return out


async def collect_system_info() -> dict:
    """Gather all sections. Net section is async; rest run in executor."""
    loop = asyncio.get_event_loop()
    os_t = loop.run_in_executor(None, _os_section)
    cpu_t = loop.run_in_executor(None, _cpu_section)
    mem_t = loop.run_in_executor(None, _mem_section)
    disk_t = loop.run_in_executor(None, _disk_section)
    gpu_t = loop.run_in_executor(None, _gpu_section)
    proc_t = loop.run_in_executor(None, _processes_section)
    net_t = _net_section()

    os_r, cpu_r, mem_r, disk_r, gpu_r, proc_r, net_r = await asyncio.gather(
        os_t, cpu_t, mem_t, disk_t, gpu_t, proc_t, net_t
    )

    return {
        "status": "ok",
        "os": os_r,
        "cpu": cpu_r,
        "mem": mem_r,
        "disk": disk_r,
        "gpu": gpu_r,
        "processes": proc_r,
        "net": net_r,
    }
