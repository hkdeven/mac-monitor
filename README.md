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

Every 10 seconds, from a separate thread that can never hold up the sampler:

- Ping (4 packets each) to the router and to two public IPs, 1.1.1.1 and
  8.8.8.8: latency min / avg / max, jitter and loss per target
- A DNS lookup sent straight to the configured resolvers, so it is never
  answered from this Mac's cache
- Link: interface, Wi-Fi or Ethernet, and for Wi-Fi the signal, noise and tx rate
- Every 60 seconds: one small HTTPS request to Apple's connectivity check page

A week of history is roughly 200 MB.

## Internet

The Internet tile gives a plain verdict. When both public IPs stop answering it
says where the break is: "router reachable, ISP side" or "router unreachable
(Wi-Fi or local network)". The chart shows latency per target with loss and
outages shaded, and a summary for the selected range: uptime, outages, median
and worst latency, loss.

"Run speed test" runs macOS's `networkQuality` once (about 20 s, roughly 200 MB
of data). It only ever runs when clicked.

## Events

An event row is written, with the process lists at that moment, when:

| Event | Condition |
|---|---|
| Load above core count | 1-minute load average above the core count for 60 s (warning); raised to critical once it has stayed above 2x the core count for 60 s. Closes when load falls below 0.9x the core count |
| Memory pressure | kernel pressure level warn or critical |
| Internet down | both public IPs unreachable for 20 s; the title says which side of the router the break is on |
| Packet loss | 10% or more over 60 s while up (closes under 3%) |
| High latency / unsteady | median ping above 150 ms, or jitter above 50 ms, over 60 s |
| DNS failing / slow | half the lookups failing, or median above 500 ms, over 60 s while pings are fine |
| Swap growing | swap used up 256 MB within 60 s |
| Disk low | a volume under 10% free |
| CPU throttled | `CPU_Speed_Limit` below 100 |

One event covers the whole incident: it closes when the condition has been
clear for 60 s (the load event instead closes as soon as load is under its
lower line). High CPU % on its own is not an event. The thresholds are constants at the top of `monitor.py`.

## Notifications

When an event opens, one macOS banner is posted with what is wrong, the key
number, the likely culprit and the dashboard address, for example "Load above
core count: Load 41 on 15 cores - node x36". One more is posted if the event
turns critical, and "Internet back" with the outage length when an outage ends.
A rule that flaps open and closed posts at most once every 10 minutes.

The banners are posted with `osascript`, so macOS shows them under
**Script Editor**. If none appear, allow them in System Settings > Notifications
> Script Editor (Allow notifications, style Banners or Alerts), and check that
no Focus mode is hiding them. Clicking a banner opens Script Editor, not the
dashboard; the address is in the text.

The "Alerts" control in the dashboard switches them On, mutes them for an hour,
or turns them Off until switched back on; the choice survives restarts. "Send
test" posts one straight away, even while muted. `NOTIFY_ENABLED`,
`NOTIFY_RULES` (per rule) and `NOTIFY_COOLDOWN_S` are at the top of `monitor.py`.

## JSON

    /api/now                         latest sample, live process lists, open events
    /api/history?range=1h            15m | 1h | 6h | 24h | 7d, downsampled to ~360 points
    /api/history?range=1h&end=TS     the window ending at a unix time
    /api/events?limit=100&since=TS   events, newest first, with their process lists
    /api/speedtest                   speed test state and last result (POST with header X-Mac-Monitor: 1 starts one)
    /api/notifications               POST {"action": "on" | "off" | "mute" | "test", "seconds": 3600}, same header
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
| Internet | `/sbin/ping`, `route -n get default`, a UDP DNS query to the servers in `/etc/resolv.conf`, CoreWLAN via ctypes |
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
