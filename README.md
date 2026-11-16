# mac-monitor

A small local monitor for this Mac: what is the machine doing now, and what was
it doing when it was slow at 3am. Python 3 standard library only, no sudo,
nothing installed system-wide.

## Use

    ./run         start it in the foreground and print the URL (Ctrl-C stops it)
    ./snapshot    print a plain-text report: now, last 24h peaks, recent events

`./run` picks the first free port in 5180-5199 (normally http://127.0.0.1:5180/).
Set `MAC_MONITOR_PORT=5190 ./run` to choose one. It listens on 127.0.0.1 only.

To leave it running after the terminal closes:

    nohup ./run > data/server.log 2>&1 &

Stop that with `pkill -f "monitor.py serve"`. It does not start at login; history
has a gap for any time it was not running (the charts show the gap).

`./snapshot` takes its own 2 second reading, so it works when the server is down.

## What is recorded

Every 5 seconds, into `data/monitor.db` (SQLite, gitignored, pruned to 7 days):

- CPU: user / system / idle, per core, load averages
- Top 10 processes by CPU and top 10 by memory (name, pid, cpu %, rss), plus the
  same totals grouped by process name
- Memory: app / wired / compressed / cached / free, pressure level, swap used,
  swap in / out rate
- Disk read / write throughput, network bytes in / out per interface
- Every 30 seconds: free space per volume, `pmset -g therm`

A week of history is roughly 150-250 MB.

## Events

An event row is written, with the process lists at that moment, when:

| Event | Condition |
|---|---|
| CPU high | total CPU above 85% for 30 s |
| Memory pressure | kernel pressure level warn or critical |
| Swap growing | swap used up 256 MB within 60 s |
| Disk low | a volume under 10% free |
| CPU throttled | `CPU_Speed_Limit` below 100 |

One event covers the whole incident: it closes when the condition has been
clear for 60 s. The thresholds are constants at the top of `monitor.py`.

## JSON

    /api/now                         latest sample, live process lists, open events
    /api/history?range=1h            15m | 1h | 6h | 24h | 7d, downsampled to ~360 points
    /api/history?range=1h&end=TS     the window ending at a unix time
    /api/events?limit=100&since=TS   events, newest first, with their process lists
    /api/processes?at=TS             process lists recorded nearest that unix time
    /api/processes?at=TS&window=240  the busiest sample within that many seconds after TS

Downsampled CPU carries both the average and the peak (`cpu_peak`) of each bucket.

## Where the numbers come from

| Metric | Source |
|---|---|
| CPU, per core | `host_processor_info` (the call `top` uses), via ctypes |
| Processes | `ps -Ao pid,pcpu,rss,time,ucomm`; cpu % is CPU time used since the last sample |
| Memory | `vm_stat`; pressure from `kern.memorystatus_vm_pressure_level`; swap from `vm.swapusage` |
| Disk IO | IOKit `IOBlockStorageDriver` statistics (what `ioreg` prints), via ctypes |
| Network | `netstat -ibn` |
| Volumes | `df -k` (`/` and anything under `/Volumes`) |
| Thermal | `pmset -g therm` |

Notes:

- A process's CPU % is of one core, so a busy 8-thread process reads 800%.
- Memory per process is resident size (`rss`), which is not the "Memory" column
  in Activity Monitor (that is footprint, and includes compressed pages).
- "Used" memory is app + wired + compressed, as in Activity Monitor. Cached
  files are reported separately and are not counted as used.
- On Apple Silicon `pmset -g therm` usually reports no CPU speed limit at all.
  The dashboard then shows "Not reported" and no throttle event can fire.
- Per-process disk and network use, temperatures and fan speeds need sudo
  (`powermetrics`, `fs_usage`, `nettop` in part) and are not collected.
