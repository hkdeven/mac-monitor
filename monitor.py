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
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ----------------------------------------------------------------------------
# Tunables. Event thresholds live here.
# ----------------------------------------------------------------------------
CPU_HIGH_PCT = 85.0            # total CPU busy % that counts as "high"
CPU_HIGH_SUSTAIN_S = 30        # ...and for how long before an event is recorded
MEM_PRESSURE_WARN_LEVEL = 2    # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
MEM_PRESSURE_CRITICAL_LEVEL = 4
SWAP_GROWTH_MB = 256           # swap used grew by at least this much...
SWAP_GROWTH_WINDOW_S = 60      # ...within this window
DISK_FREE_MIN_PCT = 10.0       # a volume with less free space than this
CPU_SPEED_LIMIT_MIN = 100      # pmset CPU_Speed_Limit below this means throttling
EVENT_CLEAR_S = 60             # condition must stay clear this long before an event closes

SAMPLE_INTERVAL_S = 5          # fast metrics: cpu, memory, processes, disk io, network
SLOW_INTERVAL_S = 30           # slow metrics: df, pmset -g therm
RETENTION_DAYS = 7
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
    return {"speed_limit": values.get("CPU_Speed_Limit"), "values": values, "notes": notes}


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
            "nproc": len(procs),
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

    def __init__(self, con):
        self.con = con
        self.pending = {}   # key -> ts the condition first held
        self.active = {}    # key -> {"id", "peak", "clear_since", "higher"}
        self.swap_hist = deque()
        # anything left open by a previous run ended when that run did
        last = con.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        con.execute("UPDATE events SET end_ts = COALESCE(?, ts) WHERE end_ts IS NULL", (last,))
        con.commit()

    def conditions(self, s):
        """[(key, holds, sustain_s, kind, severity, title, value, unit, higher_is_worse)]"""
        out = []
        busy = s["cpu"]["busy"]
        out.append(("cpu_high", busy > CPU_HIGH_PCT, CPU_HIGH_SUSTAIN_S, "cpu_high", "warning",
                    "CPU above %d%% for %ds" % (CPU_HIGH_PCT, CPU_HIGH_SUSTAIN_S), busy, "%", True))
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
        return out

    def feed(self, s):
        now = s["ts"]
        j = lambda o: json.dumps(o, separators=(",", ":"))
        for key, holds, sustain, kind, sev, title, value, unit, higher in self.conditions(s):
            act = self.active.get(key)
            if act:
                if holds:
                    act["clear_since"] = None
                    worse = value > act["peak"] if higher else value < act["peak"]
                    if worse:
                        act["peak"] = value
                        self.con.execute("UPDATE events SET peak=? WHERE id=?", (value, act["id"]))
                else:
                    if act["clear_since"] is None:
                        act["clear_since"] = now
                    if now - act["clear_since"] >= EVENT_CLEAR_S:
                        self.con.execute("UPDATE events SET end_ts=? WHERE id=?",
                                         (act["clear_since"], act["id"]))
                        del self.active[key]
                continue
            if not holds:
                self.pending.pop(key, None)
                continue
            since = self.pending.setdefault(key, now)
            # each sample describes the interval before it, hence the + interval
            if now - since + SAMPLE_INTERVAL_S < sustain:
                continue
            self.pending.pop(key, None)
            cur = self.con.execute(
                "INSERT INTO events (ts,end_ts,kind,severity,title,value,peak,unit,by_cpu,by_mem,by_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, None, kind, sev, title, value, value, unit,
                 j(s["top"]["cpu"]), j(s["top"]["mem"]), j(s["top"]["groups"])))
            self.active[key] = {"id": cur.lastrowid, "peak": value, "clear_since": None}


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

    @staticmethod
    def _cpu_spent():
        ch = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            own = time.thread_time()
        except Exception:
            own = 0.0
        return own + ch.ru_utime + ch.ru_stime

    def overhead_pct(self):
        """Sampler CPU (this thread + the commands it runs) as % of one core."""
        with self.lock:
            cost = list(self.cost)
        wall = sum(c[1] for c in cost)
        return round(sum(c[0] for c in cost) * 100.0 / wall, 3) if wall else None

    def run(self):
        con = open_db()
        engine = EventEngine(con)
        self.collector.sample()           # prime the counters
        next_at = time.monotonic() + 1
        last_prune = 0
        last_wall = time.monotonic()
        while True:
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_at = max(next_at + SAMPLE_INTERVAL_S, time.monotonic() + 1)
            c0 = self._cpu_spent()
            try:
                s = self.collector.sample()
                store_sample(con, s)
                engine.feed(s)
                if time.time() - last_prune > 3600:
                    cutoff = int(time.time()) - RETENTION_DAYS * 86400
                    con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                    con.execute("DELETE FROM procs WHERE ts < ?", (cutoff,))
                    con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                    last_prune = time.time()
                con.commit()
                self.samples_taken += 1
                wall = time.monotonic()
                with self.lock:
                    self.latest = s
                    self.cost.append((self._cpu_spent() - c0, wall - last_wall))
                last_wall = wall
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
    start = end - span
    rows = con.execute(
        "SELECT (ts/?)*? AS t, AVG(cpu_user), AVG(cpu_sys), MAX(cpu_user+cpu_sys), AVG(load1),"
        " AVG(mem_app), AVG(mem_wired), AVG(mem_compressed), AVG(swap_used), MAX(pressure),"
        " AVG(disk_read), AVG(disk_write), AVG(net_in), AVG(net_out), COUNT(*)"
        " FROM samples WHERE ts > ? AND ts <= ? GROUP BY ts/? ORDER BY t",
        (bucket, bucket, start, end, bucket)).fetchall()
    names = ["t", "cpu_user", "cpu_sys", "cpu_peak", "load1", "mem_app", "mem_wired",
             "mem_compressed", "swap_used", "pressure", "disk_read", "disk_write",
             "net_in", "net_out", "n"]
    cols = {n: [] for n in names}
    for r in rows:
        for i, n in enumerate(names):
            v = r[i]
            cols[n].append(round(v, 2) if isinstance(v, float) else v)
    evs = con.execute(
        "SELECT * FROM events WHERE ts <= ? AND COALESCE(end_ts, ?) >= ? ORDER BY ts",
        (end, now, start)).fetchall()
    return {"range": rng, "start": start, "end": end, "bucket": bucket, "live": end >= now - 2,
            "points": len(rows), "series": cols,
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
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    sampler = None
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
    Handler.sampler = sampler
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
    P("  CPU       %5.1f%% busy  (user %.1f%%, system %.1f%%, idle %.1f%%)   load %.2f %.2f %.2f" % (
        cpu["busy"], cpu["user"], cpu["sys"], cpu["idle"], *cpu["load"]))
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
    th = s["thermal"]
    if th["speed_limit"] is None:
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
        P("  Load peak       " + peak("load1", lambda v: "%.2f" % v)[0])
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
    P("")
    evs = con.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 15").fetchall()
    P("RECENT EVENTS (%d shown, 7 days kept)" % len(evs))
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
