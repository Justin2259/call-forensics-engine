#!/usr/bin/env python3
"""
Fetch and summarize the SIP trace PCAP for a Genesys conversation.

Pulls the SIP signaling PCAP for any conversation, saves it to .tmp/,
and prints a human-readable summary of the SIP dialog and media negotiation.

Usage:
  python execution/fetch_sip_pcap.py <conversationId>

API flow:
  1. POST /api/v2/telephony/siptraces/download  ->  downloadId
  2. GET  /api/v2/downloads/{downloadId}          ->  PCAP binary
"""

import os
import sys
import json
import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

try:
    import requests
except ImportError:
    print("[FAIL] pip install requests")
    sys.exit(1)

try:
    import dpkt
    import socket as _socket
    HAS_DPKT = True
except ImportError:
    HAS_DPKT = False

from genesys_auth import get_access_token, get_api_base, get_credentials_for_env


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_ts(ts_str):
    if not ts_str:
        return "N/A"
    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ts_str


def fmt_ip(raw):
    try:
        return _socket.inet_ntop(_socket.AF_INET, raw)
    except Exception:
        return raw.hex()


def section(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Fetch conversation timestamps (needed to bound the SIP trace query)
# ---------------------------------------------------------------------------

def get_conv_times(session, conv_id):
    """Return (start_iso, end_iso) for the conversation. Tries analytics first, then conv object."""
    base = session.base
    now = datetime.datetime.now(datetime.timezone.utc)
    interval_start = (now - datetime.timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    interval_end = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    try:
        r = session.post(base + "/api/v2/analytics/conversations/details/query", json={
            "interval": f"{interval_start}/{interval_end}",
            "conversationFilters": [{"type": "and", "predicates": [
                {"type": "dimension", "dimension": "conversationId", "operator": "matches", "value": conv_id}
            ]}],
            "paging": {"pageSize": 1, "pageNumber": 1}
        }, timeout=30)
        r.raise_for_status()
        convs = r.json().get("conversations", [])
        if convs:
            return convs[0].get("conversationStart"), convs[0].get("conversationEnd")
    except Exception:
        pass

    try:
        r = session.get(base + f"/api/v2/conversations/{conv_id}", timeout=30)
        if r.status_code == 200 and r.content:
            data = r.json()
            return data.get("startTime"), data.get("endTime")
    except Exception:
        pass

    return None, None


# ---------------------------------------------------------------------------
# Fetch PCAP
# ---------------------------------------------------------------------------

def fetch_pcap(session, conv_id, conv_start, conv_end):
    """Request a SIP trace download job and return the raw PCAP bytes + downloadId."""
    base = session.base

    try:
        s_dt = datetime.datetime.fromisoformat(conv_start.replace("Z", "+00:00"))
        e_dt = datetime.datetime.fromisoformat(conv_end.replace("Z", "+00:00"))
        date_start = (s_dt - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        date_end   = (e_dt + datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        date_start, date_end = conv_start, conv_end

    r = session.post(base + "/api/v2/telephony/siptraces/download", json={
        "conversationId": conv_id,
        "dateStart": date_start,
        "dateEnd": date_end,
    }, timeout=30)
    r.raise_for_status()

    download_id = r.json().get("downloadId")
    if not download_id:
        raise ValueError("No downloadId in response")

    r2 = session.get(base + f"/api/v2/downloads/{download_id}", timeout=30)
    r2.raise_for_status()

    if not r2.content or r2.content[:4] != b"\xd4\xc3\xb2\xa1":
        raise ValueError(f"Response is not a valid PCAP ({len(r2.content)} bytes)")

    return r2.content, download_id


# ---------------------------------------------------------------------------
# Parse and summarize PCAP
# ---------------------------------------------------------------------------

def parse_and_print_pcap(pcap_bytes, conv_id):
    if not HAS_DPKT:
        print("\n  [INFO] Install dpkt to parse PCAP: pip install dpkt")
        return

    import io
    sip_msgs = []
    rtp_flows = {}

    try:
        for ts, buf in dpkt.pcap.Reader(io.BytesIO(pcap_bytes)):
            try:
                eth = dpkt.ethernet.Ethernet(buf)
                ip = eth.data
                if not isinstance(ip, dpkt.ip.IP):
                    continue
                t = ip.data
                if not isinstance(t, (dpkt.tcp.TCP, dpkt.udp.UDP)):
                    continue
                payload = bytes(t.data)
                src = f"{fmt_ip(ip.src)}:{t.sport}"
                dst = f"{fmt_ip(ip.dst)}:{t.dport}"

                if b"SIP/2.0" in payload or b"INVITE" in payload or b"BYE" in payload:
                    sip_msgs.append((ts, src, dst, payload.decode("utf-8", errors="replace")))
                elif isinstance(t, dpkt.udp.UDP) and len(payload) >= 12:
                    flow_key = f"{src}->{dst}"
                    rtp_flows[flow_key] = rtp_flows.get(flow_key, 0) + 1
            except Exception:
                pass
    except Exception as e:
        print(f"  [WARN] PCAP parse error: {e}")
        return

    # SIP dialog table
    section("SIP DIALOG")
    print(f"  {'Time (UTC)':<16}  {'Source':<28}  {'Destination':<28}  Message")
    print(f"  {'-'*15}  {'-'*27}  {'-'*27}  {'-'*40}")
    invite_sdp = None
    ok_sdp = None
    first_ts = sip_msgs[0][0] if sip_msgs else None
    last_ts  = sip_msgs[-1][0] if sip_msgs else None

    for ts, src, dst, msg in sip_msgs:
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        first_line = msg.split("\r\n")[0]
        print(f"  {dt:<16}  {src:<28}  {dst:<28}  {first_line}")
        if first_line.startswith("INVITE") and "\r\n\r\n" in msg:
            invite_sdp = msg.split("\r\n\r\n", 1)[1].strip()
        if "200 OK" in first_line and "CSeq: 1 INVITE" in msg and "\r\n\r\n" in msg:
            ok_sdp = msg.split("\r\n\r\n", 1)[1].strip()

    # Timing summary
    if first_ts and last_ts and len(sip_msgs) >= 2:
        print()
        invite_time = None
        ok_time = None
        bye_time = None
        for ts, src, dst, msg in sip_msgs:
            first_line = msg.split("\r\n")[0]
            if first_line.startswith("INVITE") and invite_time is None:
                invite_time = ts
            if "200 OK" in first_line and "CSeq: 1 INVITE" in msg and ok_time is None:
                ok_time = ts
            if first_line.startswith("BYE") and bye_time is None:
                bye_time = ts

        if invite_time and ok_time:
            ring_s = round(ok_time - invite_time, 2)
            print(f"  Ring time (INVITE to 200 OK) : {ring_s}s")
        if ok_time and bye_time:
            talk_s = round(bye_time - ok_time, 1)
            m, s = divmod(int(talk_s), 60)
            print(f"  Talk time (200 OK to BYE)    : {m}m {s}s")

        # Who hung up?
        last_bye = next(((s, d, m) for ts, s, d, m in reversed(sip_msgs) if m.split("\r\n")[0].startswith("BYE")), None)
        if last_bye:
            bye_src, _, bye_msg = last_bye
            # Extract Reason header if present
            reason = next((l for l in bye_msg.split("\r\n") if l.startswith("Reason:")), None)
            print(f"  BYE originated from          : {bye_src}")
            if reason:
                print(f"  Reason header                : {reason}")

    # Media negotiation
    if invite_sdp or ok_sdp:
        section("MEDIA NEGOTIATION")
        if invite_sdp:
            print("  Genesys OFFER:")
            for line in invite_sdp.splitlines():
                if line.startswith(("c=", "m=", "a=rtpmap", "a=ptime", "a=sendrecv", "a=sendonly", "a=recvonly")):
                    print(f"    {line}")
        if ok_sdp:
            print()
            print("  LiveKit ANSWER:")
            for line in ok_sdp.splitlines():
                if line.startswith(("c=", "m=", "a=rtpmap", "a=ptime", "a=sendrecv", "a=sendonly", "a=recvonly")):
                    print(f"    {line}")

        # Codec summary
        print()
        offered = [l for l in (invite_sdp or "").splitlines() if l.startswith("a=rtpmap")]
        answered = [l for l in (ok_sdp or "").splitlines() if l.startswith("a=rtpmap")]
        offer_codecs  = ", ".join(l.split(" ", 1)[1] for l in offered if "telephone" not in l)
        answer_codecs = ", ".join(l.split(" ", 1)[1] for l in answered if "telephone" not in l)
        if offer_codecs:
            print(f"  Offered codecs  : {offer_codecs}")
        if answer_codecs:
            print(f"  Selected codec  : {answer_codecs}")
        if offer_codecs and answer_codecs:
            if offer_codecs.split(",")[0].strip().lower() != answer_codecs.strip().lower():
                print("  [NOTE] Codec downgrade: remote end did not accept the preferred codec.")

    # RTP flows
    if rtp_flows:
        section("RTP FLOWS")
        for flow, count in rtp_flows.items():
            print(f"  {flow}  ({count} packets)")
    else:
        print()
        print("  [NOTE] No RTP packets captured. The PCAP covers SIP signaling only.")
        print("         RTP media flows directly between endpoints and is not captured at the Edge.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python execution/fetch_sip_pcap.py <conversationId>")
        sys.exit(1)

    conv_id = sys.argv[1].strip().lower()
    print(f"\nFetching SIP trace PCAP for conversation: {conv_id}")
    print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    cid, csec, region = get_credentials_for_env("prod")
    auth = get_access_token(region=region, client_id=cid, client_secret=csec)
    if not auth.get("success"):
        print(f"[FAIL] Auth: {auth.get('error')}")
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {auth['access_token']}",
        "Content-Type": "application/json",
    })
    session.base = get_api_base(region)

    # Get conversation timestamps
    print("Resolving conversation timestamps...")
    conv_start, conv_end = get_conv_times(session, conv_id)
    if not conv_start or not conv_end:
        print("[FAIL] Could not resolve conversation start/end times.")
        print("       Call may be outside the 31-day analytics window or ID is incorrect.")
        sys.exit(1)

    print(f"  Start : {fmt_ts(conv_start)}")
    print(f"  End   : {fmt_ts(conv_end)}")

    # Fetch PCAP
    print("\nRequesting SIP trace download...")
    try:
        pcap_bytes, download_id = fetch_pcap(session, conv_id, conv_start, conv_end)
    except Exception as e:
        print(f"[FAIL] Could not fetch PCAP: {e}")
        sys.exit(1)

    os.makedirs(".tmp", exist_ok=True)
    pcap_path = f".tmp/{conv_id}.pcap"
    with open(pcap_path, "wb") as f:
        f.write(pcap_bytes)

    section("PCAP METADATA")
    print(f"  Conversation ID : {conv_id}")
    print(f"  Download ID     : {download_id}")
    print(f"  File            : {pcap_path}")
    print(f"  Size            : {len(pcap_bytes):,} bytes")

    # Parse and print
    parse_and_print_pcap(pcap_bytes, conv_id)

    section("END OF SIP TRACE")
    print(f"  Conversation : {conv_id}")
    print(f"  Completed at : {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()


if __name__ == "__main__":
    main()

# revised

# rev 2

# rev 6
