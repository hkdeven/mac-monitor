# 🖥️ mac-monitor

Your Mac was slow at 3am. Who did it? mac-monitor was awake, and it took notes.

A small local monitor for one Mac: what the machine is doing now, and what it
was doing when things went wrong. Python 3 standard library only, no sudo,
nothing installed system-wide.

## 🚀 Quick start

```bash
git clone https://github.com/hkdeven/mac-monitor.git
cd mac-monitor
./run
```

Open the address it prints, normally http://127.0.0.1:5180/. Press Ctrl-C to
stop it.

Just want a quick look, with no server at all?

```bash
./snapshot
```

## ✨ What you get

- 📊 A live dashboard with charts for 15 minutes, 1 hour, 6 hours, 24 hours or 7 days
- 🕵️ The top processes at any moment in the past week, so you can see who was hogging the CPU
- 🌐 An internet verdict in plain words, which says which side of the router the break is on
- 🔔 A macOS banner when something goes wrong
- 📝 A plain-text report from `./snapshot`, handy for pasting into a chat or a ticket
- 🔒 Everything stays on your Mac: one SQLite file, one local web page

## 📋 Requirements

- 🍎 macOS. It reads Mac-only sources (`vm_stat`, `pmset`, IOKit, CoreWLAN), so
  it will not run on Linux or Windows. It was built and checked on Apple
  Silicon with Darwin 27. Other versions are untested.
- 🐍 Python 3.7 or newer, as `python3` on your `PATH`. The `python3` that comes
  with Apple's Command Line Tools works (3.9.6 here). No packages to install.
- 🙅 No sudo, ever.
- ⚡ The speed test button needs `/usr/bin/networkQuality`, which ships with
  macOS 12 and later. Without it, everything else still works.

## 🕹️ Use

| Command | What it does |
|---|---|
| `./run` | Start it in the foreground and print the URL (Ctrl-C stops it) |
| `./snapshot` | Print a plain-text report: now, last 24h peaks, recent events |

`./run` picks the first free port in 5180-5199, normally
http://127.0.0.1:5180/. To choose one yourself:

```bash
MAC_MONITOR_PORT=5190 ./run
```

If you set a port and it is busy, `./run` stops with an error. It does not fall
back to another port.

To leave it running after the terminal closes:

```bash
mkdir -p data
nohup ./run > data/server.log 2>&1 &
```

The `mkdir` matters on a fresh clone: `data/` does not exist until the first
run, and the log file goes in there.

Stop that with:

```bash
pkill -f "monitor.py serve"
```

It does not start at login, and the repo ships no LaunchAgent. History has a
gap for any time it was not running (the charts show the gap).

`./snapshot` takes its own 2 second reading and runs one round of the internet
checks, so it works when the server is down. It reads the history but adds
nothing to it.

The history lives in `data/monitor.db`. To keep it somewhere else, set
`MAC_MONITOR_DB` to a full path:

```bash
MAC_MONITOR_DB="$HOME/mac-monitor.db" ./run
```

Use the same setting with `./snapshot` so it reads the same file.

## 🔒 Privacy and network

- 🏠 The dashboard and the JSON API listen on 127.0.0.1 only. Other machines on
  your network cannot reach them.
- 📦 The dashboard is one self-contained HTML file. It loads no fonts, scripts
  or trackers from the internet.
- 💾 Everything recorded stays in `data/monitor.db` on your Mac. Nothing is
  uploaded anywhere. No account, no telemetry.
- 🛡️ Calls that change something (start a speed test, change alerts) must carry
  the header `X-Mac-Monitor: 1`, so another web page open in your browser
  cannot trigger them.

The only traffic that leaves the machine is the connectivity checks:

| Check | Where it goes | How often |
|---|---|---|
| Ping, 4 packets | 1.1.1.1 (Cloudflare) and 8.8.8.8 (Google). Your router is pinged too, but that stays on your local network | Every 10 s |
| DNS lookup of `www.apple.com` | Your own configured resolvers (the first two in `/etc/resolv.conf`), over UDP port 53 | Every 10 s |
| One small HTTPS request | `https://captive.apple.com/hotspot-detect.html`, Apple's connectivity check page | Every 60 s |
| Speed test (`networkQuality`) | Apple's speed test servers | Only when you click "Run speed test" |

`./snapshot` runs one round of the first three checks each time you run it. The
targets are constants at the top of `monitor.py` (`NET_TARGETS`, `NET_DNS_NAME`,
`NET_HTTP_URL`) if you want different ones.

## 📼 What is recorded

Every 5 seconds, into `data/monitor.db` (SQLite, gitignored, pruned to 7 days):

- 🧠 CPU: user / system / idle, per core, load averages
- 🏆 Top 10 processes by CPU and top 10 by memory (name, pid, cpu %, rss), plus
  the same totals grouped by process name
- 🧮 Memory: app / wired / compressed / cached / free, pressure level, swap
  used, swap in / out rate
- 💽 Disk read / write throughput, network bytes in / out per interface
- 🌡️ Every 30 seconds: free space per volume, `pmset -g therm`

Every 10 seconds, from a separate thread that can never hold up the sampler:

- 🏓 Ping (4 packets each) to the router and to two public IPs, 1.1.1.1 and
  8.8.8.8: latency min / avg / max, jitter and loss per target
- 📖 A DNS lookup sent straight to the configured resolvers, so it is never
  answered from this Mac's cache
- 📶 Link: interface, Wi-Fi or Ethernet, and for Wi-Fi the signal, noise and tx
  rate. The Wi-Fi network name is not read.
- 🍏 Every 60 seconds: one small HTTPS request to Apple's connectivity check
  page

A week of history is roughly 200 MB.

## 🌐 Internet

The Internet tile gives a plain verdict. When both public IPs stop answering it
says where the break is: "router reachable, ISP side", "router unreachable
(Wi-Fi or local network)", or "no network connection on this Mac" when there is
no default route at all. The chart shows latency per target with loss and
outages shaded, and a summary for the selected range: uptime, outages, median
and worst latency, loss.

"Run speed test" runs macOS's `networkQuality` once (about 20 s, roughly 200 MB
of data). It only ever runs when clicked. The regular checks pause while it
runs, so the test's own traffic is not recorded as loss.

## 🚨 Events

An event row is written, with the process lists at that moment, when:

| Event | Condition |
|---|---|
| Load above core count | 1-minute load average above the core count for 60 s (warning); raised to critical once it has stayed above 2x the core count for 60 s. Closes when load falls below 0.9x the core count |
| Memory pressure | kernel pressure level warn or critical |
| Internet down | both public IPs unreachable for 20 s; the title says which side of the router the break is on |
| Packet loss | 10% or more over 60 s while up (closes under 3%) |
| High latency / unsteady | median ping above 150 ms, or jitter above 50 ms, over 60 s (closes under 100 ms and 30 ms) |
| DNS failing / slow | half the lookups failing, or median above 500 ms, over 60 s while pings are fine (closes under a quarter failing and 300 ms) |
| Swap growing | swap used up 256 MB within 60 s |
| Disk low | a volume under 10% free (warning); critical under 5% |
| CPU throttled | `CPU_Speed_Limit` below 100 |

One event covers the whole incident. It closes when the condition has been
clear for a while: 60 s for memory, swap, disk and throttle events, 30 s for
the internet events. The load event instead closes as soon as load is under its
lower line. High CPU % on its own is not an event. The thresholds are constants
at the top of `monitor.py`.

## 🔔 Notifications

When an event opens, one macOS banner is posted with what is wrong, the key
number, the likely culprit and the dashboard address. The title starts with
"mac-monitor:", for example "mac-monitor: Load above core count" with the text
"Load 41 on 15 cores - node x36" and the address. One more is posted if the
event turns critical, and "Internet back" with the outage length when an outage
ends. A rule that flaps open and closed posts at most once every 10 minutes.

The banners are posted with `osascript`, so macOS shows them under
**Script Editor**. If none appear, allow them in System Settings > Notifications
> Script Editor (Allow notifications, style Banners or Alerts), and check that
no Focus mode is hiding them. Clicking a banner opens Script Editor, not the
dashboard. The address is in the text.

The "Alerts" control in the dashboard switches them On, mutes them for an hour,
or turns them Off until switched back on. The choice survives restarts. "Send
test" posts one straight away, even while muted. `NOTIFY_ENABLED`,
`NOTIFY_RULES` (per rule) and `NOTIFY_COOLDOWN_S` are at the top of `monitor.py`.

## 🧩 JSON

Everything the dashboard shows is also available as JSON:

```text
/api/now                         latest sample, live process lists, open events
/api/history?range=1h            15m | 1h | 6h | 24h | 7d, downsampled to ~360 points or fewer
/api/history?range=1h&end=TS     the window ending at a unix time
/api/events?limit=100&since=TS   events, newest first, with their process lists
/api/speedtest                   speed test state and last result (POST with header X-Mac-Monitor: 1 starts one)
/api/notifications               POST {"action": "on" | "off" | "mute" | "test", "seconds": 3600}, same header
/api/processes?at=TS             process lists recorded nearest that unix time
/api/processes?at=TS&window=240  the busiest sample within that many seconds after TS
```

Try it:

```bash
curl -s http://127.0.0.1:5180/api/now
```

Downsampled CPU carries both the average and the peak (`cpu_peak`) of each
bucket. `/api/processes` with no `at` returns the newest lists. For "mute",
`seconds` is kept between 60 and 7 days.

## 🔬 Where the numbers come from

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

- 🧵 A process's CPU % is of one core, so a busy 8-thread process reads 800%.
- 📏 Memory per process is resident size (`rss`), which is not the "Memory"
  column in Activity Monitor (that is footprint, and includes compressed pages).
- 🧮 "Used" memory is app + wired + compressed, as in Activity Monitor. Cached
  files are reported separately and are not counted as used.
- 🌡️ On Apple Silicon `pmset -g therm` usually reports no CPU speed limit at
  all. The dashboard then shows "Not reported" and no throttle event can fire.
- 🚫 Per-process disk and network use, temperatures and fan speeds need sudo
  (`powermetrics`, `fs_usage`, `nettop` in part) and are not collected.

## 🧹 Stop or uninstall

Stop it with Ctrl-C in its terminal, or, if you started it with `nohup`:

```bash
pkill -f "monitor.py serve"
```

To uninstall, stop it and delete the folder. The history is in `data/` inside
that folder (or wherever you pointed `MAC_MONITOR_DB`), so it goes too. Nothing
else was installed, so there is nothing else to clean up.

## 📜 Licence

MIT. See [LICENSE](LICENSE).
