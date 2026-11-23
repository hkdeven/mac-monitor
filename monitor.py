#!/usr/bin/env python3
"""mac-monitor: a small local macOS machine monitor.

One process: a background sampler thread writing to SQLite, plus an HTTP
server on 127.0.0.1 serving a dashboard and JSON endpoints.
Standard library only. No sudo.

    python3 monitor.py serve      start sampler + dashboard (what ./run does)
    python3 monitor.py snapshot   print a one-shot text report (./snapshot)
"""

import ctypes
import ctypes.util
import json
import os
import re
import resource
import socket
import sqlite3
import subprocess
import sys
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ----------------------------------------------------------------------------
# Tunables. Event thresholds live here.
# ----------------------------------------------------------------------------
LOAD_WARN_X_NCPU = 1.0         # 1-minute load above this many times the core count: warning
LOAD_CRITICAL_X_NCPU = 2.0     # ...above this many times the core count: critical
LOAD_SUSTAIN_S = 60            # ...once it has stayed above the line this long
LOAD_CLEAR_X_NCPU = 0.9        # the load event closes when load falls below this (hysteresis)
MEM_PRESSURE_WARN_LEVEL = 2    # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
MEM_PRESSURE_CRITICAL_LEVEL = 4
SWAP_GROWTH_MB = 256           # swap used grew by at least this much...
SWAP_GROWTH_WINDOW_S = 60      # ...within this window
DISK_FREE_MIN_PCT = 10.0       # a volume with less free space than this
CPU_SPEED_LIMIT_MIN = 100      # pmset CPU_Speed_Limit below this means throttling
EVENT_CLEAR_S = 60             # condition must stay clear this long before an event closes

# Internet checks
NET_PROBE_INTERVAL_S = 10      # ping the router and both public targets, and query DNS, this often
NET_TARGETS = [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8")]   # two independent public IPs
NET_PING_COUNT = 4             # pings per target per round
NET_DNS_NAME = "www.apple.com"                                     # name looked up each round
NET_HTTP_URL = "https://captive.apple.com/hotspot-detect.html"     # Apple's connectivity check page
NET_HTTP_INTERVAL_S = 60       # end-to-end HTTPS check this often
NET_HTTP_TIMEOUT_S = 8
NET_DOWN_SUSTAIN_S = 20        # both public targets unreachable this long: "Internet down" event
NET_WINDOW_S = 60              # loss, latency, jitter and DNS are judged over this window
NET_LOSS_PCT = 10.0            # packet loss over the window that opens an event...
NET_LOSS_CLEAR_PCT = 3.0       # ...and the level it must fall back under
NET_LATENCY_MS = 150.0         # median latency to the better public target
NET_LATENCY_CLEAR_MS = 100.0
NET_JITTER_MS = 50.0           # median ping stddev
NET_JITTER_CLEAR_MS = 30.0
NET_DNS_SLOW_MS = 500.0        # median lookup time while pings are fine
NET_DNS_SLOW_CLEAR_MS = 300.0
NET_DNS_FAIL_PCT = 50          # share of lookups failing over the window while pings are fine
NET_CLEAR_S = 30               # a network event closes after this long back to normal

# macOS notifications (a banner when an event opens)
NOTIFY_ENABLED = True          # master switch; the dashboard can also mute them
NOTIFY_RULES = {               # per rule
    "load_high": True, "net_down": True, "net_loss": True, "net_slow": True, "net_dns": True,
    "mem_pressure": True, "swap_growing": True, "disk_low": True, "cpu_throttle": True,
}
NOTIFY_COOLDOWN_S = 600        # a rule that flaps open and closed posts at most once in this long
NOTIFY_RECOVERY_KINDS = ("net_down",)   # these also post once when they end
NOTIFY_TIMEOUT_S = 10          # give up on a notification that takes longer than this to post
OSASCRIPT = "/usr/bin/osascript"

SAMPLE_INTERVAL_S = 5          # fast metrics: cpu, memory, processes, disk io, network
SLOW_INTERVAL_S = 30           # slow metrics: df, pmset -g therm
RETENTION_DAYS = 0             # 0 keeps everything; a number prunes rows older than that many days
TOP_N = 10                     # processes kept per sample, by cpu and by memory
PORT_RANGE = (5180, 5199)      # first free port is used unless MAC_MONITOR_PORT is set

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MAC_MONITOR_DB") or os.path.join(HERE, "data", "monitor.db")

# range name -> (seconds, bucket seconds). Buckets keep every range near 360 points.
RANGES = {
    "15m": (900, 5),
    "1h": (3600, 10),
    "6h": (21600, 60),
    "24h": (86400, 240),
    "7d": (604800, 1800),
    "30d": (2592000, 7200),
    "1y": (31536000, 86400),
    "all": (None, None),           # from the first sample to now
}

PRESSURE_NAMES = {1: "normal", 2: "warn", 4: "critical"}


def run_cmd(args, timeout=10):
    try:
        out = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=timeout)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# Native readers (ctypes). These replace a subprocess per sample for the
# metrics that have a cheap kernel call; each has a command-line fallback.
# ----------------------------------------------------------------------------
class Native:
    def __init__(self):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.host = self.libc.mach_host_self()
        self.task = ctypes.c_uint.in_dll(self.libc, "mach_task_self_").value
        self.libc.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
        self.libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_size_t),
                                           ctypes.c_void_p, ctypes.c_size_t]
        self._iokit = None
        try:
            self._init_iokit()
        except Exception:
            self._iokit = None

    # -- per-core CPU ticks: host_processor_info(PROCESSOR_CPU_LOAD_INFO) ----
    def cpu_ticks(self):
        """Returns [(user, system, idle, nice), ...] cumulative ticks per core."""
        ncpu = ctypes.c_uint()
        info = ctypes.POINTER(ctypes.c_uint)()
        count = ctypes.c_uint()
        kr = self.libc.host_processor_info(self.host, 2, ctypes.byref(ncpu),
                                           ctypes.byref(info), ctypes.byref(count))
        if kr != 0:
            raise OSError("host_processor_info failed: %d" % kr)
        try:
            return [(info[i * 4], info[i * 4 + 1], info[i * 4 + 2], info[i * 4 + 3])
                    for i in range(ncpu.value)]
        finally:
            self.libc.vm_deallocate(self.task, ctypes.cast(info, ctypes.c_void_p),
                                    count.value * 4)

    # -- sysctl ---------------------------------------------------------------
    def sysctl_raw(self, name, size):
        buf = ctypes.create_string_buffer(size)
        sz = ctypes.c_size_t(size)
        if self.libc.sysctlbyname(name.encode(), buf, ctypes.byref(sz), None, 0) != 0:
            raise OSError("sysctl %s failed" % name)
        return buf.raw[:sz.value]

    def sysctl_int(self, name):
        raw = self.sysctl_raw(name, 8)
        return int.from_bytes(raw, "little", signed=True)

    def sysctl_str(self, name):
        return self.sysctl_raw(name, 256).split(b"\0", 1)[0].decode("utf-8", "replace")

    def swap_usage(self):
        """vm.swapusage is struct xsw_usage {u64 total, avail, used; ...}."""
        raw = self.sysctl_raw("vm.swapusage", 64)
        total = int.from_bytes(raw[0:8], "little")
        used = int.from_bytes(raw[16:24], "little")
        return total, used

    # -- disk byte counters: IOBlockStorageDriver "Statistics" ---------------
    def _init_iokit(self):
        io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        vp, u32 = ctypes.c_void_p, ctypes.c_uint
        io.IOServiceMatching.restype = vp
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceGetMatchingServices.argtypes = [u32, vp, ctypes.POINTER(u32)]
        io.IOIteratorNext.restype = u32
        io.IOIteratorNext.argtypes = [u32]
        io.IOObjectRelease.argtypes = [u32]
        io.IORegistryEntryCreateCFProperty.restype = vp
        io.IORegistryEntryCreateCFProperty.argtypes = [u32, vp, vp, u32]
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, u32]
        cf.CFDictionaryGetValue.restype = vp
        cf.CFDictionaryGetValue.argtypes = [vp, vp]
        cf.CFNumberGetValue.argtypes = [vp, ctypes.c_long, vp]
        cf.CFRelease.argtypes = [vp]
        mk = lambda s: cf.CFStringCreateWithCString(None, s, 0x08000100)
        self._k_stats, self._k_read, self._k_write = (
            mk(b"Statistics"), mk(b"Bytes (Read)"), mk(b"Bytes (Write)"))
        self._iokit, self._cf = io, cf

    def disk_bytes(self):
        """Returns (bytes_read, bytes_written) summed over all block devices."""
        if not self._iokit:
            return disk_bytes_cmd()
        io, cf = self._iokit, self._cf
        it = ctypes.c_uint()
        if io.IOServiceGetMatchingServices(0, io.IOServiceMatching(b"IOBlockStorageDriver"),
                                           ctypes.byref(it)) != 0:
            return disk_bytes_cmd()
        rd = wr = 0
        val = ctypes.c_int64()
        try:
            while True:
                entry = io.IOIteratorNext(it.value)
                if not entry:
                    break
                stats = io.IORegistryEntryCreateCFProperty(entry, self._k_stats, None, 0)
                if stats:
                    for key, is_read in ((self._k_read, True), (self._k_write, False)):
                        num = cf.CFDictionaryGetValue(stats, key)
                        if num and cf.CFNumberGetValue(num, 4, ctypes.byref(val)):
                            if is_read:
                                rd += val.value
                            else:
                                wr += val.value
                    cf.CFRelease(stats)
                io.IOObjectRelease(entry)
        finally:
            io.IOObjectRelease(it.value)
        return rd, wr


def disk_bytes_cmd():
    out = run_cmd(["ioreg", "-c", "IOBlockStorageDriver", "-r", "-w0", "-k", "Statistics"])
    rd = sum(int(x) for x in re.findall(r'"Bytes \(Read\)"=(\d+)', out))
    wr = sum(int(x) for x in re.findall(r'"Bytes \(Write\)"=(\d+)', out))
    return rd, wr


# ----------------------------------------------------------------------------
# Parsers for command output (formats checked against this machine, Darwin 27)
# ----------------------------------------------------------------------------
def parse_vm_stat(out):
    """vm_stat -> (page_size, {label: pages})."""
    page = 16384
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        page = int(m.group(1))
    vals = {}
    for line in out.splitlines()[1:]:
        if ":" not in line:
            continue
        k, v = line.rsplit(":", 1)
        v = v.strip().rstrip(".")
        if v.isdigit():
            vals[k.strip().strip('"')] = int(v)
    return page, vals


def parse_cputime(s):
    """ps TIME column: [[D-]H:]M:SS.cc -> seconds."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    total = 0.0
    for part in s.split(":"):
        total = total * 60 + float(part)
    return total + days * 86400


def parse_ps(out):
    """ps -Ao pid,pcpu,rss,time,ucomm -> [(pid, pcpu, rss_kb, cpu_seconds, name)]."""
    rows = []
    for line in out.splitlines()[1:]:
        f = line.split(None, 4)
        if len(f) < 5:
            continue
        try:
            rows.append((int(f[0]), float(f[1]), int(f[2]), parse_cputime(f[3]), f[4].strip()))
        except ValueError:
            continue
    return rows


def parse_netstat(out):
    """netstat -ibn -> {iface: (ibytes, obytes)} from the <Link#> rows."""
    res = {}
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) < 10 or not f[2].startswith("<Link#"):
            continue
        try:
            res[f[0].rstrip("*")] = (int(f[-5]), int(f[-2]))
        except ValueError:
            continue
    return res


_DF_RE = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(\d+)\s+(\d+)\s+(\S+)\s+(/.*)$")


def parse_df(out):
    """df -k -> user-relevant volumes: / plus anything under /Volumes."""
    vols = []
    for line in out.splitlines()[1:]:
        m = _DF_RE.match(line)
        if not m:
            continue
        mount = m.group(9)
        total, avail = int(m.group(2)) * 1024, int(m.group(4)) * 1024
        if total <= 0:
            continue
        if mount != "/" and not (mount.startswith("/Volumes/") and mount != "/Volumes/Recovery"):
            continue
        vols.append({"mount": mount, "fs": m.group(1), "total": total, "free": avail,
                     "free_pct": round(avail * 100.0 / total, 2)})
    return vols


def parse_therm(out):
    """pmset -g therm -> {'speed_limit': int|None, 'values': {...}, 'notes': [...]}.

    On Apple Silicon pmset usually prints only "No ... has been recorded" notes;
    then speed_limit is None (not reported), which is not the same as 100.
    """
    values = {}
    for k, v in re.findall(r"^\s*(\w+)\s*=\s*(-?\d+)", out, re.M):
        values[k] = int(v)
    notes = [l.replace("Note:", "").strip() for l in out.splitlines() if l.startswith("Note:")]
    state = thermal_state()
    if state is not None:
        values["state"] = state
    return {"speed_limit": values.get("CPU_Speed_Limit"), "state": state,
            "state_name": THERMAL_STATES.get(state), "values": values, "notes": notes}


THERMAL_STATES = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}
THERMAL_STATE_WARN = 2         # NSProcessInfo.thermalState at or above this means macOS is throttling
_thermal_fn = None


def thermal_state():
    """NSProcessInfo.thermalState via the Objective-C runtime: 0 nominal, 1 fair,
    2 serious, 3 critical. Works on Apple Silicon, where pmset reports nothing.
    None when the call is not available."""
    global _thermal_fn
    try:
        if _thermal_fn is None:
            ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
            objc = ctypes.CDLL(ctypes.util.find_library("objc"))
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            proto = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
            proto_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
            addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
            info = proto(addr)(objc.objc_getClass(b"NSProcessInfo"),
                               objc.sel_registerName(b"processInfo"))
            sel = objc.sel_registerName(b"thermalState")
            send = proto_long(addr)
            _thermal_fn = lambda: send(info, sel)
        state = int(_thermal_fn())
        return state if state in THERMAL_STATES else None
    except Exception:
        _thermal_fn = False
        return None


# ----------------------------------------------------------------------------
# Collector: turns raw counters into one sample dict
# ----------------------------------------------------------------------------
class Collector:
    def __init__(self):
        self.native = None
        try:
            self.native = Native()
            self.native.cpu_ticks()
        except Exception:
            self.native = None
        self.ncpu = os.cpu_count() or 1
        self.mem_total = self._sysctl_int("hw.memsize")
        self.boot = self._boot_time()
        self.host = {
            "name": socket.gethostname(),
            "model": self._sysctl_str("machdep.cpu.brand_string"),
            "ncpu": self.ncpu,
            "mem_total": self.mem_total,
            "core_groups": self._core_groups(),
        }
        self.prev = None          # previous raw counters
        self.prev_proc = {}       # pid -> cpu seconds
        self.names = {}           # pid -> (short name, full name)
        self.last_top = None      # (top_cpu, top_mem, top_groups, nproc) of the last good ps
        self.slow = {"volumes": [], "thermal": parse_therm("")}
        self.slow_at = 0

    def _sysctl_int(self, name):
        try:
            return self.native.sysctl_int(name)
        except Exception:
            try:
                return int(run_cmd(["sysctl", "-n", name]).strip())
            except ValueError:
                return 0

    def _sysctl_str(self, name):
        try:
            return self.native.sysctl_str(name)
        except Exception:
            return run_cmd(["sysctl", "-n", name]).strip()

    def _boot_time(self):
        m = re.search(r"sec = (\d+)", run_cmd(["sysctl", "-n", "kern.boottime"]))
        return int(m.group(1)) if m else int(time.time())

    def _core_groups(self):
        """[{name, count}] in core-index order where the kernel reports it."""
        groups = []
        n = self._sysctl_int("hw.nperflevels")
        for i in range(n if 0 < n < 8 else 0):
            cnt = self._sysctl_int("hw.perflevel%d.logicalcpu" % i)
            if cnt > 0:
                groups.append({"name": self._sysctl_str("hw.perflevel%d.name" % i), "count": cnt})
        return groups

    def _swap(self):
        try:
            return self.native.swap_usage()
        except Exception:
            m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", run_cmd(["sysctl", "vm.swapusage"]))
            if not m:
                return 0, 0
            return int(float(m.group(1)) * 1048576), int(float(m.group(2)) * 1048576)

    def _full_names(self, short):
        """{pid: 16-char name} -> {pid: full name}, asking ps only about pids not seen before."""
        missing = [pid for pid, nm in short.items() if self.names.get(pid, (None,))[0] != nm]
        if missing:
            found = {}
            out = run_cmd(["ps", "-c", "-o", "pid=,comm=", "-p", ",".join(map(str, missing))])
            for line in out.splitlines():
                f = line.split(None, 1)
                if len(f) == 2 and f[0].isdigit():
                    found[int(f[0])] = f[1].strip()
            for pid in missing:   # a process that already exited keeps its short name
                self.names[pid] = (short[pid], found.get(pid) or short[pid])
        return {pid: self.names[pid][1] for pid in short}

    def _raw(self):
        now = time.time()
        raw = {"t": now}
        raw["ticks"] = self.native.cpu_ticks() if self.native else None
        # ucomm (the kernel's 16-char name) costs about half of comm, which makes ps
        # read every process's argument block; full names are looked up for the
        # few listed processes only (see _full_names)
        raw["ps"] = parse_ps(run_cmd(["ps", "-Ao", "pid,pcpu,rss,time,ucomm"]))
        raw["page"], raw["vm"] = parse_vm_stat(run_cmd(["vm_stat"]))
        raw["net"] = parse_netstat(run_cmd(["netstat", "-ibn"]))
        try:
            raw["disk"] = self.native.disk_bytes() if self.native else disk_bytes_cmd()
        except Exception:
            raw["disk"] = disk_bytes_cmd()
        raw["swap"] = self._swap()
        raw["pressure"] = self._sysctl_int("kern.memorystatus_vm_pressure_level")
        return raw

    def _slow(self, now):
        if now - self.slow_at >= SLOW_INTERVAL_S - 1:
            self.slow = {"volumes": parse_df(run_cmd(["df", "-k"])),
                         "thermal": parse_therm(run_cmd(["pmset", "-g", "therm"]))}
            self.slow_at = now

    def sample(self):
        """Take one sample. Rates are relative to the previous call (0 on the first)."""
        raw = self._raw()
        prev = self.prev
        self.prev = raw
        now = raw["t"]
        dt = max(now - prev["t"], 0.001) if prev else None
        self._slow(now)

        # ---- processes -----------------------------------------------------
        if not raw["ps"] and self.last_top:
            # ps timed out (machine badly overloaded): keep the last lists rather than none
            raw["ps_failed"] = True
        procs = []
        groups = {}               # name -> [count, cpu, rss_kb, busiest pid, its cpu]
        cur_proc = {}
        for pid, pcpu, rss_kb, cpusec, name in raw["ps"]:
            cur_proc[pid] = cpusec
            if dt and pid in self.prev_proc and cpusec >= self.prev_proc[pid]:
                cpu = (cpusec - self.prev_proc[pid]) / dt * 100.0
            else:
                cpu = pcpu  # first sighting: fall back to ps's own decayed figure
            procs.append((pid, name, round(cpu, 1), rss_kb))
            grp = groups.setdefault(name, [0, 0.0, 0, pid, -1.0])
            grp[0] += 1
            grp[1] += cpu
            grp[2] += rss_kb
            if cpu > grp[4]:
                grp[3], grp[4] = pid, cpu
        if not raw.get("ps_failed"):
            self.prev_proc = cur_proc
        if len(self.names) > 400:
            self.names = {p: v for p, v in self.names.items() if p in cur_proc}
        # same-named processes added together (30 node workers at 40% each never
        # top a per-process list, but together they are the answer)
        grows = [(n, g[0], round(g[1], 1), g[2]) for n, g in groups.items()]
        top_groups = sorted(grows, key=lambda g: -g[2])[:TOP_N]
        seen = {g[0] for g in top_groups}
        top_groups += [g for g in sorted(grows, key=lambda g: -g[3])[:TOP_N] if g[0] not in seen]
        top_cpu = sorted(procs, key=lambda p: -p[2])[:TOP_N]
        top_mem = sorted(procs, key=lambda p: -p[3])[:TOP_N]
        # full names for what is about to be stored
        short = {p[0]: p[1] for p in top_cpu + top_mem}
        short.update({groups[g[0]][3]: g[0] for g in top_groups})
        full = self._full_names(short)
        top_cpu = [(p[0], full[p[0]], p[2], p[3]) for p in top_cpu]
        top_mem = [(p[0], full[p[0]], p[2], p[3]) for p in top_mem]

        def group_name(nm):
            # groups are keyed by the kernel's 16-char name. A lone process gets its
            # full name back; a cut-off name shared by several is marked as cut off,
            # since its members may differ past the 16th character
            if len(nm) < 16:
                return nm
            fn = full[groups[nm][3]]
            return fn if groups[nm][0] == 1 and fn.startswith(nm) else nm + "\u2026"
        top_groups = [(group_name(g[0]),) + g[1:] for g in top_groups]

        nproc = len(procs)
        if raw.get("ps_failed"):
            top_cpu, top_mem, top_groups, nproc = self.last_top
        else:
            self.last_top = (top_cpu, top_mem, top_groups, nproc)

        # ---- cpu -------------------------------------------------------------
        per_core = []
        user = system = idle = 0.0
        if raw["ticks"] and prev and prev.get("ticks") and len(prev["ticks"]) == len(raw["ticks"]):
            tu = ts_ = ti = 0
            for a, b in zip(prev["ticks"], raw["ticks"]):
                du = ((b[0] - a[0]) & 0xFFFFFFFF) + ((b[3] - a[3]) & 0xFFFFFFFF)
                ds = (b[1] - a[1]) & 0xFFFFFFFF
                di = (b[2] - a[2]) & 0xFFFFFFFF
                tot = du + ds + di
                per_core.append(round((du + ds) * 100.0 / tot, 1) if tot else 0.0)
                tu, ts_, ti = tu + du, ts_ + ds, ti + di
            tot = tu + ts_ + ti
            if tot:
                user, system, idle = tu * 100.0 / tot, ts_ * 100.0 / tot, ti * 100.0 / tot
        elif not raw["ticks"]:
            # no Mach access: approximate from the process table (no user/system split)
            busy = min(100.0, sum(p[2] for p in procs) / self.ncpu)
            user, system, idle = busy, 0.0, 100.0 - busy
        else:
            idle = 100.0
        try:
            load = list(os.getloadavg())
        except OSError:
            load = [0.0, 0.0, 0.0]

        # ---- memory ----------------------------------------------------------
        vm, page = raw["vm"], raw["page"]
        g = lambda k: vm.get(k, 0) * page
        wired = g("Pages wired down")
        compressed = g("Pages occupied by compressor")
        app = max(0, g("Anonymous pages") - g("Pages purgeable"))
        free = g("Pages free") + g("Pages speculative")
        swap_in = swap_out = 0.0
        if prev:
            pv = prev["vm"]
            swap_in = max(0, vm.get("Swapins", 0) - pv.get("Swapins", 0)) * page / dt
            swap_out = max(0, vm.get("Swapouts", 0) - pv.get("Swapouts", 0)) * page / dt

        # ---- disk io / network -------------------------------------------------
        disk_r = disk_w = 0.0
        ifaces = {}
        net_in = net_out = 0.0
        if prev:
            disk_r = max(0, raw["disk"][0] - prev["disk"][0]) / dt
            disk_w = max(0, raw["disk"][1] - prev["disk"][1]) / dt
            for name, (ib, ob) in raw["net"].items():
                if name not in prev["net"] or (ib == 0 and ob == 0):
                    continue
                pi, po = prev["net"][name]
                ri, ro = max(0, ib - pi) / dt, max(0, ob - po) / dt
                ifaces[name] = {"in_bps": round(ri), "out_bps": round(ro)}
                if not name.startswith("lo"):
                    net_in += ri
                    net_out += ro

        level = raw["pressure"]
        return {
            "ts": int(round(now)),
            "cpu": {"user": round(user, 2), "sys": round(system, 2), "idle": round(idle, 2),
                    "busy": round(user + system, 2), "per_core": per_core,
                    "load": [round(x, 2) for x in load], "ncpu": self.ncpu},
            "mem": {"total": self.mem_total, "used": app + wired + compressed, "app": app,
                    "wired": wired, "compressed": compressed, "cached": g("File-backed pages"),
                    "free": free, "pressure_level": level,
                    "pressure": PRESSURE_NAMES.get(level, "unknown")},
            "swap": {"total": raw["swap"][0], "used": raw["swap"][1],
                     "in_bps": round(swap_in), "out_bps": round(swap_out)},
            "disk": {"volumes": self.slow["volumes"], "read_bps": round(disk_r),
                     "write_bps": round(disk_w)},
            "net": {"in_bps": round(net_in), "out_bps": round(net_out), "ifaces": ifaces},
            "thermal": self.slow["thermal"],
            "uptime_s": int(now - self.boot),
            "nproc": nproc,
            "ps_stale": bool(raw.get("ps_failed")),
            "top": {"cpu": top_cpu, "mem": top_mem, "groups": top_groups},
        }


def proc_dicts(rows):
    return [{"pid": r[0], "name": r[1], "cpu": r[2], "rss": r[3] * 1024} for r in rows]


def group_dicts(rows):
    return [{"name": r[0], "count": r[1], "cpu": r[2], "rss": r[3] * 1024} for r in rows]


def top_dicts(by_cpu, by_mem, by_name):
    """Stored JSON text (or None) -> the process lists every endpoint returns."""
    return {"by_cpu": proc_dicts(json.loads(by_cpu or "[]")),
            "by_mem": proc_dicts(json.loads(by_mem or "[]")),
            "by_name": group_dicts(json.loads(by_name or "[]"))}


# ----------------------------------------------------------------------------
# Internet probing: router, two public IPs, DNS, one HTTPS check, link info.
# Runs in its own thread so a dead line (every probe timing out) never delays
# the 5 s sampler; the sampler only collects finished results.
# ----------------------------------------------------------------------------
_PING_STATS = re.compile(r"(\d+) packets transmitted, (\d+) packets received")
_PING_RTT = re.compile(r"min/avg/max/stddev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")


def parse_ping(out, count):
    """Summary of `ping -q`. No statistics line at all (no route, bad address,
    killed) counts as every packet lost."""
    res = {"sent": count, "recv": 0, "loss": 100.0, "min": None, "avg": None, "max": None, "sd": None}
    m = _PING_STATS.search(out or "")
    if m and int(m.group(1)) > 0:
        res["sent"], res["recv"] = int(m.group(1)), int(m.group(2))
        res["loss"] = round(100.0 * (res["sent"] - res["recv"]) / res["sent"], 1)
    m = _PING_RTT.search(out or "")
    if m and res["recv"]:
        res["min"], res["avg"], res["max"], res["sd"] = (float(x) for x in m.groups())
    return res


def default_route():
    """(gateway ip, interface) of the default route, or (None, None)."""
    out = run_cmd(["route", "-n", "get", "default"], timeout=3)
    gw = re.search(r"^\s*gateway:\s*(\S+)", out, re.M)
    ifc = re.search(r"^\s*interface:\s*(\S+)", out, re.M)
    return (gw.group(1) if gw else None, ifc.group(1) if ifc else None)


def dns_servers():
    """Resolvers macOS is using now (it keeps /etc/resolv.conf current)."""
    try:
        with open("/etc/resolv.conf") as f:
            return [l.split()[1] for l in f if l.startswith("nameserver") and len(l.split()) > 1]
    except OSError:
        return []


def dns_probe(name, timeout=2.0):
    """Ask the configured resolvers for `name` directly over UDP, so the answer
    is never served from this Mac's cache. Tries each server in turn.
    Returns {"ok", "ms", "server", "error"}."""
    servers = dns_servers()[:2]
    if not servers:
        return {"ok": False, "ms": None, "server": None, "error": "no resolver configured"}
    qid = int.from_bytes(os.urandom(2), "big")
    try:
        qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.strip(".").split(".")) + b"\0"
    except (UnicodeError, ValueError):
        return {"ok": False, "ms": None, "server": None, "error": "bad name"}
    packet = qid.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"
    err = "timeout"
    for server in servers:
        fam = socket.AF_INET6 if ":" in server else socket.AF_INET
        t0 = time.monotonic()
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(packet, (server, 53))
                while True:
                    data, _ = sock.recvfrom(1500)
                    if len(data) >= 12 and data[:2] == packet[:2]:
                        break
            ms = round((time.monotonic() - t0) * 1000, 1)
            rcode, answers = data[3] & 0x0F, int.from_bytes(data[6:8], "big")
            if rcode == 0 and answers > 0:
                return {"ok": True, "ms": ms, "server": server, "error": None}
            return {"ok": False, "ms": ms, "server": server,
                    "error": "NXDOMAIN" if rcode == 3 else "rcode %d" % rcode if rcode else "empty answer"}
        except socket.timeout:
            err = "timeout"
        except OSError as e:
            err = e.strerror or "network error"
    return {"ok": False, "ms": None, "server": servers[0], "error": err}


class WifiInfo:
    """Signal, noise and tx rate from CoreWLAN through the Objective-C runtime.
    No sudo and no location permission needed for these (the network name does
    need it, so it is not read)."""

    def __init__(self):
        ctypes.CDLL("/System/Library/Frameworks/CoreWLAN.framework/CoreWLAN")
        self.objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self.objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        self.objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]

    def _msg(self, obj, sel, restype=ctypes.c_void_p):
        fn = ctypes.cast(self.objc.objc_msgSend,
                         ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p))
        return fn(obj, self.objc.sel_registerName(sel))

    def read(self):
        """{"iface", "rssi", "noise", "tx_rate", "channel", "band"} or None when Wi-Fi is off."""
        pool = self.objc.objc_autoreleasePoolPush()
        try:
            client = self._msg(self.objc.objc_getClass(b"CWWiFiClient"), b"sharedWiFiClient")
            ifc = self._msg(client, b"interface") if client else None
            if not ifc or not self._msg(ifc, b"powerOn", ctypes.c_bool):
                return None
            name = self._msg(ifc, b"interfaceName")
            name = ctypes.string_at(self._msg(name, b"UTF8String")).decode() if name else None
            rssi = self._msg(ifc, b"rssiValue", ctypes.c_long)
            chan = self._msg(ifc, b"wlanChannel")
            return {"iface": name, "rssi": rssi or None,
                    "noise": self._msg(ifc, b"noiseMeasurement", ctypes.c_long) or None,
                    "tx_rate": self._msg(ifc, b"transmitRate", ctypes.c_double) or None,
                    "channel": self._msg(chan, b"channelNumber", ctypes.c_long) if chan else None,
                    "band": {1: "2.4 GHz", 2: "5 GHz", 3: "6 GHz"}.get(
                        self._msg(chan, b"channelBand", ctypes.c_long)) if chan else None}
        finally:
            self.objc.objc_autoreleasePoolPop(pool)


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


SPEEDTEST_RUNNING = threading.Event()   # probes pause while a speed test saturates the line


class NetProbe(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="netprobe")
        self.lock = threading.Lock()
        self.pending = deque()        # finished cycles the sampler has not stored yet
        self.latest = None
        self.recent = deque()         # last NET_WINDOW_S of cycles, for the window figures
        self.http = None              # last finished HTTPS check
        self.http_reported = 0
        self.http_started = 0
        self.http_busy = False
        self.ports = {}
        try:
            self.wifi = WifiInfo()
            self.wifi.read()
        except Exception:
            self.wifi = None

    # -- test hook: MAC_MONITOR_NET_OVERRIDE names a JSON file re-read every cycle,
    #    {"targets": ["192.0.2.1", "192.0.2.2"], "gateway": "192.0.2.3", "dns_name": "x.invalid"}
    @staticmethod
    def overrides():
        path = os.environ.get("MAC_MONITOR_NET_OVERRIDE")
        if not path:
            return {}
        try:
            with open(path) as f:
                o = json.load(f)
            return o if isinstance(o, dict) else {}
        except (OSError, ValueError):
            return {}

    def link_info(self, iface):
        info = {"iface": iface, "type": None, "rssi": None, "noise": None, "tx_rate": None,
                "channel": None, "band": None}
        if not iface:
            return info
        wifi = None
        try:
            wifi = self.wifi.read() if self.wifi else None
        except Exception:
            wifi = None
        if wifi and wifi["iface"] == iface:
            info.update(wifi, type="Wi-Fi")
            return info
        if iface not in self.ports:
            out = run_cmd(["networksetup", "-listallhardwareports"], timeout=5)
            for port, dev in re.findall(r"Hardware Port: (.+)\nDevice: (\S+)", out):
                self.ports[dev] = port
            self.ports.setdefault(iface, "VPN or tunnel" if iface.startswith(("utun", "ipsec", "ppp")) else "Other")
        info["type"] = self.ports[iface]
        return info

    def _http_check(self):
        t0 = time.monotonic()
        res = {"ok": False, "ms": None, "error": None, "url": NET_HTTP_URL}
        try:
            import urllib.request
            req = urllib.request.Request(NET_HTTP_URL, headers={"User-Agent": "Mozilla/5.0 (connectivity check)"})
            with urllib.request.urlopen(req, timeout=NET_HTTP_TIMEOUT_S) as r:
                r.read(512)
                res["ok"] = r.status in (200, 204)
                res["error"] = None if res["ok"] else "HTTP %d" % r.status
            res["ms"] = round((time.monotonic() - t0) * 1000, 1)
        except Exception as e:
            res["error"] = (str(getattr(e, "reason", e)) or e.__class__.__name__)[:80]
        res["ts"] = int(time.time())
        with self.lock:
            self.http = res
            self.http_busy = False

    def cycle(self, wait_http=False):
        """One round of probes. Bounded: pings give up after 3 s, DNS after 2 s per server."""
        ov = self.overrides()
        now = time.time()
        gw, iface = default_route()
        if ov.get("gateway"):
            gw = ov["gateway"]
        targets = list(NET_TARGETS)
        for i, ip in enumerate(ov.get("targets") or []):
            if i < len(targets):
                targets[i] = (targets[i][0], str(ip))
        procs = []
        for label, ip in [("Router", gw)] + targets:
            p = None
            if ip:
                try:
                    p = subprocess.Popen(
                        ["/sbin/ping", "-n", "-q", "-c", str(NET_PING_COUNT), "-i", "0.25",
                         "-W", "1000", "-t", "3", ip],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                except OSError:
                    p = None
            procs.append((label, ip, p))
        if not self.http_busy and now - self.http_started >= NET_HTTP_INTERVAL_S:
            self.http_busy, self.http_started = True, now
            th = threading.Thread(target=self._http_check, daemon=True, name="httpcheck")
            th.start()
            if wait_http:
                th.join(NET_HTTP_TIMEOUT_S + 2)
        dns = dns_probe(ov.get("dns_name") or NET_DNS_NAME)
        dns["name"] = ov.get("dns_name") or NET_DNS_NAME
        pings = []
        for label, ip, p in procs:
            out = ""
            if p:
                try:
                    out = p.communicate(timeout=6)[0].decode("utf-8", "replace")
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.communicate()
            r = parse_ping(out, NET_PING_COUNT)
            r.update(name=label, ip=ip)
            pings.append(r)
        gwr, pub = pings[0], pings[1:]
        up = [t for t in pub if t["recv"] > 0]
        if up:
            state = "up"
            best = min(up, key=lambda t: t["avg"])
            lat, jit, loss = best["avg"], best["sd"], min(t["loss"] for t in pub)
        else:
            state = "down_isp" if gwr["recv"] > 0 else "down_local"
            lat = jit = None
            loss = 100.0
        res = {"ts": int(round(now)), "state": state, "gateway": gwr, "targets": pub,
               "latency_ms": lat, "jitter_ms": jit, "loss": loss, "dns": dns,
               "link": self.link_info(iface), "http_new": None}
        with self.lock:
            if self.http and self.http["ts"] != self.http_reported:
                res["http_new"] = self.http
                self.http_reported = self.http["ts"]
            res["http"] = self.http
            self.recent.append(res)
            while self.recent and self.recent[0]["ts"] < res["ts"] - NET_WINDOW_S:
                self.recent.popleft()
            ok = [r for r in self.recent if r["state"] == "up"]
            res["window"] = {
                "cycles": len(ok),
                "full": len(ok) >= max(3, NET_WINDOW_S // NET_PROBE_INTERVAL_S - 2),
                "loss": round(sum(r["loss"] for r in ok) / len(ok), 1) if ok else None,
                "latency_ms": median([r["latency_ms"] for r in ok]),
                "jitter_ms": median([r["jitter_ms"] for r in ok]),
                "dns_ms": median([r["dns"]["ms"] for r in ok if r["dns"]["ok"]]),
                "dns_fail_pct": round(100.0 * sum(1 for r in ok if not r["dns"]["ok"]) / len(ok)) if ok else None,
            }
            res["verdict"] = net_verdict(res)
            self.latest = res
        return res

    def drain(self):
        with self.lock:
            out = list(self.pending)
            self.pending.clear()
        return out

    def run(self):
        next_at = time.monotonic()
        while True:
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_at = max(next_at + NET_PROBE_INTERVAL_S, time.monotonic() + 2)
            if SPEEDTEST_RUNNING.is_set():
                continue      # its own traffic would read as loss and latency; leave a gap instead
            try:
                res = self.cycle()
                if SPEEDTEST_RUNNING.is_set():
                    continue
                with self.lock:
                    self.pending.append(res)
            except Exception as e:
                sys.stderr.write("netprobe error: %r\n" % (e,))


def net_problems(res):
    """Which degraded conditions hold on the window figures (not counting 'down').
    Returns {"loss": bool, "latency": bool, "jitter": bool, "dns_fail": bool, "dns_slow": bool}."""
    w = res["window"]
    full = w["full"] and res["state"] == "up"
    return {
        "loss": bool(full and w["loss"] is not None and w["loss"] >= NET_LOSS_PCT),
        "latency": bool(full and w["latency_ms"] is not None and w["latency_ms"] > NET_LATENCY_MS),
        "jitter": bool(full and w["jitter_ms"] is not None and w["jitter_ms"] > NET_JITTER_MS),
        "dns_fail": bool(full and w["dns_fail_pct"] is not None and w["dns_fail_pct"] >= NET_DNS_FAIL_PCT),
        "dns_slow": bool(full and w["dns_ms"] is not None and w["dns_ms"] > NET_DNS_SLOW_MS),
    }


def net_where(res):
    if res["state"] == "down_local":
        if not res["gateway"]["ip"]:
            return "no network connection on this Mac"
        return "router unreachable (Wi-Fi or local network)"
    return "router reachable, ISP side"


def net_verdict(res):
    """{"level": good|warning|critical, "text": plain verdict} for the tile and snapshot."""
    if res["state"] != "up":
        return {"level": "critical", "text": "Down - " + net_where(res)}
    w, p = res["window"], net_problems(res)
    lat = res["latency_ms"]
    if p["loss"]:
        return {"level": "warning", "text": "Dropping packets, %.0f%% loss" % w["loss"]}
    if p["latency"]:
        return {"level": "warning", "text": "Slow, %.0f ms" % w["latency_ms"]}
    if p["jitter"]:
        return {"level": "warning", "text": "Unsteady, jitter %.0f ms" % w["jitter_ms"]}
    if p["dns_fail"]:
        return {"level": "warning", "text": "DNS failing"}
    if p["dns_slow"]:
        return {"level": "warning", "text": "DNS slow, %.0f ms" % w["dns_ms"]}
    loss = w["loss"] if w["loss"] is not None else res["loss"]
    return {"level": "good", "text": "Good, %.0f ms, %.0f%% loss" % (lat, loss)}


def store_net(con, r):
    g, (a, b) = r["gateway"], r["targets"]
    h, d, l = r["http_new"], r["dns"], r["link"]
    con.execute(
        "INSERT OR REPLACE INTO net VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["ts"], r["state"], l["iface"], g["ip"], g["avg"], g["max"], g["loss"],
         a["avg"], a["max"], a["sd"], a["loss"], b["avg"], b["max"], b["sd"], b["loss"],
         r["latency_ms"], r["loss"], d["ms"] if d["ok"] else None, 1 if d["ok"] else 0,
         h["ms"] if h and h["ok"] else None, None if h is None else (1 if h["ok"] else 0),
         l["rssi"], l["noise"], l["tx_rate"]))


def net_summary(con, start, end):
    """Uptime, outages and latency over a window, from the raw probe rows.
    Aggregated in SQL so a 7-day window stays cheap."""
    n, down_rows, worst, loss, lat_n = con.execute(
        "SELECT COUNT(*), SUM(state != 'up'), MAX(lat_ms), AVG(CASE WHEN state = 'up' THEN loss END),"
        " COUNT(lat_ms) FROM net WHERE ts > ? AND ts <= ?", (start, end)).fetchone()
    if not n:
        return None
    med = None
    if lat_n:
        med = con.execute("SELECT lat_ms FROM net WHERE ts > ? AND ts <= ? AND lat_ms IS NOT NULL"
                          " ORDER BY lat_ms LIMIT 1 OFFSET ?", (start, end, lat_n // 2)).fetchone()[0]
    gap = NET_PROBE_INTERVAL_S * 1.5      # down rounds further apart than this are separate outages
    outages = []
    for (ts,) in con.execute("SELECT ts FROM net WHERE ts > ? AND ts <= ? AND state != 'up' ORDER BY ts",
                             (start, end)):
        if outages and ts - outages[-1][1] <= gap:
            outages[-1][1] = ts
        else:
            outages.append([ts, ts])
    lengths = [e - s + NET_PROBE_INTERVAL_S for s, e in outages]
    return {"probes": n, "uptime_pct": round(100.0 * (n - (down_rows or 0)) / n, 2),
            "outages": len(outages), "outage_s": sum(lengths),
            "longest_outage_s": max(lengths or [0]),
            "last_outage": outages[-1][0] if outages else None,
            "latency_median_ms": round(med, 1) if med is not None else None,
            "latency_worst_ms": round(worst, 1) if worst is not None else None,
            "loss_pct": round(loss, 2) if loss is not None else None}


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts INTEGER PRIMARY KEY,
  cpu_user REAL, cpu_sys REAL, cpu_idle REAL,
  load1 REAL, load5 REAL, load15 REAL, per_core TEXT,
  mem_used INTEGER, mem_app INTEGER, mem_wired INTEGER, mem_compressed INTEGER,
  mem_cached INTEGER, mem_free INTEGER, pressure INTEGER,
  swap_used INTEGER, swap_total INTEGER, swap_in REAL, swap_out REAL,
  disk_read REAL, disk_write REAL, volumes TEXT,
  net_in REAL, net_out REAL, ifaces TEXT,
  speed_limit INTEGER, thermal TEXT,
  uptime INTEGER, nproc INTEGER
);
CREATE TABLE IF NOT EXISTS procs (
  ts INTEGER PRIMARY KEY,
  by_cpu TEXT,   -- JSON [[pid, name, cpu_pct, rss_kb], ...]
  by_mem TEXT,
  by_name TEXT   -- JSON [[name, process_count, cpu_pct_sum, rss_kb_sum], ...]
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER, end_ts INTEGER,
  kind TEXT, severity TEXT, title TEXT,
  value REAL, peak REAL, unit TEXT,
  by_cpu TEXT, by_mem TEXT, by_name TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);   -- small persistent settings
CREATE TABLE IF NOT EXISTS net (
  ts INTEGER PRIMARY KEY, state TEXT,          -- up | down_isp | down_local
  iface TEXT, gw TEXT, gw_ms REAL, gw_max REAL, gw_loss REAL,
  a_ms REAL, a_max REAL, a_sd REAL, a_loss REAL,   -- first public target
  b_ms REAL, b_max REAL, b_sd REAL, b_loss REAL,   -- second public target
  lat_ms REAL, loss REAL,                      -- the better target's latency, the lower loss
  dns_ms REAL, dns_ok INTEGER,
  http_ms REAL, http_ok INTEGER,               -- NULL on rounds without an HTTPS check
  rssi INTEGER, noise INTEGER, tx_rate REAL
);
"""


def open_db(readonly=False):
    if readonly:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5)
    else:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def meta_get(key, default=None):
    """A persisted setting (JSON), readable from any thread."""
    if not os.path.exists(DB_PATH):
        return default
    try:
        # not read-only: at startup no other connection holds the WAL open yet, and
        # a read-only one cannot open a WAL database on its own
        con = open_db()
        try:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else default
    except (sqlite3.Error, ValueError):
        return default


def meta_set(key, value):
    con = open_db()
    try:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
        con.commit()
    finally:
        con.close()


def store_sample(con, s):
    j = lambda o: json.dumps(o, separators=(",", ":"))
    c, m, sw, d, n = s["cpu"], s["mem"], s["swap"], s["disk"], s["net"]
    # idle interfaces and pmset's fixed notes would only pad every row
    busy_ifaces = {k: v for k, v in n["ifaces"].items() if v["in_bps"] or v["out_bps"]}
    con.execute(
        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s["ts"], c["user"], c["sys"], c["idle"], c["load"][0], c["load"][1], c["load"][2],
         j(c["per_core"]), m["used"], m["app"], m["wired"], m["compressed"], m["cached"],
         m["free"], m["pressure_level"], sw["used"], sw["total"], sw["in_bps"], sw["out_bps"],
         d["read_bps"], d["write_bps"], j(d["volumes"]), n["in_bps"], n["out_bps"],
         j(busy_ifaces), s["thermal"]["speed_limit"], j(s["thermal"]["values"]), s["uptime_s"], s["nproc"]))
    con.execute("INSERT OR REPLACE INTO procs VALUES (?,?,?,?)",
                (s["ts"], j(s["top"]["cpu"]), j(s["top"]["mem"]), j(s["top"]["groups"])))


def event_dict(row, with_procs=True):
    e = {k: row[k] for k in ("id", "ts", "end_ts", "kind", "severity", "title", "value", "peak", "unit")}
    e["active"] = row["end_ts"] is None
    if with_procs:
        e["processes"] = top_dicts(row["by_cpu"], row["by_mem"], row["by_name"])
    return e


# ----------------------------------------------------------------------------
# Event detection
# ----------------------------------------------------------------------------
class EventEngine:
    """Opens an event row when a condition holds (optionally sustained), keeps
    its peak value current, and closes it once the condition has stayed clear
    for EVENT_CLEAR_S. One open event per condition key, so a long incident is
    one row with a duration rather than a row per sample."""

    def __init__(self, con, notifier=None):
        self.con = con
        self.notifier = notifier
        self.pending = {}   # key -> ts the condition first held
        self.active = {}    # key -> {"id", "peak", "clear_since", "higher"}
        self.swap_hist = deque()
        self.load_since = None        # ts the load first went above the warning line
        self.load_crit_since = None   # ts the load first went above the critical line
        self.load_event = None        # {"id", "peak", "critical"} while a load event is open
        # anything left open by a previous run ended when that run did
        last = con.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        con.execute("UPDATE events SET end_ts = COALESCE(?, ts) WHERE end_ts IS NULL", (last,))
        con.commit()

    def conditions(self, s):
        """[(key, holds, sustain_s, kind, severity, title, value, unit, higher_is_worse)]"""
        out = []
        level = s["mem"]["pressure_level"]
        out.append(("mem_warn", level >= MEM_PRESSURE_WARN_LEVEL, 0, "mem_pressure", "warning",
                    "Memory pressure: warn", level, "level", True))
        out.append(("mem_critical", level >= MEM_PRESSURE_CRITICAL_LEVEL, 0, "mem_pressure",
                    "critical", "Memory pressure: critical", level, "level", True))
        used = s["swap"]["used"]
        self.swap_hist.append((s["ts"], used))
        while self.swap_hist and self.swap_hist[0][0] < s["ts"] - SWAP_GROWTH_WINDOW_S:
            self.swap_hist.popleft()
        grown = (used - self.swap_hist[0][1]) / 1048576.0
        out.append(("swap_growing", grown >= SWAP_GROWTH_MB, 0, "swap_growing", "warning",
                    "Swap grew %d MB in %ds" % (grown, SWAP_GROWTH_WINDOW_S), grown, "MB", True))
        for v in s["disk"]["volumes"]:
            out.append(("disk_low:" + v["mount"], v["free_pct"] < DISK_FREE_MIN_PCT, 0, "disk_low",
                        "critical" if v["free_pct"] < DISK_FREE_MIN_PCT / 2 else "warning",
                        "Disk %s under %d%% free" % (v["mount"], DISK_FREE_MIN_PCT),
                        v["free_pct"], "% free", False))
        limit = s["thermal"]["speed_limit"]
        out.append(("throttle", limit is not None and limit < CPU_SPEED_LIMIT_MIN, 0, "cpu_throttle",
                    "warning", "CPU speed limited to %s%%" % limit, limit or 0, "%", False))
        state = s["thermal"].get("state")
        out.append(("thermal_state", state is not None and state >= THERMAL_STATE_WARN, 0, "cpu_throttle",
                    "critical" if state == 3 else "warning",
                    "Thermal state %s" % s["thermal"].get("state_name"), state or 0, "", True))
        return out

    def feed_load(self, s):
        """Load above core count: more runnable work than cores. High CPU % alone
        is not an event. Opens as a warning after LOAD_SUSTAIN_S above the core
        count, is raised to critical after LOAD_SUSTAIN_S above the critical
        line, and closes as soon as load is back under the clear line (the gap
        between the two lines is what stops it flapping)."""
        now, n, load = s["ts"], s["cpu"]["ncpu"], s["cpu"]["load"][0]
        warn, crit, clear = n * LOAD_WARN_X_NCPU, n * LOAD_CRITICAL_X_NCPU, n * LOAD_CLEAR_X_NCPU
        j = lambda o: json.dumps(o, separators=(",", ":"))
        self.load_crit_since = (self.load_crit_since or now) if load > crit else None
        act = self.load_event
        opened = False
        who = self._culprits(s["top"])
        body = "Load %.0f on %d cores" % (load, n) + (" - " + who if who else "")
        if act is None:
            if load <= warn:
                self.load_since = None
                return
            self.load_since = self.load_since or now
            if now - self.load_since < LOAD_SUSTAIN_S:
                return
            cur = self.con.execute(
                "INSERT INTO events (ts,end_ts,kind,severity,title,value,peak,unit,by_cpu,by_mem,by_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, None, "load_high", "warning",
                 "Load above %g cores for %ds" % (warn, LOAD_SUSTAIN_S), load, load, "load",
                 j(s["top"]["cpu"]), j(s["top"]["mem"]), j(s["top"]["groups"])))
            act = self.load_event = {"id": cur.lastrowid, "peak": load, "critical": False}
            opened = True
        elif load < clear:
            self.con.execute("UPDATE events SET end_ts=? WHERE id=?", (now, act["id"]))
            self.load_event = self.load_since = None
            return
        elif load > act["peak"]:
            act["peak"] = load
            self.con.execute("UPDATE events SET peak=? WHERE id=?", (load, act["id"]))
        if (not act["critical"] and self.load_crit_since is not None
                and now - self.load_crit_since >= LOAD_SUSTAIN_S):
            act["critical"] = True
            self._notify("critical", "load_high", "load_high",
                         "Critical: load above %gx core count" % LOAD_CRITICAL_X_NCPU, body)
            opened = False        # one banner, not two, when it opens already critical
            self.con.execute(
                "UPDATE events SET severity='critical', title=? WHERE id=?",
                ("Load above %g (%gx %d cores) for %ds" % (crit, LOAD_CRITICAL_X_NCPU, n, LOAD_SUSTAIN_S),
                 act["id"]))
        if opened:
            self._notify("open", "load_high", "load_high", "Load above core count", body)

    def track(self, key, now, opens, stays, sustain, clear_s, kind, sev, title, value, unit, higher, top,
              retitle=True):
        """One hysteresis rule. Opens after `opens` has held for `sustain` seconds,
        stays open while `stays` holds, closes once `stays` has been false for
        `clear_s`. Severity and title follow the condition while it is open."""
        j = lambda o: json.dumps(o, separators=(",", ":"))
        act = self.active.get(key)
        if act:
            if stays:
                act["clear_since"] = None
                if (value > act["peak"]) if higher else (value < act["peak"]):
                    act["peak"] = value
                    self.con.execute("UPDATE events SET peak=? WHERE id=?", (value, act["id"]))
                if opens and retitle and (title != act["title"] or sev != act["sev"]):
                    if sev == "critical" and act["sev"] != "critical":
                        self._notify_track("critical", key, kind, title, value, unit, top)
                    act["title"], act["sev"] = title, sev
                    self.con.execute("UPDATE events SET title=?, severity=? WHERE id=?", (title, sev, act["id"]))
            else:
                if act["clear_since"] is None:
                    act["clear_since"] = now
                if now - act["clear_since"] >= clear_s:
                    self.con.execute("UPDATE events SET end_ts=? WHERE id=?", (act["clear_since"], act["id"]))
                    if kind in NOTIFY_RECOVERY_KINDS:
                        self._notify_track("recover", key, kind, act["title"], value, unit, top,
                                           started=act["ts"], ended=act["clear_since"])
                    del self.active[key]
            return
        if not opens:
            self.pending.pop(key, None)
            return
        since = self.pending.setdefault(key, now)
        if now - since < sustain:
            return
        self.pending.pop(key, None)
        cur = self.con.execute(
            "INSERT INTO events (ts,end_ts,kind,severity,title,value,peak,unit,by_cpu,by_mem,by_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, None, kind, sev, title, value, value, unit,
             j(top["cpu"]), j(top["mem"]), j(top["groups"])))
        self.active[key] = {"id": cur.lastrowid, "peak": value, "clear_since": None, "title": title,
                            "sev": sev, "ts": now}
        self._notify_track("open", key, kind, title, value, unit, top)

    def feed_net(self, r, top):
        """Network rules, fed once per finished probe round."""
        now, w, p = r["ts"], r["window"], net_problems(r)
        down = r["state"] != "up"
        self.track("net_down", now, down, down, NET_DOWN_SUSTAIN_S, NET_CLEAR_S, "net_down", "critical",
                   "Internet down - " + net_where(r), 100.0, "% loss", True, top)
        up = not down and w["full"]
        loss = w["loss"] or 0.0
        self.track("net_loss", now, p["loss"], up and loss >= NET_LOSS_CLEAR_PCT, 0, NET_CLEAR_S,
                   "net_loss", "warning", "Packet loss above %g%%" % NET_LOSS_PCT, loss, "% loss", True, top)
        lat, jit = w["latency_ms"] or 0.0, w["jitter_ms"] or 0.0
        slow = p["latency"] or p["jitter"]
        still = up and (lat > NET_LATENCY_CLEAR_MS or jit > NET_JITTER_CLEAR_MS)
        if p["latency"] or not p["jitter"]:
            title, value, unit = "High latency, above %g ms" % NET_LATENCY_MS, lat, "ms"
        else:
            title, value, unit = "Unsteady connection, jitter above %g ms" % NET_JITTER_MS, jit, "ms jitter"
        self.track("net_slow", now, slow, still, 0, NET_CLEAR_S, "net_slow", "warning", title, value, unit, True, top)
        fail, dms = w["dns_fail_pct"] or 0, w["dns_ms"] or 0.0
        dns_bad = p["dns_fail"] or p["dns_slow"]
        dns_still = up and (fail >= NET_DNS_FAIL_PCT / 2.0 or dms > NET_DNS_SLOW_CLEAR_MS)
        if p["dns_fail"] or not p["dns_slow"]:
            title, value, unit = "DNS failing while the connection is up", float(fail), "% failed"
        else:
            title, value, unit = "DNS slow, above %g ms" % NET_DNS_SLOW_MS, dms, "ms"
        self.track("net_dns", now, dns_bad, dns_still, 0, NET_CLEAR_S, "net_dns", "warning", title, value, unit, True, top)

    def feed(self, s):
        self.feed_load(s)
        for key, holds, sustain, kind, sev, title, value, unit, higher in self.conditions(s):
            self.track(key, s["ts"], holds, holds, sustain, EVENT_CLEAR_S, kind, sev, title, value, unit,
                       higher, s["top"], retitle=kind != "swap_growing")

    # -- notifications: text is put together here, posting happens on the notifier's thread
    @staticmethod
    def _culprits(top, by_memory=False):
        rows = sorted(top.get("groups") or [], key=lambda g: -(g[3] if by_memory else g[2]))[:2]
        if by_memory:
            return ", ".join("%s %s" % (g[0], fmt_bytes(g[3] * 1024)) for g in rows)
        return ", ".join("%s x%d" % (g[0], g[1]) for g in rows if g[2] >= 50)

    def _notify(self, phase, key, kind, title, body):
        if self.notifier:
            self.notifier.event(phase, key, kind, title, body)

    def _notify_track(self, phase, key, kind, title, value, unit, top, started=None, ended=None):
        head, _, where = title.partition(" - ")
        if phase == "recover":
            return self._notify(phase, key, kind, "Internet back",
                                "Was down for %s (%s)" % (fmt_dur(ended - started), where or "cause unknown"))
        if kind == "net_down":
            body = where
        elif kind in ("mem_pressure", "swap_growing"):
            body = "Using most memory: " + (self._culprits(top, True) or "unknown")
        elif kind == "disk_low":
            body = "%.1f%% free" % value
        elif kind == "net_loss":
            body = "%.0f%% of pings lost over the last minute" % value
        elif kind == "net_slow":
            body = "%.0f %s over the last minute" % (value, unit)
        elif kind == "net_dns":
            body = "Pings are fine, so the line is up; name lookups are the problem"
        else:
            body = ""
        self._notify(phase, key, kind, ("Critical: " if phase == "critical" else "") + head, body)


# ----------------------------------------------------------------------------
# Sampler thread
# ----------------------------------------------------------------------------
class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sampler")
        self.collector = Collector()
        self.latest = None
        self.lock = threading.Lock()
        self.cost = deque(maxlen=120)   # (cpu seconds spent, wall seconds covered) per sample
        self.samples_taken = 0
        self.started = time.time()
        self.net = NetProbe()
        self.notifier = None

    @staticmethod
    def _cpu_spent():
        ch = resource.getrusage(resource.RUSAGE_CHILDREN)
        me = resource.getrusage(resource.RUSAGE_SELF)
        return me.ru_utime + me.ru_stime + ch.ru_utime + ch.ru_stime

    def overhead_pct(self):
        """CPU of the whole monitor (sampler, network probes, the commands they
        run, and serving the dashboard) as % of one core."""
        with self.lock:
            cost = list(self.cost)
        wall = sum(c[1] for c in cost)
        return round(sum(c[0] for c in cost) * 100.0 / wall, 3) if wall else None

    def run(self):
        con = open_db()
        engine = EventEngine(con, self.notifier)
        self.collector.sample()           # prime the counters
        self.net.start()
        next_at = time.monotonic() + 1
        last_prune = 0
        last_wall = time.monotonic()
        last_cpu = self._cpu_spent()
        while True:
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_at = max(next_at + SAMPLE_INTERVAL_S, time.monotonic() + 1)
            try:
                s = self.collector.sample()
                store_sample(con, s)
                engine.feed(s)
                for r in self.net.drain():
                    store_net(con, r)
                    engine.feed_net(r, s["top"])
                if RETENTION_DAYS > 0 and time.time() - last_prune > 3600:
                    cutoff = int(time.time()) - RETENTION_DAYS * 86400
                    con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                    con.execute("DELETE FROM procs WHERE ts < ?", (cutoff,))
                    con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                    con.execute("DELETE FROM net WHERE ts < ?", (cutoff,))
                    last_prune = time.time()
                con.commit()
                self.samples_taken += 1
                wall = time.monotonic()
                with self.lock:
                    self.latest = s
                    cpu = self._cpu_spent()
                    self.cost.append((cpu - last_cpu, wall - last_wall))
                last_wall, last_cpu = wall, cpu
            except Exception as e:  # never let one bad sample stop the sampler
                sys.stderr.write("sampler error: %r\n" % (e,))
                try:
                    con.rollback()
                except Exception:
                    pass


# ----------------------------------------------------------------------------
# Queries shared by the HTTP API and the snapshot report
# ----------------------------------------------------------------------------
def query_history(con, rng, end=None):
    span, bucket = RANGES[rng]
    now = int(time.time())
    end = int(end) if end else now
    if span is None:
        first = con.execute("SELECT MIN(ts) FROM samples").fetchone()[0] or end
        span = max(900, end - first + 60)
        bucket = max(5, -(-span // 360 // 5) * 5)
    start = end - span
    rows = con.execute(
        "SELECT (ts/?)*? AS t, AVG(cpu_user), AVG(cpu_sys), MAX(cpu_user+cpu_sys), AVG(load1),"
        " AVG(mem_app), AVG(mem_wired), AVG(mem_compressed), AVG(swap_used), MAX(pressure),"
        " AVG(disk_read), AVG(disk_write), AVG(net_in), AVG(net_out), COUNT(*), MAX(load1)"
        " FROM samples WHERE ts > ? AND ts <= ? GROUP BY ts/? ORDER BY t",
        (bucket, bucket, start, end, bucket)).fetchall()
    names = ["t", "cpu_user", "cpu_sys", "cpu_peak", "load1", "mem_app", "mem_wired",
             "mem_compressed", "swap_used", "pressure", "disk_read", "disk_write",
             "net_in", "net_out", "n", "load_peak"]
    cols = {n: [] for n in names}
    for r in rows:
        for i, n in enumerate(names):
            v = r[i]
            cols[n].append(round(v, 2) if isinstance(v, float) else v)
    # internet probes, laid onto the same buckets (None where a bucket has no probe)
    nb = max(bucket, NET_PROBE_INTERVAL_S)
    net = {}
    for r in con.execute(
            "SELECT (ts/?)*? AS t, AVG(gw_ms), AVG(a_ms), AVG(b_ms), AVG(loss), MAX(state != 'up'),"
            " AVG(dns_ms), MAX(MAX(COALESCE(a_max, 0), COALESCE(b_max, 0))), AVG(gw_loss)"
            " FROM net WHERE ts > ? AND ts <= ? GROUP BY ts/?", (nb, nb, start, end, nb)):
        net[r[0]] = r
    nnames = ["net_gw", "net_a", "net_b", "net_loss", "net_down", "dns_ms", "net_worst", "net_gw_loss"]
    for n in nnames:
        cols[n] = []
    for t in cols["t"]:
        r = net.get(t) if t % nb == 0 else None
        for i, n in enumerate(nnames):
            v = r[i + 1] if r is not None else None
            cols[n].append(round(v, 1) if isinstance(v, float) else v)
    evs = con.execute(
        "SELECT * FROM events WHERE ts <= ? AND COALESCE(end_ts, ?) >= ? ORDER BY ts",
        (end, now, start)).fetchall()
    return {"range": rng, "start": start, "end": end, "bucket": bucket, "live": end >= now - 2,
            "points": len(rows), "series": cols, "net_summary": net_summary(con, start, end),
            "net_targets": [{"name": n, "ip": ip} for n, ip in NET_TARGETS],
            "events": [event_dict(e, with_procs=False) for e in evs]}


def query_processes(con, at=None, window=0):
    """Top processes recorded nearest to `at`. With window>0, the busiest
    sample inside [at, at+window) is used so a downsampled chart point maps to
    the moment that made it tall."""
    row = None
    if at is None:
        row = con.execute("SELECT * FROM procs ORDER BY ts DESC LIMIT 1").fetchone()
    else:
        at = int(at)
        if window and window > SAMPLE_INTERVAL_S:
            hit = con.execute(
                "SELECT ts FROM samples WHERE ts >= ? AND ts < ? ORDER BY cpu_user+cpu_sys DESC LIMIT 1",
                (at, at + int(window))).fetchone()
            if hit:
                row = con.execute("SELECT * FROM procs WHERE ts = ?", (hit[0],)).fetchone()
        if row is None:
            a = con.execute("SELECT * FROM procs WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (at,)).fetchone()
            b = con.execute("SELECT * FROM procs WHERE ts > ? ORDER BY ts LIMIT 1", (at,)).fetchone()
            cands = [r for r in (a, b) if r is not None]
            row = min(cands, key=lambda r: abs(r["ts"] - at)) if cands else None
    if row is None:
        return {"requested": at, "ts": None, "by_cpu": [], "by_mem": [], "by_name": [], "cpu": None}
    srow = con.execute("SELECT cpu_user, cpu_sys, mem_used, pressure FROM samples WHERE ts = ?",
                       (row["ts"],)).fetchone()
    out = {"requested": at, "ts": row["ts"],
           "cpu": round(srow[0] + srow[1], 1) if srow else None,
           "mem_used": srow[2] if srow else None}
    out.update(top_dicts(row["by_cpu"], row["by_mem"], row["by_name"]))
    return out


def query_events(con, limit=100, since=None):
    rows = con.execute("SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                       (int(since or 0), int(limit))).fetchall()
    return [event_dict(r) for r in rows]


# ----------------------------------------------------------------------------
# Notifications. `osascript` posts them (they appear under "Script Editor").
# Text goes in as arguments to a fixed script, never spliced into AppleScript,
# because process names are arbitrary. Posting happens on this thread with a
# hard timeout, so a slow or failing osascript cannot hold up sampling.
# ----------------------------------------------------------------------------
class Notifier(threading.Thread):
    SCRIPT = ["-e", "on run argv", "-e",
              "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run"]

    def __init__(self, url):
        super().__init__(daemon=True, name="notifier")
        self.url = url
        self.q = queue.Queue(maxsize=20)
        self.lock = threading.Lock()
        self.posted = {}        # cooldown key -> ts of the last notification
        self.open_posted = set()  # event keys whose opening was announced (so recovery may be)
        self.mute = meta_get("notify_mute") or {"until": 0, "off": False}
        self.last = None        # result of the most recent post

    def state(self):
        if not NOTIFY_ENABLED:
            return "disabled"
        with self.lock:
            if self.mute.get("off"):
                return "off"
            return "muted" if self.mute.get("until", 0) > time.time() else "on"

    def status(self):
        st = self.state()
        with self.lock:
            return {"state": st, "muted_until": self.mute.get("until") if st == "muted" else None,
                    "cooldown_s": NOTIFY_COOLDOWN_S, "last": self.last,
                    "rules": {k: bool(v) for k, v in NOTIFY_RULES.items()}}

    def set_mute(self, action, seconds=3600):
        with self.lock:
            if action == "on":
                self.mute = {"until": 0, "off": False}
            elif action == "off":
                self.mute = {"until": 0, "off": True}
            elif action == "mute":
                self.mute = {"until": int(time.time() + max(60, min(int(seconds), 7 * 86400))), "off": False}
            else:
                raise ValueError("unknown action")
            mute = dict(self.mute)
        meta_set("notify_mute", mute)
        return self.status()

    def event(self, phase, key, kind, title, body):
        """Called from the sampler thread; only ever queues."""
        now = time.time()
        if phase == "recover":
            announced = key in self.open_posted
            self.open_posted.discard(key)
            if not announced:
                return False
        if self.state() != "on" or not NOTIFY_RULES.get(kind, True):
            return False
        ck = kind if phase == "open" else kind + ":" + phase
        if phase != "recover" and now - self.posted.get(ck, 0) < NOTIFY_COOLDOWN_S:
            return False
        self.posted[ck] = now
        if phase == "open":
            self.open_posted.add(key)
        try:
            self.q.put_nowait((title, body))
        except queue.Full:
            return False
        return True

    def post(self, title, body):
        """Post now, on the calling thread. Returns {"ts", "title", "body", "ok", "error"}."""
        text = (body + "  " if body else "") + self.url
        res = {"ts": int(time.time()), "title": title, "body": text, "ok": False, "error": None}
        try:
            out = subprocess.run([OSASCRIPT] + self.SCRIPT + [title, text], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, timeout=NOTIFY_TIMEOUT_S)
            res["ok"] = out.returncode == 0
            if not res["ok"]:
                res["error"] = (out.stderr.decode("utf-8", "replace").strip() or "osascript exit %d" % out.returncode)[:200]
        except subprocess.TimeoutExpired:
            res["error"] = "osascript did not finish within %d s" % NOTIFY_TIMEOUT_S
        except OSError as e:
            res["error"] = str(e)[:200]
        with self.lock:
            self.last = res
        if not res["ok"]:
            sys.stderr.write("notification failed: %s\n" % res["error"])
        return res

    def run(self):
        while True:
            title, body = self.q.get()
            self.post("mac-monitor: " + title, body)


# ----------------------------------------------------------------------------
# On-demand speed test (networkQuality ships with macOS). Never runs by itself:
# it moves a couple of hundred MB and disturbs the line while it runs.
# ----------------------------------------------------------------------------
class SpeedTest:
    def __init__(self):
        self.lock = threading.Lock()
        self.running_since = None
        self.last = meta_get("speedtest")

    def status(self):
        with self.lock:
            return {"available": os.path.exists("/usr/bin/networkQuality"),
                    "running": self.running_since is not None, "started": self.running_since,
                    "expected_s": 20, "last": self.last}

    def start(self):
        with self.lock:
            if self.running_since is None:
                self.running_since = int(time.time())
                threading.Thread(target=self._run, daemon=True, name="speedtest").start()
        return self.status()

    def _run(self):
        res = {"ts": int(time.time()), "ok": False, "error": None}
        SPEEDTEST_RUNNING.set()
        try:
            out = subprocess.run(["/usr/bin/networkQuality", "-c"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=90)
            d = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
            if d.get("dl_throughput"):
                res.update(ok=True, down_mbps=round(d["dl_throughput"] / 1e6, 1),
                           up_mbps=round(d.get("ul_throughput", 0) / 1e6, 1),
                           rtt_ms=round(d["base_rtt"], 1) if d.get("base_rtt") else None,
                           responsiveness_rpm=round(d["responsiveness"]) if d.get("responsiveness") else None,
                           iface=d.get("interface_name"),
                           megabytes=round((d.get("dl_bytes_transferred", 0) + d.get("ul_bytes_transferred", 0)) / 1e6))
            else:
                res["error"] = (out.stderr.decode("utf-8", "replace").strip() or "no result")[:160]
        except subprocess.TimeoutExpired:
            res["error"] = "timed out after 90 s"
        except Exception as e:
            res["error"] = str(e)[:160]
        SPEEDTEST_RUNNING.clear()
        res["took_s"] = int(time.time()) - res["ts"]
        try:
            meta_set("speedtest", res)
        except sqlite3.Error:
            pass
        with self.lock:
            self.last, self.running_since = res, None


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    sampler = None
    speedtest = None
    server_version = "mac-monitor"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def do_POST(self):
        # state-changing calls must come from the dashboard's own script: a custom
        # header cannot be sent cross-site without a preflight this server never grants
        u = urlparse(self.path)
        try:
            raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
            if self.headers.get("X-Mac-Monitor") != "1":
                return self._json({"error": "missing X-Mac-Monitor header"}, 403)
            if u.path == "/api/notifications":
                body = json.loads(raw.decode("utf-8") or "{}")
                nt, action = self.sampler.notifier, body.get("action")
                if action == "test":      # explicit, so it posts even while muted
                    res = nt.post("mac-monitor: test notification", "Notifications from mac-monitor look like this.")
                    return self._json(dict(nt.status(), test=res))
                return self._json(nt.set_mute(action, body.get("seconds", 3600)))
            if u.path == "/api/speedtest":
                return self._json(self.speedtest.start())
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"error": repr(e)}, 500)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/now":
                return self._json(self.api_now())
            if u.path == "/api/history":
                rng = q.get("range", "1h")
                if rng not in RANGES:
                    return self._json({"error": "range must be one of " + ", ".join(RANGES)}, 400)
                con = open_db(readonly=True)
                try:
                    return self._json(query_history(con, rng, q.get("end")))
                finally:
                    con.close()
            if u.path == "/api/events":
                con = open_db(readonly=True)
                try:
                    return self._json({"events": query_events(con, q.get("limit", 100), q.get("since"))})
                finally:
                    con.close()
            if u.path == "/api/speedtest":
                return self._json(self.speedtest.status())
            if u.path == "/api/processes":
                con = open_db(readonly=True)
                try:
                    return self._json(query_processes(con, q.get("at"), int(q.get("window", 0))))
                finally:
                    con.close()
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self._json({"error": "bad parameter: %s" % e}, 400)
        except Exception as e:
            self._json({"error": repr(e)}, 500)

    def api_now(self):
        sm = self.sampler
        with sm.lock:
            s = sm.latest
        if s is None:
            return {"ready": False, "host": sm.collector.host}
        out = dict(s)
        out["ready"] = True
        out["top"] = {"by_cpu": proc_dicts(s["top"]["cpu"]), "by_mem": proc_dicts(s["top"]["mem"]),
                      "by_name": group_dicts(s["top"]["groups"])}
        out["host"] = sm.collector.host
        con = open_db(readonly=True)
        try:
            rows = con.execute("SELECT * FROM events WHERE end_ts IS NULL ORDER BY ts DESC").fetchall()
            out["active_events"] = [event_dict(r, with_procs=False) for r in rows]
        finally:
            con.close()
        with sm.net.lock:
            net = sm.net.latest
        out["internet"] = net and {k: v for k, v in net.items() if k != "http_new"}
        out["notifications"] = sm.notifier.status() if sm.notifier else None
        n = sm.collector.ncpu
        out["limits"] = {"load_warn": n * LOAD_WARN_X_NCPU, "load_critical": n * LOAD_CRITICAL_X_NCPU,
                         "load_clear": n * LOAD_CLEAR_X_NCPU, "load_sustain_s": LOAD_SUSTAIN_S}
        out["sampler"] = {"interval_s": SAMPLE_INTERVAL_S, "overhead_pct_of_one_core": sm.overhead_pct(),
                          "samples_this_run": sm.samples_taken, "running_s": int(time.time() - sm.started),
                          "native_readers": sm.collector.native is not None}
        return out


def pick_port():
    env = os.environ.get("MAC_MONITOR_PORT")
    if env:
        return int(env), True
    for port in range(PORT_RANGE[0], PORT_RANGE[1] + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as the real server does
            try:
                s.bind(("127.0.0.1", port))
                return port, False
            except OSError:
                continue
    raise SystemExit("no free port in %d-%d; set MAC_MONITOR_PORT" % PORT_RANGE)


def serve():
    port, forced = pick_port()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit("cannot listen on 127.0.0.1:%d (%s)" % (port, e))
    httpd.daemon_threads = True
    sampler = Sampler()
    sampler.notifier = Notifier("http://127.0.0.1:%d/" % port)
    sampler.notifier.start()
    Handler.sampler = sampler
    Handler.speedtest = SpeedTest()
    sampler.start()
    print("mac-monitor: http://127.0.0.1:%d/   (data: %s)" % (port, DB_PATH), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ----------------------------------------------------------------------------
# Snapshot report
# ----------------------------------------------------------------------------
def fmt_bytes(n, rate=False):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            s = ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
            return s + ("/s" if rate else "")
        n /= 1024


def fmt_dur(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return "%dd %dh %dm" % (d, h, m)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm %ds" % (m, s) if m else "%ds" % s


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def snapshot():
    c = Collector()
    c.sample()
    time.sleep(2)
    s = c.sample()
    P = print
    P("mac-monitor snapshot   %s   %s" % (fmt_ts(s["ts"]), c.host["name"]))
    P("%s, %d cores, %s RAM, up %s, %d processes" % (
        c.host["model"], c.ncpu, fmt_bytes(c.mem_total), fmt_dur(s["uptime_s"]), s["nproc"]))
    P("")
    P("NOW (2s sample)")
    cpu, mem, sw = s["cpu"], s["mem"], s["swap"]
    P("  CPU       %5.1f%% busy  (user %.1f%%, system %.1f%%, idle %.1f%%)" % (
        cpu["busy"], cpu["user"], cpu["sys"], cpu["idle"]))
    l1 = cpu["load"][0]
    state = ("CRITICAL, above %gx cores" % LOAD_CRITICAL_X_NCPU if l1 > c.ncpu * LOAD_CRITICAL_X_NCPU
             else "WARNING, above core count" if l1 > c.ncpu * LOAD_WARN_X_NCPU else "ok")
    P("  Load      %.2f of %d cores  (1 min; 5 min %.2f, 15 min %.2f)   %s" % (
        l1, c.ncpu, cpu["load"][1], cpu["load"][2], state))
    if cpu["per_core"]:
        P("  Per core  " + " ".join("%3.0f" % x for x in cpu["per_core"]))
    P("  Memory    %s used of %s  (app %s, wired %s, compressed %s, cached %s, free %s)" % (
        fmt_bytes(mem["used"]), fmt_bytes(mem["total"]), fmt_bytes(mem["app"]), fmt_bytes(mem["wired"]),
        fmt_bytes(mem["compressed"]), fmt_bytes(mem["cached"]), fmt_bytes(mem["free"])))
    P("  Pressure  %s (level %d)" % (mem["pressure"], mem["pressure_level"]))
    P("  Swap      %s used of %s   in %s  out %s" % (
        fmt_bytes(sw["used"]), fmt_bytes(sw["total"]), fmt_bytes(sw["in_bps"], True), fmt_bytes(sw["out_bps"], True)))
    for v in s["disk"]["volumes"]:
        P("  Disk      %-22s %s free of %s (%.1f%% free)" % (
            v["mount"], fmt_bytes(v["free"]), fmt_bytes(v["total"]), v["free_pct"]))
    P("  Disk IO   read %s   write %s" % (fmt_bytes(s["disk"]["read_bps"], True), fmt_bytes(s["disk"]["write_bps"], True)))
    P("  Network   in %s   out %s   (%s)" % (
        fmt_bytes(s["net"]["in_bps"], True), fmt_bytes(s["net"]["out_bps"], True),
        ", ".join(k for k, v in s["net"]["ifaces"].items()
                  if not k.startswith("lo") and (v["in_bps"] or v["out_bps"])) or "idle"))
    probe = NetProbe()
    net = probe.cycle(wait_http=True)
    g, d, l, h = net["gateway"], net["dns"], net["link"], probe.http
    ms = lambda v: "no reply" if v is None else "%.1f ms" % v
    P("  Internet  %s" % net["verdict"]["text"])
    P("    router  %-15s %s, %.0f%% loss" % (g["ip"] or "none", ms(g["avg"]), g["loss"]))
    for t in net["targets"]:
        P("    %-7s %-15s %s (max %s, jitter %s), %.0f%% loss" % (
            t["name"][:7], t["ip"], ms(t["avg"]), ms(t["max"]), ms(t["sd"]), t["loss"]))
    P("    DNS     %s via %s: %s" % (d["name"], d["server"] or "no resolver",
                                    ms(d["ms"]) if d["ok"] else "FAILED (%s)" % d["error"]))
    P("    resolvers  " + (", ".join(dns_servers()) or "none configured"))
    if h:
        P("    HTTPS   %s: %s" % (h["url"], ms(h["ms"]) if h["ok"] else "FAILED (%s)" % h["error"]))
    if l["type"] == "Wi-Fi":
        P("    link    Wi-Fi %s, signal %s dBm, noise %s dBm, tx rate %s Mbps, channel %s (%s)" % (
            l["iface"], l["rssi"], l["noise"], "%.0f" % l["tx_rate"] if l["tx_rate"] else "?", l["channel"], l["band"]))
    else:
        P("    link    %s %s" % (l["type"] or "none", l["iface"] or ""))
    th = s["thermal"]
    if th["speed_limit"] is None and th.get("state") is not None:
        P("  Thermal   %s (macOS thermal state %d of 3)" % (th["state_name"], th["state"]))
    elif th["speed_limit"] is None:
        P("  Thermal   CPU speed limit not reported by pmset on this machine" +
          ("  [%s]" % "; ".join(th["notes"]) if th["notes"] else ""))
    else:
        P("  Thermal   " + ", ".join("%s=%s" % kv for kv in th["values"].items()))
    P("")
    for title, key in (("TOP BY CPU", "cpu"), ("TOP BY MEMORY", "mem")):
        P(title)
        for pid, name, pc, rss in s["top"][key]:
            P("  %6.1f%%  %10s  %7d  %s" % (pc, fmt_bytes(rss * 1024), pid, name))
        P("")
    P("TOP BY NAME (same-named processes added together)")
    for name, cnt, pc, rss in s["top"]["groups"][:TOP_N]:
        P("  %6.1f%%  %10s  %4d x  %s" % (pc, fmt_bytes(rss * 1024), cnt, name))
    P("")
    if not os.path.exists(DB_PATH):
        P("No history yet (%s does not exist; start ./run to begin recording)." % DB_PATH)
        return
    con = open_db(readonly=True)
    since = int(time.time()) - 86400
    n, first, last = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples WHERE ts > ?", (since,)).fetchone()
    P("LAST 24H  (%d samples%s)" % (n, ", %s to %s" % (fmt_ts(first), fmt_ts(last)) if n else ""))
    if n:
        if time.time() - last > 3 * SAMPLE_INTERVAL_S:
            P("  note: newest sample is %s old, the sampler does not appear to be running" % fmt_dur(time.time() - last))

        def peak(expr, fmt):
            r = con.execute("SELECT ts, %s AS v FROM samples WHERE ts > ? ORDER BY v DESC LIMIT 1" % expr, (since,)).fetchone()
            return "%s at %s" % (fmt(r["v"]), fmt_ts(r["ts"])), r["ts"]

        avg = con.execute("SELECT AVG(cpu_user+cpu_sys), AVG(mem_used), MIN(mem_free) FROM samples WHERE ts > ?", (since,)).fetchone()
        txt, cpu_ts = peak("cpu_user+cpu_sys", lambda v: "%.1f%%" % v)
        P("  CPU peak        %s   (average %.1f%%)" % (txt, avg[0]))
        top = query_processes(con, cpu_ts)["by_name"][:3]
        if top:
            P("    top then:     " + ", ".join("%s x%d %.0f%%" % (p["name"], p["count"], p["cpu"]) for p in top))
        P("  Load peak       " + peak("load1", lambda v: "%.2f of %d cores" % (v, c.ncpu))[0])
        P("  Memory peak     %s   (average %s)" % (peak("mem_used", fmt_bytes)[0], fmt_bytes(avg[1])))
        P("  Compressed peak " + peak("mem_compressed", fmt_bytes)[0])
        P("  Swap peak       " + peak("swap_used", fmt_bytes)[0])
        P("  Pressure peak   " + peak("pressure", lambda v: PRESSURE_NAMES.get(v, str(v)))[0])
        P("  Disk read peak  " + peak("disk_read", lambda v: fmt_bytes(v, True))[0])
        P("  Disk write peak " + peak("disk_write", lambda v: fmt_bytes(v, True))[0])
        P("  Net in peak     " + peak("net_in", lambda v: fmt_bytes(v, True))[0])
        P("  Net out peak    " + peak("net_out", lambda v: fmt_bytes(v, True))[0])
        r = con.execute("SELECT MIN(speed_limit) FROM samples WHERE ts > ?", (since,)).fetchone()[0]
        P("  Speed limit min %s" % ("not reported" if r is None else "%d%%" % r))
    try:
        ns = net_summary(con, since, int(time.time()))
    except sqlite3.Error:
        ns = None
    P("")
    P("INTERNET, LAST 24H" + ("  (%d probe rounds)" % ns["probes"] if ns else ""))
    if not ns:
        P("  no probes recorded yet")
    else:
        P("  Uptime          %.2f%%" % ns["uptime_pct"])
        P("  Outages         %d, %s in total%s" % (
            ns["outages"], fmt_dur(ns["outage_s"]),
            ", longest %s, last at %s" % (fmt_dur(ns["longest_outage_s"]), fmt_ts(ns["last_outage"])) if ns["outages"] else ""))
        P("  Latency         median %s, worst %s" % (ms(ns["latency_median_ms"]), ms(ns["latency_worst_ms"])))
        P("  Packet loss     %s while up" % ("-" if ns["loss_pct"] is None else "%.2f%%" % ns["loss_pct"]))
        st = meta_get("speedtest")
        if st and st.get("ok"):
            P("  Last speed test %s: %.1f Mbps down, %.1f Mbps up" % (fmt_ts(st["ts"]), st["down_mbps"], st["up_mbps"]))
    P("")
    evs = con.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 15").fetchall()
    mute = meta_get("notify_mute") or {}
    P("NOTIFICATIONS  " + (
        "disabled in monitor.py (NOTIFY_ENABLED)" if not NOTIFY_ENABLED
        else "off, switched off in the dashboard" if mute.get("off")
        else "muted until %s" % fmt_ts(mute["until"]) if mute.get("until", 0) > time.time()
        else "on: a macOS banner (shown under Script Editor) when an event opens"))
    P("")
    P("RECENT EVENTS (%d shown, %s)" % (len(evs), "%d days kept" % RETENTION_DAYS if RETENTION_DAYS > 0 else "all kept"))
    if not evs:
        P("  none recorded")
    for e in evs:
        dur = "ongoing" if e["end_ts"] is None else "lasted " + fmt_dur(e["end_ts"] - e["ts"])
        P("  %s  %-8s %s  (peak %.1f %s, %s)" % (fmt_ts(e["ts"]), e["severity"], e["title"], e["peak"], e["unit"], dur))
        top = json.loads(e["by_name"] or "[]")[:4]
        if top:
            P("      top cpu: " + ", ".join("%s x%d %.0f%%" % (p[0], p[1], p[2]) for p in top))
    con.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "snapshot":
        snapshot()
    else:
        sys.exit(__doc__)
