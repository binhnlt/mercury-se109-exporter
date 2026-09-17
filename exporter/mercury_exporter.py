#!/usr/bin/env python3
"""
Prometheus exporter for Mercury / TP-Link (Realtek) web-managed switches
such as the Mercury SE109 Pro (2.5 Gbps managed switch).

These switches expose NO SNMP, so we log into the web UI (/logon.cgi using
TP-Link's `securityEncode` password obfuscation) and scrape the port
statistics page, then republish the counters as Prometheus metrics.

Usage:
    # Normal run: serve /metrics on :9109
    SWITCH_HOST=192.168.0.251 SWITCH_USER=admin SWITCH_PASS='your-password' \
        python3 mercury_exporter.py

    # One-shot dump of the raw statistics page (for adapting the parser):
    SWITCH_HOST=192.168.0.251 SWITCH_USER=admin SWITCH_PASS='your-password' \
        python3 mercury_exporter.py --dump

Environment variables:
    SWITCH_HOST     switch IP/host           (default 192.168.0.251)
    SWITCH_USER     web username             (required)
    SWITCH_PASS     web password (plaintext) (required)
    EXPORTER_PORT   port to serve metrics    (default 9109)
    SCRAPE_TIMEOUT  per-request timeout secs (default 8)
    STATS_PATH      stats page path          (default PortStatisticsRpm.htm)
"""
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from prometheus_client import start_http_server, REGISTRY
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

# --- TP-Link securityEncode keys (taken verbatim from the switch's cryp_new.js)
_KEY1 = "RDpbLfCPsJZ7fiv"
_KEY2 = ("yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD0"
         "2KZciXTysVXiV8ngg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57f"
         "QMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwm"
         "CAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW")


def security_encode(pw, key1=_KEY1, key2=_KEY2):
    """Port of hex_md5()/securityEncode() from the switch's cryp_new.js."""
    out = []
    e, c, j = len(pw), len(key1), len(key2)
    h = e if e > c else c
    for g in range(h):
        l = 187
        i = 187
        if g >= e:
            i = ord(key1[g])
        elif g >= c:
            l = ord(pw[g])
        else:
            l = ord(pw[g])
            i = ord(key1[g])
        out.append(key2[(l ^ i) % j])
    return "".join(out)


class SwitchClient:
    def __init__(self, host, user, password, timeout=8,
                 stats_path="PortStatisticsRpm.htm"):
        self.base = "http://%s" % host
        self.user = user
        self.password = password
        self.timeout = timeout
        self.stats_path = stats_path
        self._cookie = None  # some firmwares set a cookie, many are IP-session

    def _request(self, path, data=None, headers=None):
        url = self.base + "/" + path.lstrip("/")
        hdrs = {
            "User-Agent": "mercury-exporter/1.0",
            "Referer": self.base + "/",
        }
        if self._cookie:
            hdrs["Cookie"] = self._cookie
        if headers:
            hdrs.update(headers)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=hdrs,
                                     method="POST" if body else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sc = resp.headers.get("Set-Cookie")
            if sc:
                self._cookie = sc.split(";", 1)[0]
            return resp.read().decode("utf-8", "replace")

    def login(self):
        enc = security_encode(self.password)
        html = self._request("logon.cgi", data={
            "username": self.user,
            "password": enc,
            "logon": "Login",
        })
        # logonInfo = new Array( N, ...)  -> N==0 means success
        m = re.search(r"logonInfo\s*=\s*new Array\(\s*(\d+)", html)
        err = int(m.group(1)) if m else -1
        if err != 0:
            raise RuntimeError(
                "login failed (logonInfo code=%s; 1=bad user/pass, "
                "2=not allowed/locked, 5=session timeout)" % err)
        return True

    def _is_login_page(self, html):
        m = re.search(r"logonInfo\s*=\s*new Array\(\s*(\d+)", html)
        return (m is not None and m.group(1) != "0") or \
               ("logon.cgi" in html and "submitForm" in html)

    def _fetch_authed(self, path):
        """GET a page, (re-)logging in if the session has expired."""
        html = self._request(path)
        if self._is_login_page(html):
            self.login()
            html = self._request(path)
        return html

    def fetch_stats(self):
        """Return the raw port-statistics page HTML."""
        return self._fetch_authed(self.stats_path)

    def fetch_system(self):
        """Return the raw main/system page HTML (device info + uptime)."""
        return self._fetch_authed("MainRpm.htm")


# --- Parsing -----------------------------------------------------------------
#
# The Realtek/TP-Link "Easy-Smart" style statistics page embeds data as JS:
#
#   var max_port_num = 9;
#   var all_info = {
#       state:[1,1,...],            // 1=enabled 0=disabled
#       link_status:[6,0,...],      // 0=down; other codes = negotiated speed
#       pkts:[TxGood,TxBad,RxGood,RxBad, TxGood,TxBad,RxGood,RxBad, ...] // 4/port
#   };
#
# Newer 2.5G models may add extra fields or a bytes[] array. The parser below
# extracts every JS array generically, then maps the known ones. If the real
# page differs, run `--dump`, look at the arrays, and adjust FIELD MAP below.

def _extract_js_arrays(html):
    """Return {name: [ints]} for every `name:[...]` or `var name = [...]`."""
    arrays = {}
    # object-style  key:[ 1,2,3 ]
    for name, body in re.findall(r"([A-Za-z_]\w*)\s*:\s*\[([^\]]*)\]", html):
        arrays[name] = _nums(body)
    # var-style     var name = new Array( 1,2,3 )  /  var name = [1,2,3]
    for name, body in re.findall(
            r"var\s+([A-Za-z_]\w*)\s*=\s*(?:new Array\(|\[)([^\]\)]*)[\]\)]", html):
        arrays.setdefault(name, _nums(body))
    return arrays


def _nums(body):
    return [int(x) for x in re.findall(r"-?\d+", body)]


def _scalar(html, name):
    m = re.search(r"var\s+%s\s*=\s*(\d+)" % re.escape(name), html)
    return int(m.group(1)) if m else None


# link_status code -> Mbps.  Confirmed against the SE109 Pro's own link_info[]
# array on PortStatisticsRpm.htm:
#   0 断开(down) 1 自动(auto) 2 10M-half 3 10M-full 4 100M-half 5 100M-full
#   6 1000M-full 7 2.5G-full 8 10G-full(SFP+) 9 (unused)
SPEED_MAP = {0: 0, 1: 0, 2: 10, 3: 10, 4: 100, 5: 100, 6: 1000, 7: 2500,
             8: 10000, 9: 0}


def parse_stats(html):
    """
    Returns a list of per-port dicts:
       {port, enabled, link_up, speed_mbps,
        tx_good, tx_bad, rx_good, rx_bad}
    Raises ValueError if it can't find recognizable data.
    """
    arrays = _extract_js_arrays(html)
    n = _scalar(html, "max_port_num") or _scalar(html, "port_num")
    state = arrays.get("state")
    link = arrays.get("link_status")
    pkts = arrays.get("pkts")

    if not n:
        # infer from the longest plausible per-port array
        if state:
            n = len(state)
        elif link:
            n = len(link)
        else:
            raise ValueError("could not locate max_port_num / state / link_status")

    ports = []
    for p in range(n):
        st = state[p] if state and p < len(state) else None
        lk = link[p] if link and p < len(link) else 0
        row = {
            "port": p + 1,
            "enabled": st,
            "link_up": 1 if lk and lk != 0 else 0,
            "speed_mbps": SPEED_MAP.get(lk, 0) if lk else 0,
            "link_code": lk,
        }
        if pkts and len(pkts) >= 4 * (p + 1):
            base = 4 * p
            row["tx_good"] = pkts[base + 0]
            row["tx_bad"] = pkts[base + 1]
            row["rx_good"] = pkts[base + 2]
            row["rx_bad"] = pkts[base + 3]
        ports.append(row)
    if not ports:
        raise ValueError("no ports parsed")
    return ports


# --- System / device info ----------------------------------------------------
#
# MainRpm.htm carries a JS object `info_ds` with the device identity plus
# `workTime` (uptime). CPU / RAM / temperature / fan are NOT exposed by this
# hardware, so there is nothing to scrape for those.
#
#   info_ds = { descriStr:["my-switch"], macStr:["AA:BB:.."], ipStr:["192.168.0.251"],
#               netmaskStr:[".."], gatewayStr:[".."], firmwareStr:[".."],
#               hardwareStr:["SE109 Pro"], workTime:["3 day - 13 hour - 23 min - 4 sec"] }

def _js_str_field(html, name):
    """First string element of a JS array field:  name:["value"]  ."""
    m = re.search(re.escape(name) + r'\s*:\s*\[\s*"([^"]*)"', html)
    return m.group(1).strip() if m else ""


def parse_uptime_seconds(worktime):
    """'3 day - 13 hour - 23 min - 4 sec' -> 308584 (seconds)."""
    units = {"day": 86400, "hour": 3600, "min": 60, "sec": 1}
    total = 0
    for val, unit in re.findall(r"(\d+)\s*(day|hour|min|sec)", worktime):
        total += int(val) * units[unit]
    return total


def parse_system(html):
    """Extract device identity + uptime from MainRpm.htm."""
    return {
        "hostname": _js_str_field(html, "descriStr"),
        "mac": _js_str_field(html, "macStr"),
        "ip": _js_str_field(html, "ipStr"),
        "netmask": _js_str_field(html, "netmaskStr"),
        "gateway": _js_str_field(html, "gatewayStr"),
        "firmware": _js_str_field(html, "firmwareStr"),
        "model": _js_str_field(html, "hardwareStr"),  # MainRpm puts model here
        "uptime_seconds": parse_uptime_seconds(_js_str_field(html, "workTime")),
    }


# --- Prometheus collector ----------------------------------------------------
class SwitchCollector:
    def __init__(self, client):
        self.client = client

    def collect(self):
        up = GaugeMetricFamily(
            "mercury_switch_up",
            "1 if the switch scrape (login+parse) succeeded, else 0")
        try:
            html = self.client.fetch_stats()
            ports = parse_stats(html)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("scrape error: %s\n" % e)
            up.add_metric([], 0.0)
            yield up
            return
        up.add_metric([], 1.0)
        yield up

        # --- device info + uptime (best-effort; never fails the port scrape) ---
        try:
            sysinfo = parse_system(self.client.fetch_system())
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("system-info scrape error: %s\n" % e)
            sysinfo = None
        if sysinfo:
            upt = GaugeMetricFamily(
                "mercury_switch_uptime_seconds",
                "Switch uptime in seconds (from workTime)")
            upt.add_metric([], sysinfo["uptime_seconds"])
            yield upt

            info = GaugeMetricFamily(
                "mercury_switch_info",
                "Switch device identity; value is always 1, data is in the labels",
                labels=["hostname", "mac", "ip", "netmask", "gateway",
                        "firmware", "model"])
            info.add_metric([sysinfo["hostname"], sysinfo["mac"], sysinfo["ip"],
                             sysinfo["netmask"], sysinfo["gateway"],
                             sysinfo["firmware"], sysinfo["model"]], 1.0)
            yield info

        link = GaugeMetricFamily(
            "mercury_port_link_up", "1 if the port link is up",
            labels=["port"])
        admin = GaugeMetricFamily(
            "mercury_port_admin_enabled", "1 if the port is administratively enabled",
            labels=["port"])
        speed = GaugeMetricFamily(
            "mercury_port_speed_mbps", "Negotiated link speed in Mbps",
            labels=["port"])
        txg = CounterMetricFamily(
            "mercury_port_tx_good_packets_total", "Good packets transmitted",
            labels=["port"])
        txb = CounterMetricFamily(
            "mercury_port_tx_bad_packets_total", "Bad/error packets transmitted",
            labels=["port"])
        rxg = CounterMetricFamily(
            "mercury_port_rx_good_packets_total", "Good packets received",
            labels=["port"])
        rxb = CounterMetricFamily(
            "mercury_port_rx_bad_packets_total", "Bad/error packets received",
            labels=["port"])

        for r in ports:
            lbl = [str(r["port"])]
            link.add_metric(lbl, r["link_up"])
            if r.get("enabled") is not None:
                admin.add_metric(lbl, r["enabled"])
            speed.add_metric(lbl, r["speed_mbps"])
            if "tx_good" in r:
                txg.add_metric(lbl, r["tx_good"])
                txb.add_metric(lbl, r["tx_bad"])
                rxg.add_metric(lbl, r["rx_good"])
                rxb.add_metric(lbl, r["rx_bad"])

        yield link
        yield admin
        yield speed
        yield txg
        yield txb
        yield rxg
        yield rxb


def main():
    host = os.environ.get("SWITCH_HOST", "192.168.0.251")
    user = os.environ.get("SWITCH_USER")
    password = os.environ.get("SWITCH_PASS")
    port = int(os.environ.get("EXPORTER_PORT", "9109"))
    timeout = float(os.environ.get("SCRAPE_TIMEOUT", "8"))
    stats_path = os.environ.get("STATS_PATH", "PortStatisticsRpm.htm")

    if not user or not password:
        sys.exit("SWITCH_USER and SWITCH_PASS must be set")

    client = SwitchClient(host, user, password, timeout, stats_path)

    if "--dump" in sys.argv:
        client.login()
        print(client.fetch_stats())
        return

    REGISTRY.register(SwitchCollector(client))
    start_http_server(port)
    sys.stderr.write("mercury_exporter serving /metrics on :%d (switch %s)\n"
                     % (port, host))
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
