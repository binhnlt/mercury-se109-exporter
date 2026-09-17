"""Unit tests for mercury_exporter parsing logic.

Run with:  pytest exporter/   (or: python -m pytest)
No switch or network access required — everything is tested against sampled
page fragments captured from a real Mercury SE109 Pro.
"""
import mercury_exporter as m


# --- login password encoding -------------------------------------------------
# Golden vectors produced by this implementation, which was verified
# byte-for-byte against the switch's own cryp_new.js `hex_md5()`/securityEncode.
def test_security_encode_golden_vectors():
    assert m.security_encode("admin") == "WaQ7xbhc9TefbwK"
    assert m.security_encode("password") == "xHVQ3wiB9TefbwK"
    assert m.security_encode("ChangeMe123!") == "QpQL4VKU3A0tbwK"


def test_security_encode_is_deterministic():
    assert m.security_encode("admin") == m.security_encode("admin")


# --- port statistics parsing -------------------------------------------------
# Real fragment shape from PortStatisticsRpm.htm (9-port SE109 Pro).
SAMPLE_STATS = """
var max_port_num = 9;
var all_info = {
 state:[1,1,1,1,1,1,1,1,1,0,0],
 link_status:[0,6,6,6,6,0,0,7,8,0,0],
 pkts:[0,0,0,0, 802781,0,231805,0, 28439517,0,48576666,0, 19450236,0,15915457,0,
       4283998,0,5199921,0, 0,0,0,0, 0,0,0,0, 13445524,0,12739889,0,
       81806381,0,64069634,0, 0,0,0]
};
"""


def test_parse_stats_port_count():
    assert len(m.parse_stats(SAMPLE_STATS)) == 9


def test_parse_stats_speed_codes():
    ports = m.parse_stats(SAMPLE_STATS)
    speeds = {p["port"]: p["speed_mbps"] for p in ports}
    assert speeds[1] == 0        # down
    assert speeds[2] == 1000     # code 6 -> 1G
    assert speeds[8] == 2500     # code 7 -> 2.5G
    assert speeds[9] == 10000    # code 8 -> 10G SFP+


def test_parse_stats_link_up():
    ports = {p["port"]: p for p in m.parse_stats(SAMPLE_STATS)}
    assert ports[1]["link_up"] == 0
    assert ports[9]["link_up"] == 1


def test_parse_stats_packet_counters():
    ports = {p["port"]: p for p in m.parse_stats(SAMPLE_STATS)}
    # pkts layout is [TxGood, TxBad, RxGood, RxBad] per port
    assert ports[2]["tx_good"] == 802781
    assert ports[2]["rx_good"] == 231805
    assert ports[9]["tx_good"] == 81806381
    assert ports[9]["rx_bad"] == 0


# --- system info parsing -----------------------------------------------------
SAMPLE_MAIN = (
    'var info_ds = { descriStr:["my-switch"], macStr:["AA:BB:CC:DD:EE:FF"], '
    'firmwareStr:["1.0.0 Build 20240914 Rel.40108"], hardwareStr:["SE109 Pro"], '
    'ipStr:["192.168.0.251"], netmaskStr:["255.255.255.0"], gatewayStr:[""], '
    'workTime:["3 day - 13 hour - 23 min - 4 sec"], }'
)


def test_parse_system_fields():
    s = m.parse_system(SAMPLE_MAIN)
    assert s["hostname"] == "my-switch"
    assert s["mac"] == "AA:BB:CC:DD:EE:FF"
    assert s["ip"] == "192.168.0.251"
    assert s["model"] == "SE109 Pro"
    assert s["gateway"] == ""


def test_parse_uptime_seconds():
    assert m.parse_uptime_seconds("3 day - 13 hour - 23 min - 4 sec") == (
        3 * 86400 + 13 * 3600 + 23 * 60 + 4
    )
    assert m.parse_uptime_seconds("") == 0
    assert m.parse_uptime_seconds("45 sec") == 45
