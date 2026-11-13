#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# angie-top - терминальный аналог Angie Console Light поверх REST API (/status/).
# Зависимости: только стандартная библиотека Python 3.8+ (curses, urllib).
# Структура JSON соответствует документации модуля http_api Angie 1.12.
#
#   angie-top -u http://127.0.0.1:8080/status/
#   angie-top -u https://host/status/ --user mon --password secret -k
#   angie-top -u http://127.0.0.1:8080/status/ --once --no-color   # снимок для скриптов

import argparse
import base64
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone

__version__ = "1.0.0"

# ---------- стили (не зависят от curses, чтобы рендер можно было тестировать) ----------
NORMAL, DIM, BOLD, GREEN, RED, YELLOW, CYAN, MAGENTA, TITLE = range(9)

SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
FULL, EMPTY = "\u2588", "\u2591"

# Листья-«гейджи»: для них история хранит значение, а не скорость.
GAUGES = {
    "processing", "current", "active", "idle", "keepalive", "size", "used",
    "free", "weight", "max_conns", "generation", "cold", "waiting",
    "header_time", "response_time", "connect_time", "first_byte_time",
    "last_byte_time",
}

STATE_STYLE = {
    "up": GREEN, "unavailable": RED, "unhealthy": RED, "down": DIM,
    "draining": YELLOW, "recovering": YELLOW, "busy": YELLOW, "checking": CYAN,
}


def use_ascii():
    global SPARK, FULL, EMPTY
    SPARK, FULL, EMPTY = "_.-~*#%@", "#", "."


# ---------- форматирование ----------
def human(n, prec=1):
    n = float(n)
    for u in ("", "k", "M", "G", "T"):
        if abs(n) < 1000 or u == "T":
            if not u and abs(n) >= 100:
                return f"{n:.0f}"
            if not u and n == int(n):
                return f"{int(n)}"
            return f"{n:.{prec}f}{u}"
        n /= 1000.0


def bh(n):  # байты -> человекочитаемо
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f}B" if u == "B" else f"{n:.1f}{u}"
        n /= 1024.0


def dur(ms):
    ms = float(ms)
    if ms < 1000:
        return f"{ms:.0f}ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def spark(vals, width):
    v = list(vals)[-width:]
    if not v:
        return ""
    mx = max(v)
    if mx <= 0:
        return SPARK[0] * len(v)
    return "".join(SPARK[0 if x <= 0 else max(1, min(7, int(x / mx * 7 + 0.5)))] for x in v)


def bar(used, total, width):
    if total <= 0:
        return EMPTY * width
    f = max(0, min(width, int(round(used / total * width))))
    return FULL * f + EMPTY * (width - f)


def cut(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def parse_t(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(dt):
    if dt is None:
        return "?"
    s = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


# ---------- модель: два снимка -> скорости + история для спарклайнов ----------
class Model:
    def __init__(self, history=120):
        self.data = {}
        self.prev = None            # (monotonic, data)
        self.rates = {}             # tuple(path) -> скорость в секунду
        self.hist = {}              # tuple(path) -> deque
        self.history = history
        self.error = None
        self.ok_time = None

    def _series(self, path):
        s = self.hist.get(path)
        if s is None:
            s = self.hist[path] = deque(maxlen=self.history)
        return s

    @staticmethod
    def _gen(data):
        a = data.get("angie") or {}
        return (a.get("generation"), a.get("load_time"))

    def _walk(self, path, old, new, dt, rates):
        for k, nv in new.items():
            p = path + (k,)
            if isinstance(nv, dict):
                ov = old.get(k)
                if isinstance(ov, dict):
                    self._walk(p, ov, nv, dt, rates)
            elif isinstance(nv, (int, float)) and not isinstance(nv, bool):
                if k in GAUGES:
                    continue
                ov = old.get(k)
                if isinstance(ov, (int, float)) and nv >= ov:
                    rates[p] = (nv - ov) / dt
                # nv < ov: счётчик сброшен (reload/рестарт) - скорость пропускаем

    def _gauges(self, path, d):
        for k, v in d.items():
            p = path + (k,)
            if isinstance(v, dict):
                self._gauges(p, v)
            elif k in GAUGES and isinstance(v, (int, float)) and not isinstance(v, bool):
                self._series(p).append(v)

    def _aggregate(self, rates):
        agg = {"rps": 0.0, "2": 0.0, "3": 0.0, "4": 0.0, "5": 0.0,
               "disc": 0.0, "rx": 0.0, "tx": 0.0}
        for name, z in ((self.data.get("http") or {}).get("server_zones") or {}).items():
            b = ("http", "server_zones", name)
            agg["rps"] += rates.get(b + ("requests", "total"), 0.0)
            agg["disc"] += rates.get(b + ("requests", "discarded"), 0.0)
            agg["rx"] += rates.get(b + ("data", "received"), 0.0)
            agg["tx"] += rates.get(b + ("data", "sent"), 0.0)
            for code in (z.get("responses") or {}):
                if code.isdigit() and code[0] in "2345":
                    agg[code[0]] += rates.get(b + ("responses", code), 0.0)
        for k, v in agg.items():
            rates[("_", "http", k)] = v
        sa = {"cps": 0.0, "rx": 0.0, "tx": 0.0, "ok": 0.0, "err": 0.0}
        for name, z in ((self.data.get("stream") or {}).get("server_zones") or {}).items():
            b = ("stream", "server_zones", name)
            sa["cps"] += rates.get(b + ("connections", "total"), 0.0)
            sa["rx"] += rates.get(b + ("data", "received"), 0.0)
            sa["tx"] += rates.get(b + ("data", "sent"), 0.0)
            for k in (z.get("sessions") or {}):
                r = rates.get(b + ("sessions", k), 0.0)
                sa["ok" if k == "success" else "err"] += r
        for k, v in sa.items():
            rates[("_", "stream", k)] = v

    def update(self, data):
        now = time.monotonic()
        rates = {}
        if self.prev is not None and self._gen(self.prev[1]) == self._gen(data):
            dt = now - self.prev[0]
            if dt > 0:
                self._walk((), self.prev[1], data, dt, rates)
        self.data = data
        self._aggregate(rates)
        self._gauges((), data)
        for p, v in rates.items():
            self._series(p).append(v)
        self.prev = (now, data)
        self.rates = rates
        self.error = None
        self.ok_time = time.time()

    def rate(self, *path):
        return self.rates.get(tuple(path), 0.0)

    def series(self, *path):
        return self.hist.get(tuple(path), ())

    def val(self, *path, default=0):
        d = self.data
        for k in path:
            if not isinstance(d, dict) or k not in d:
                return default
            d = d[k]
        return d


# ---------- сеть ----------
def norm_url(u):
    return u if u.endswith("/") else u + "/"


def build_headers(args):
    h = {"Accept": "application/json", "User-Agent": f"angie-top/{__version__}"}
    if args.user:
        tok = base64.b64encode(f"{args.user}:{args.password or ''}".encode()).decode()
        h["Authorization"] = "Basic " + tok
    for raw in args.header or []:
        if ":" in raw:
            k, v = raw.split(":", 1)
            h[k.strip()] = v.strip()
    return h


def fetch_status(url, timeout=3.0, headers=None, insecure=False):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(url + sep + "pretty=off", headers=headers or {})
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(body)
            raise RuntimeError(f"HTTP {e.code}: {j.get('error')}: {j.get('description')}")
        except ValueError:
            raise RuntimeError(f"HTTP {e.code}: {body[:120]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"connection failed: {e.reason}")
    except OSError as e:
        raise RuntimeError(f"connection failed: {e}")
    except ValueError:
        raise RuntimeError("invalid JSON in API response")
    if isinstance(data, dict) and set(data) == {"error", "description"}:
        raise RuntimeError(f"API error: {data['error']}: {data['description']}")
    return data


# ---------- helpers рендера: строка = список сегментов (текст, стиль) ----------
def T(t, s=NORMAL):
    return (str(t), s)


def L(*segs):
    return list(segs)


def blank():
    return [T("")]


def hdr(t):
    return L(T(t, TITLE))


def zones(m, *base):
    d = m.val(*base, default={})
    return d if isinstance(d, dict) else {}


def cls_rate(m, base, cls):
    r = m.val(*base, "responses", default={}) or {}
    return sum(m.rate(*base, "responses", k) for k in r if k.isdigit() and k[0] == cls)


def cls_total(zone, cls):
    return sum(v for k, v in (zone.get("responses") or {}).items()
               if k.isdigit() and k[0] == cls)


# ---------- вкладки ----------
def render_overview(m, w):
    out = []
    a = zones(m, "angie")
    out.append(L(T(f"Angie {a.get('version', '?')}", BOLD),
                 T(f"   address {a.get('address', '?')}   generation {a.get('generation', '?')}"
                   f"   reloaded {ago(parse_t(a.get('load_time')))} ago", DIM)))
    out.append(blank())
    c = zones(m, "connections")
    sw = 24
    out.append(hdr("CONNECTIONS"))
    out.append(L(T("  active   "), T(f"{c.get('active', 0):>7}", BOLD),
                 T("  " + spark(m.series("connections", "active"), sw), CYAN),
                 T(f"   idle {c.get('idle', 0)}", DIM)))
    dr = m.rate("connections", "dropped")
    out.append(L(T("  accepted "), T(human(c.get("accepted", 0))),
                 T(f" (+{human(m.rate('connections', 'accepted'))}/s)"),
                 T(" " + spark(m.series("connections", "accepted"), sw), CYAN)))
    out.append(L(T("  dropped  "), T(human(c.get("dropped", 0)), RED if dr > 0 else NORMAL),
                 T(f" (+{human(dr)}/s)", RED if dr > 0 else DIM)))
    sz = zones(m, "http", "server_zones")
    if sz:
        out.append(blank())
        out.append(hdr(f"HTTP  (server zones: {len(sz)})"))
        out.append(L(T("  RPS      "), T(human(m.rate("_", "http", "rps")), BOLD), T("/s"),
                     T("  " + spark(m.series("_", "http", "rps"), sw), CYAN),
                     T(f"   in {bh(m.rate('_', 'http', 'rx'))}/s  out {bh(m.rate('_', 'http', 'tx'))}/s", DIM)))
        r5 = m.rate("_", "http", "5")
        out.append(L(T(f"  2xx/s {human(m.rate('_', 'http', '2'))}   "
                       f"3xx/s {human(m.rate('_', 'http', '3'))}   "
                       f"4xx/s {human(m.rate('_', 'http', '4'))}   "),
                     T(f"5xx/s {human(r5)}", RED if r5 > 0 else GREEN),
                     T(f"   discarded/s {human(m.rate('_', 'http', 'disc'))}", DIM)))
    st = zones(m, "stream", "server_zones")
    if st:
        out.append(blank())
        out.append(hdr(f"STREAM  (server zones: {len(st)})"))
        er = m.rate("_", "stream", "err")
        out.append(L(T(f"  conn/s {human(m.rate('_', 'stream', 'cps'))}   "
                       f"ok/s {human(m.rate('_', 'stream', 'ok'))}   "),
                     T(f"err/s {human(er)}", RED if er > 0 else GREEN),
                     T(f"   in {bh(m.rate('_', 'stream', 'rx'))}/s  out {bh(m.rate('_', 'stream', 'tx'))}/s", DIM)))
    out.append(blank())
    out.append(hdr("ALERTS"))
    alerts = collect_alerts(m)
    if alerts:
        for style, msg in alerts[:8]:
            out.append(L(T("  ! "), T(msg, style)))
    else:
        out.append(L(T("  no alerts", GREEN)))
    if sz:
        out.append(blank())
        out.append(hdr("TOP ZONES BY RPS"))
        top = sorted(sz, key=lambda z: m.rate("http", "server_zones", z, "requests", "total"),
                     reverse=True)[:5]
        for z in top:
            b = ("http", "server_zones", z)
            r5 = cls_rate(m, b, "5")
            out.append(L(T(f"  {cut(z, 24):<24}"),
                         T(f"{human(m.rate(*b, 'requests', 'total')):>8}/s"),
                         T(" " + spark(m.series(*b, "requests", "total"), sw), CYAN),
                         T(f"  5xx/s {human(r5)}", RED if r5 > 0 else DIM)))
    return out


def collect_alerts(m):
    al = []
    r = m.rate("connections", "dropped")
    if r > 0:
        al.append((RED, f"connections dropped +{human(r)}/s - check worker_connections / nofile"))
    r = m.rate("_", "http", "5")
    if r > 0:
        al.append((RED, f"HTTP 5xx responses +{human(r)}/s"))
    for un, u in zones(m, "http", "upstreams").items():
        for addr, p in (u.get("peers") or {}).items():
            st = p.get("state")
            if st in ("unavailable", "unhealthy"):
                al.append((RED, f"upstream {un}: peer {addr} is {st} "
                                f"(fails={p.get('health', {}).get('fails', 0)})"))
            elif st in ("down", "draining", "busy", "recovering"):
                al.append((YELLOW, f"upstream {un}: peer {addr} is {st}"))
    for un, u in zones(m, "stream", "upstreams").items():
        for addr, p in (u.get("peers") or {}).items():
            st = p.get("state")
            if st in ("unavailable", "unhealthy"):
                al.append((RED, f"stream upstream {un}: peer {addr} is {st}"))
    for zn, z in zones(m, "slabs").items():
        fails = sum(s.get("fails", 0) for s in (z.get("slots") or {}).values())
        if fails:
            al.append((RED, f"slab zone {zn}: {fails} allocation fails - enlarge zone"))
    for zn in zones(m, "http", "limit_reqs"):
        r = m.rate("http", "limit_reqs", zn, "rejected")
        if r > 0:
            al.append((YELLOW, f"limit_req {zn}: rejected +{human(r)}/s"))
    for zn, z in zones(m, "http", "server_zones").items():
        r = m.rate("http", "server_zones", zn, "ssl", "failed")
        if r > 0:
            al.append((YELLOW, f"ssl {zn}: failed handshakes +{human(r)}/s"))
    for zn in zones(m, "resolvers"):
        r = m.rate("resolvers", zn, "responses", "timedout")
        if r > 0:
            al.append((YELLOW, f"resolver {zn}: timeouts +{human(r)}/s"))
    return al


SZ_LEFT = "  {:<20}{:>9}{:>6}{:>8}{:>8}{:>8}{:>8}"
SZ_RIGHT = "{:>8}{:>10}{:>10}"
SZ_HEADER = (SZ_LEFT.format("ZONE", "RPS", "PROC", "DISC/s", "2xx/s", "3xx/s", "4xx/s")
             + SZ_RIGHT.format("5xx/s", "RX/s", "TX/s"))


def _zone_row(m, b, zn, proc):
    r5 = cls_rate(m, b, "5")
    left = SZ_LEFT.format(cut(zn, 20), human(m.rate(*b, "requests", "total")), proc,
                          human(m.rate(*b, "requests", "discarded")),
                          human(cls_rate(m, b, "2")), human(cls_rate(m, b, "3")),
                          human(cls_rate(m, b, "4")))
    right = (f"{bh(m.rate(*b, 'data', 'received')):>10}"
             f"{bh(m.rate(*b, 'data', 'sent')):>10}")
    return L(T(left), T(f"{human(r5):>8}", RED if r5 > 0 else NORMAL), T(right))


def render_http(m, w):
    out = [hdr("HTTP SERVER ZONES")]
    sz = zones(m, "http", "server_zones")
    if not sz:
        out.append(L(T("  (no server zones; add status_zone to server{})", DIM)))
    else:
        out.append(L(T(SZ_HEADER, DIM)))
        for zn, z in sorted(sz.items()):
            b = ("http", "server_zones", zn)
            out.append(_zone_row(m, b, zn, z.get("requests", {}).get("processing", 0)))
    ssl_zones = [(zn, z) for zn, z in sorted(sz.items()) if z.get("ssl")]
    if ssl_zones:
        out.append(blank())
        out.append(hdr("SSL"))
        h = "  {:<20}{:>9}{:>9}{:>10}{:>10}"
        out.append(L(T(h.format("ZONE", "HS/s", "REUSE%", "FAILED/s", "TMO/s"), DIM)))
        for zn, z in ssl_zones:
            b = ("http", "server_zones", zn, "ssl")
            hs, ru = m.rate(*b, "handshaked"), m.rate(*b, "reuses")
            pct = 100.0 * ru / (hs + ru) if hs + ru > 0 else 0.0
            fl = m.rate(*b, "failed")
            out.append(L(T(h.format(cut(zn, 20), human(hs), f"{pct:.1f}",
                                    human(fl), human(m.rate(*b, "timedout"))),
                         RED if fl > 0 else NORMAL)))
    lz = zones(m, "http", "location_zones")
    if lz:
        out.append(blank())
        out.append(hdr("HTTP LOCATION ZONES"))
        out.append(L(T(SZ_HEADER, DIM)))
        for zn, z in sorted(lz.items()):
            b = ("http", "location_zones", zn)
            out.append(_zone_row(m, b, zn, "-"))
    return out


UP_HDR = "  {:<22}{:<12}{:>9}{:>8}{:>8}{:>7}{:>7}{:>7}{:>6}{:>9}"


def render_upstreams(m, w):
    out = [hdr("HTTP UPSTREAMS")]
    ups = zones(m, "http", "upstreams")
    if not ups:
        out.append(L(T("  (no upstreams; add 'zone' to upstream{})", DIM)))
    for un, u in sorted(ups.items()):
        peers = u.get("peers") or {}
        nup = sum(1 for p in peers.values() if p.get("state") == "up")
        st = GREEN if peers and nup == len(peers) else (RED if nup == 0 else YELLOW)
        out.append(blank())
        out.append(L(T(f"\u25b2 {un}", BOLD), T(f"  peers {nup}/{len(peers)} up", st),
                     T(f"  keepalive {u.get('keepalive', 0)}", DIM)))
        out.append(L(T(UP_HDR.format("PEER", "STATE", "CUR/MAX", "SEL/s", "2xx/s",
                                     "4xx/s", "5xx/s", "FAILS", "UNAVL", "DOWNTIME"), DIM)))
        for addr, p in sorted(peers.items()):
            b = ("http", "upstreams", un, "peers", addr)
            state = p.get("state", "?")
            sel = p.get("selected", {})
            cur = f"{sel.get('current', 0)}/{p.get('max_conns', '-')}"
            h = p.get("health", {})
            r5 = cls_rate(m, b, "5")
            fails = h.get("fails", 0)
            out.append(L(
                T(f"  {cut(addr, 22):<22}"),
                T(f"{state:<12}", STATE_STYLE.get(state, NORMAL)),
                T(f"{cur:>9}{human(m.rate(*b, 'selected', 'total')):>8}"
                  f"{human(cls_rate(m, b, '2')):>8}{human(cls_rate(m, b, '4')):>7}"),
                T(f"{human(r5):>7}", RED if r5 > 0 else NORMAL),
                T(f"{fails:>7}", RED if fails > 0 else NORMAL),
                T(f"{h.get('unavailable', 0):>6}{dur(h.get('downtime', 0)):>9}")))
            extra = (f"      \u21b3 {p.get('server', '')}  w={p.get('weight', 1)}"
                     + ("  backup" if p.get("backup") else ""))
            if "header_time" in h or "response_time" in h:
                extra += f"  header {h.get('header_time', '-')}ms  resp {h.get('response_time', '-')}ms"
            extra += (f"  rx {bh(m.rate(*b, 'data', 'received'))}/s"
                      f"  tx {bh(m.rate(*b, 'data', 'sent'))}/s")
            if h.get("downstart"):
                extra += f"  down since {h['downstart']}"
            out.append(L(T(extra, DIM)))
        q = u.get("queue")
        if q:
            out.append(L(T(f"      queue: waiting {q.get('waiting', 0)}  queued {q.get('queued', 0)}"
                           f"  overflows {q.get('overflows', 0)}  timedout {q.get('timedout', 0)}", YELLOW)))
    return out


def render_cache(m, w):
    out = [hdr("HTTP CACHES")]
    cs = zones(m, "http", "caches")
    if not cs:
        out.append(L(T("  (no caches)", DIM)))
    else:
        h = "  {:<18}{:>10}{:>6}{:>8}{:>8}{:>8}{:>8}{:>8}"
        out.append(L(T(h.format("ZONE", "SIZE", "COLD", "HIT/s", "MISS/s", "EXP/s",
                                "BYP/s", "HIT%"), DIM)))
        for zn, z in sorted(cs.items()):
            b = ("http", "caches", zn)
            tot = sum((z.get(k) or {}).get("responses", 0) for k in
                      ("hit", "miss", "expired", "stale", "updating", "revalidated", "bypass"))
            pct = 100.0 * (z.get("hit") or {}).get("responses", 0) / tot if tot else 0.0
            out.append(L(T(h.format(cut(zn, 18), bh(z.get("size", 0)),
                                    "yes" if z.get("cold") else "no",
                                    human(m.rate(*b, "hit", "responses")),
                                    human(m.rate(*b, "miss", "responses")),
                                    human(m.rate(*b, "expired", "responses")),
                                    human(m.rate(*b, "bypass", "responses")),
                                    f"{pct:.1f}"))))
    lc = zones(m, "http", "limit_conns")
    if lc:
        out.append(blank())
        out.append(hdr("LIMIT_CONN ZONES"))
        h = "  {:<20}{:>10}{:>10}{:>11}{:>11}"
        out.append(L(T(h.format("ZONE", "PASS/s", "SKIP/s", "REJECT/s", "EXHAUST/s"), DIM)))
        for zn in sorted(lc):
            b = ("http", "limit_conns", zn)
            rj = m.rate(*b, "rejected")
            out.append(L(T(h.format(cut(zn, 20), human(m.rate(*b, "passed")),
                                    human(m.rate(*b, "skipped")), human(rj),
                                    human(m.rate(*b, "exhausted"))),
                         YELLOW if rj > 0 else NORMAL)))
    lr = zones(m, "http", "limit_reqs")
    if lr:
        out.append(blank())
        out.append(hdr("LIMIT_REQ ZONES"))
        h = "  {:<20}{:>10}{:>10}{:>11}{:>11}"
        out.append(L(T(h.format("ZONE", "PASS/s", "DELAY/s", "REJECT/s", "EXHAUST/s"), DIM)))
        for zn in sorted(lr):
            b = ("http", "limit_reqs", zn)
            rj = m.rate(*b, "rejected")
            out.append(L(T(h.format(cut(zn, 20), human(m.rate(*b, "passed")),
                                    human(m.rate(*b, "delayed")), human(rj),
                                    human(m.rate(*b, "exhausted"))),
                         YELLOW if rj > 0 else NORMAL)))
    return out


def render_memory(m, w):
    out = [hdr("SHARED MEMORY ZONES (slabs)")]
    sl = zones(m, "slabs")
    if not sl:
        out.append(L(T("  (no slab zones)", DIM)))
    else:
        h = "  {:<20}{:>14}  {:<22}{:>7}{:>9}"
        out.append(L(T(h.format("ZONE", "PAGES u/t", "USAGE", "USE%", "SLOTFAIL"), DIM)))
        for zn, z in sorted(sl.items()):
            pg = z.get("pages", {})
            used, free = pg.get("used", 0), pg.get("free", 0)
            tot = used + free
            pct = 100.0 * used / tot if tot else 0.0
            fails = sum(s.get("fails", 0) for s in (z.get("slots") or {}).values())
            out.append(L(
                T(f"  {cut(zn, 20):<20}{f'{used}/{tot}':>14}  "),
                T(bar(used, tot, 22), RED if pct > 90 else GREEN),
                T(f"{pct:>6.1f}%"),
                T(f"{fails:>9}", RED if fails else NORMAL)))
    rs = zones(m, "resolvers")
    if rs:
        out.append(blank())
        out.append(hdr("RESOLVERS"))
        h = "  {:<16}{:>8}{:>7}{:>7}{:>8}{:>8}{:>7}"
        out.append(L(T(h.format("ZONE", "NAME/s", "SRV/s", "ADDR/s", "OK/s", "TMO/s", "ERR/s"), DIM)))
        for zn, z in sorted(rs.items()):
            b = ("resolvers", zn)
            resp = z.get("responses") or {}
            err = sum(m.rate(*b, "responses", k) for k in resp
                      if k not in ("success", "timedout"))
            tmo = m.rate(*b, "responses", "timedout")
            out.append(L(T(h.format(cut(zn, 16), human(m.rate(*b, "queries", "name")),
                                    human(m.rate(*b, "queries", "srv")),
                                    human(m.rate(*b, "queries", "addr")),
                                    human(m.rate(*b, "responses", "success")),
                                    human(tmo), human(err)),
                         YELLOW if tmo + err > 0 else NORMAL)))
    return out


def render_stream(m, w):
    out = [hdr("STREAM SERVER ZONES")]
    sz = zones(m, "stream", "server_zones")
    if not sz:
        out.append(L(T("  (no stream server zones)", DIM)))
    else:
        h = "  {:<18}{:>8}{:>6}{:>8}{:>8}{:>8}{:>8}{:>10}{:>10}"
        out.append(L(T(h.format("ZONE", "CONN/s", "PROC", "DISC/s", "PASS/s",
                                "OK/s", "ERR/s", "RX/s", "TX/s"), DIM)))
        for zn, z in sorted(sz.items()):
            b = ("stream", "server_zones", zn)
            sess = z.get("sessions") or {}
            err = sum(m.rate(*b, "sessions", k) for k in sess if k != "success")
            out.append(L(T(h.format(cut(zn, 18), human(m.rate(*b, "connections", "total")),
                                    z.get("connections", {}).get("processing", 0),
                                    human(m.rate(*b, "connections", "discarded")),
                                    human(m.rate(*b, "connections", "passed")),
                                    human(m.rate(*b, "sessions", "success")),
                                    human(err),
                                    bh(m.rate(*b, "data", "received")),
                                    bh(m.rate(*b, "data", "sent"))),
                         RED if err > 0 else NORMAL)))
    ups = zones(m, "stream", "upstreams")
    if ups:
        h = "  {:<22}{:<12}{:>9}{:>8}{:>7}{:>6}{:>9}"
        for un, u in sorted(ups.items()):
            peers = u.get("peers") or {}
            nup = sum(1 for p in peers.values() if p.get("state") == "up")
            st = GREEN if peers and nup == len(peers) else (RED if nup == 0 else YELLOW)
            out.append(blank())
            out.append(L(T(f"\u25b2 {un}", BOLD), T(f"  peers {nup}/{len(peers)} up", st)))
            out.append(L(T(h.format("PEER", "STATE", "CUR/MAX", "CONN/s", "FAILS",
                                    "UNAVL", "DOWNTIME"), DIM)))
            for addr, p in sorted(peers.items()):
                b = ("stream", "upstreams", un, "peers", addr)
                state = p.get("state", "?")
                sel = p.get("selected", {})
                hh = p.get("health", {})
                cur = f"{sel.get('current', 0)}/{p.get('max_conns', '-')}"
                out.append(L(
                    T(f"  {cut(addr, 22):<22}"),
                    T(f"{state:<12}", STATE_STYLE.get(state, NORMAL)),
                    T(f"{cur:>9}{human(m.rate(*b, 'selected', 'total')):>8}"
                      f"{hh.get('fails', 0):>7}{hh.get('unavailable', 0):>6}"
                      f"{dur(hh.get('downtime', 0)):>9}")))
                extra = (f"      \u21b3 {p.get('server', '')}  connect {hh.get('connect_time', '-')}ms"
                         f"  first {hh.get('first_byte_time', '-')}ms  last {hh.get('last_byte_time', '-')}ms"
                         f"  rx {bh(m.rate(*b, 'data', 'received'))}/s"
                         f"  tx {bh(m.rate(*b, 'data', 'sent'))}/s")
                out.append(L(T(extra, DIM)))
    return out


TABS = [
    ("Overview", render_overview),
    ("HTTP zones", render_http),
    ("Upstreams", render_upstreams),
    ("Cache&Limits", render_cache),
    ("Memory&DNS", render_memory),
    ("Stream", render_stream),
]

HELP = [
    "1..6        switch tab (Overview / HTTP / Upstreams / Cache / Memory / Stream)",
    "Tab, <- ->  next / previous tab",
    "Up/Down j/k scroll, PgUp/PgDn, g/G top/bottom",
    "Space       pause/resume polling",
    "+ / -       poll interval -/+ 0.5s (0.5..60s)",
    "r           force refresh now, reset scroll",
    "? or h      this help",
    "q           quit",
    "",
    "Rates are computed from counter deltas between polls.",
    "On config reload (generation change) or counter reset rates are skipped for one tick.",
    "Data source: Angie http_api module, GET /status/?pretty=off",
]


# ---------- режим --once: текстовый снимок (для SSH, cron, goose) ----------
ANSI = {NORMAL: "", DIM: "\x1b[2m", BOLD: "\x1b[1m", GREEN: "\x1b[32m",
        RED: "\x1b[1;31m", YELLOW: "\x1b[33m", CYAN: "\x1b[36m",
        MAGENTA: "\x1b[35m", TITLE: "\x1b[1;36m"}


def run_once(args, model, headers):
    try:
        model.update(fetch_status(args.url, args.timeout, headers, args.insecure))
        if not args.snapshot:
            time.sleep(max(0.1, args.interval))
            model.update(fetch_status(args.url, args.timeout, headers, args.insecure))
    except RuntimeError as e:
        print(f"angie-top: {e}", file=sys.stderr)
        return 1
    color = sys.stdout.isatty() and not args.no_color
    tabs = [TABS[args.tab - 1]] if args.tab else TABS
    lines = [L(T(f"angie-top {__version__}  {args.url}  {datetime.now():%Y-%m-%d %H:%M:%S}", TITLE))]
    for name, fn in tabs:
        lines.append(blank())
        lines.append(L(T(f"===== {name} " + "=" * max(0, args.width - 12 - len(name)), DIM)))
        lines.extend(fn(model, args.width))
    try:
        for line in lines:
            if color:
                print("".join(ANSI[s] + t + ("\x1b[0m" if s else "") for t, s in line))
            else:
                print("".join(t for t, _ in line))
    except BrokenPipeError:
        pass
    return 0


# ---------- интерактивный TUI (curses) ----------
def run_tui(args, model, headers):
    try:
        import curses
    except ImportError:
        print("curses unavailable; use --once for plain-text mode", file=sys.stderr)
        return 1

    state = {"tab": (args.tab - 1) if args.tab else 0, "scroll": {}, "paused": False,
             "interval": max(0.5, args.interval), "help": False,
             "next_due": 0.0, "thread": None, "box": None}

    def poll():
        try:
            data = fetch_status(args.url, args.timeout, headers, args.insecure)
            state["box"] = ("ok", data)
        except Exception as e:
            state["box"] = ("err", str(e))

    def loop(scr):
        curses.curs_set(0)
        scr.nodelay(True)
        smap = {NORMAL: 0, DIM: curses.A_DIM, BOLD: curses.A_BOLD, TITLE: curses.A_BOLD}
        if curses.has_colors():
            curses.use_default_colors()
            for i, c in enumerate([curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW,
                                   curses.COLOR_CYAN, curses.COLOR_MAGENTA], 1):
                curses.init_pair(i, c, -1)
            smap.update({GREEN: curses.color_pair(1), RED: curses.color_pair(2) | curses.A_BOLD,
                         YELLOW: curses.color_pair(3), CYAN: curses.color_pair(4),
                         MAGENTA: curses.color_pair(5), TITLE: curses.color_pair(4) | curses.A_BOLD})

        def put(y, x, s, attr=0):
            h, w = scr.getmaxyx()
            if y < 0 or y >= h or x >= w or not s:
                return
            try:
                scr.addstr(y, x, s[: max(0, w - x - 1)], attr)
            except curses.error:
                pass

        while True:
            # --- клавиатура ---
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                if ch in (ord("q"), ord("Q")):
                    return
                if ord("1") <= ch <= ord(str(len(TABS))):
                    state["tab"] = ch - ord("1")
                elif ch in (9, curses.KEY_RIGHT):
                    state["tab"] = (state["tab"] + 1) % len(TABS)
                elif ch == curses.KEY_LEFT:
                    state["tab"] = (state["tab"] - 1) % len(TABS)
                elif ch == ord(" "):
                    state["paused"] = not state["paused"]
                elif ch in (ord("+"), ord("=")):
                    state["interval"] = max(0.5, state["interval"] - 0.5)
                elif ch == ord("-"):
                    state["interval"] = min(60.0, state["interval"] + 0.5)
                elif ch in (ord("?"), ord("h")):
                    state["help"] = not state["help"]
                elif ch == ord("r"):
                    state["next_due"] = 0.0
                    state["scroll"] = {}
                elif ch in (curses.KEY_UP, ord("k")):
                    t = state["tab"]
                    state["scroll"][t] = max(0, state["scroll"].get(t, 0) - 1)
                elif ch in (curses.KEY_DOWN, ord("j")):
                    t = state["tab"]
                    state["scroll"][t] = state["scroll"].get(t, 0) + 1
                elif ch == curses.KEY_PPAGE:
                    t = state["tab"]
                    state["scroll"][t] = max(0, state["scroll"].get(t, 0) - 10)
                elif ch == curses.KEY_NPAGE:
                    t = state["tab"]
                    state["scroll"][t] = state["scroll"].get(t, 0) + 10
                elif ch == ord("g"):
                    state["scroll"][state["tab"]] = 0
                elif ch == ord("G"):
                    state["scroll"][state["tab"]] = 10 ** 6
            # --- фоновый опрос, чтобы UI не подвисал на сети ---
            now = time.monotonic()
            if state["thread"] is None and not state["paused"] and now >= state["next_due"]:
                state["thread"] = threading.Thread(target=poll, daemon=True)
                state["thread"].start()
            if state["box"] is not None:
                kind, payload = state["box"]
                state["box"] = None
                state["thread"] = None
                if kind == "ok":
                    model.update(payload)
                else:
                    model.error = payload
                state["next_due"] = time.monotonic() + state["interval"]
            # --- отрисовка ---
            scr.erase()
            h, w = scr.getmaxyx()
            put(0, 0, f" angie-top {__version__} ", smap[TITLE] | curses.A_REVERSE)
            put(0, 18, cut(args.url, max(10, w - 40)), smap[DIM])
            put(0, max(0, w - 21), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), smap[BOLD])
            x = 0
            for i, (name, _) in enumerate(TABS):
                label = f" {i + 1} {name} "
                put(1, x, label, (smap[TITLE] | curses.A_REVERSE) if i == state["tab"] else smap[DIM])
                x += len(label) + 1
            status = f"[{state['interval']:.1f}s]"
            if state["paused"]:
                status += " PAUSED"
            if model.error:
                status += " ERR"
            elif model.ok_time:
                status += f" upd {int(time.time() - model.ok_time)}s"
            put(1, max(x, w - len(status) - 1), status,
                smap[RED] if model.error else (smap[YELLOW] if state["paused"] else smap[GREEN]))
            lines = [L(T(s, DIM)) for s in HELP] if state["help"] else TABS[state["tab"]][1](model, w)
            body_h = max(1, h - 3)
            max_scroll = max(0, len(lines) - body_h)
            sc = min(state["scroll"].get(state["tab"], 0), max_scroll)
            state["scroll"][state["tab"]] = sc
            for i, line in enumerate(lines[sc: sc + body_h]):
                x = 0
                for text, style in line:
                    put(2 + i, x, text, smap[style])
                    x += len(text)
                    if x >= w:
                        break
            footer = (f"ERROR: {model.error}" if model.error else
                      "1-6 tabs  \u2190\u2192 switch  \u2191\u2193 scroll  space pause  +/- interval  r refresh  ? help  q quit")
            put(h - 1, 0, cut(footer, w - 1), smap[RED] if model.error else smap[DIM])
            scr.refresh()
            time.sleep(0.05)

    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
    return 0


# ---------- main ----------
def main():
    p = argparse.ArgumentParser(
        prog="angie-top",
        description="Terminal 'Console Light' for Angie via the /status/ API.",
        epilog="Keys: 1-6 tabs, arrows scroll/switch, space pause, +/- interval, ? help, q quit.")
    p.add_argument("-u", "--url", default="http://127.0.0.1:8080/status/",
                   help="base API URL (default: %(default)s)")
    p.add_argument("-i", "--interval", type=float, default=1.0, help="poll interval, s (default 1)")
    p.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout, s")
    p.add_argument("--user", help="basic-auth user")
    p.add_argument("--password", help="basic-auth password")
    p.add_argument("-H", "--header", action="append", help="extra header 'Name: value' (repeatable)")
    p.add_argument("-k", "--insecure", action="store_true", help="skip TLS verification")
    p.add_argument("--once", action="store_true", help="print a text snapshot and exit")
    p.add_argument("--snapshot", action="store_true", help="with --once: single poll, totals only")
    p.add_argument("--tab", type=int, choices=range(1, len(TABS) + 1),
                   help="start tab / with --once print only this tab")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors in --once")
    p.add_argument("--ascii", action="store_true", help="ASCII sparklines/bars")
    p.add_argument("--history", type=int, default=120, help="sparkline history length")
    p.add_argument("--width", type=int, default=100, help="text width for --once")
    p.add_argument("--json", action="store_true", help="dump raw /status/ JSON and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()

    if args.ascii or "utf" not in (sys.stdout.encoding or "").lower():
        use_ascii()
    args.url = norm_url(args.url)
    headers = build_headers(args)

    if args.json:
        try:
            print(json.dumps(fetch_status(args.url, args.timeout, headers, args.insecure),
                             indent=2, ensure_ascii=False))
        except RuntimeError as e:
            print(f"angie-top: {e}", file=sys.stderr)
            return 1
        return 0

    model = Model(args.history)
    if args.once:
        return run_once(args, model, headers)
    return run_tui(args, model, headers)


if __name__ == "__main__":
    sys.exit(main())