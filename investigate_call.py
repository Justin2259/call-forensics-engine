#!/usr/bin/env python3
"""
Investigate a specific Genesys call in maximum detail.

Pulls: analytics CDR, participant segments/metrics, flow execution history
       with prompts/DTMF/decisions/variables, recording metadata, and
       resolves agent names.

Usage:
  python execution/investigate_call.py <conversationId>

Notes:
  - Flow execution history requires org-level execution data enabled
    (Admin > Architect > Settings > Execution Data) and the OAuth client
    to have Architect > Flow Instance > View/Search permissions.
  - Analytics interval is capped at 31 days. Calls older than 31 days will
    still appear in the conversation object but not in analytics CDR.
  - Flow execution data is retained for 10 days maximum.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta

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

from genesys_auth import get_access_token, get_api_base, get_credentials_for_env


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def build_session(token, region):
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    s.base = get_api_base(region)
    return s


def genesys_get(session, path, params=None):
    r = session.get(session.base + path, params=params, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if not r.content:
        return None
    try:
        return r.json()
    except Exception:
        return None


def genesys_post(session, path, body):
    r = session.post(session.base + path, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_ts(ts_str):
    if not ts_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return ts_str


def fmt_hms(ts_str):
    """Return just HH:MM:SS from an ISO timestamp."""
    if not ts_str:
        return "?"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts_str[:19]


def fmt_ms(ms):
    if ms is None:
        return "N/A"
    s = int(ms) // 1000
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def section(title):
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def subsection(title):
    print(f"\n--- {title} ---")


def resolve_user(session, user_id, cache):
    if user_id in cache:
        return cache[user_id]
    try:
        d = genesys_get(session, f"/api/v2/users/{user_id}", params={"fields": "name,email"})
        if d:
            name  = d.get("name", user_id)
            email = d.get("email", "")
            cache[user_id] = f"{name} <{email}>" if email else name
        else:
            cache[user_id] = user_id
    except Exception:
        cache[user_id] = user_id
    return cache[user_id]


# ---------------------------------------------------------------------------
# Analytics CDR section
# ---------------------------------------------------------------------------

def print_analytics(conv):
    section("ANALYTICS - CONVERSATION DETAIL RECORD")
    start = conv.get("conversationStart")
    end   = conv.get("conversationEnd")

    print(f"  Conversation ID   : {conv.get('conversationId', 'N/A')}")
    print(f"  Start             : {fmt_ts(start)}")
    print(f"  End               : {fmt_ts(end)}")

    if start and end:
        try:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            dur = int((e - s).total_seconds())
            m, sec = divmod(dur, 60)
            print(f"  Total Duration    : {m}m {sec}s ({dur}s)")
        except Exception:
            pass

    print(f"  Originating Dir   : {conv.get('originatingDirection', 'N/A')}")
    print(f"  External Tag      : {conv.get('externalTag', 'none')}")

    mos = conv.get("mediaStatsMinConversationMos")
    if mos:
        print(f"  Min MOS           : {round(mos, 2)}  (5.0 = perfect)")


def print_participants(conv, session, user_cache):
    section("PARTICIPANTS")
    participants = conv.get("participants", [])
    print(f"  Total: {len(participants)}")

    for i, p in enumerate(participants):
        purpose = p.get("purpose", "unknown")
        user_id = p.get("userId")
        sessions = p.get("sessions", [])

        subsection(f"Participant {i+1}: {purpose.upper()}")
        print(f"  Participant ID : {p.get('participantId', '?')}")

        if user_id:
            print(f"  User           : {resolve_user(session, user_id, user_cache)}  (ID: {user_id})")

        for sess in sessions:
            ani  = sess.get("ani", "").replace("tel:", "")
            dnis = sess.get("dnis", "").replace("tel:", "")
            if ani:
                print(f"  ANI            : {ani}")
            if dnis and not dnis.startswith("sip:"):
                print(f"  DNIS           : {dnis}")
            if sess.get("remoteName"):
                print(f"  Remote Name    : {sess['remoteName']}")
            if sess.get("recording"):
                print(f"  Recording      : yes")

            metrics  = sess.get("metrics", [])
            segments = sess.get("segments", [])

            if metrics:
                # Only show meaningful metrics (skip near-zero noise)
                shown = [(m["name"], m.get("value")) for m in metrics
                         if m.get("value") and m.get("value") != 0]
                if shown:
                    print(f"\n  Metrics:")
                    for name_m, val_m in shown:
                        val_str = fmt_ms(val_m) if str(name_m).startswith("t") else str(val_m)
                        print(f"    {name_m:<20} {val_str}")

            if segments:
                print(f"\n  Segments ({len(segments)}):")
                for seg in segments:
                    seg_type   = seg.get("segmentType", "?")
                    seg_start  = fmt_hms(seg.get("segmentStart"))
                    seg_end    = fmt_hms(seg.get("segmentEnd"))
                    disconnect = seg.get("disconnectType", "")
                    queue_name = seg.get("queueName", "")
                    flow_name  = seg.get("flowName", "")
                    flow_ver   = seg.get("flowVersion", "")

                    line = f"    [{seg_type}]  {seg_start} -> {seg_end}"
                    if queue_name:
                        line += f"  queue={queue_name}"
                    if flow_name:
                        line += f"  flow={flow_name} v{flow_ver}"
                    if disconnect:
                        line += f"  disconnect={disconnect}"
                    print(line)

            # IVR flow outcomes
            flow_data = sess.get("flow")
            if flow_data:
                for o in flow_data.get("outcomes", []):
                    oname = o.get("name", o.get("flowOutcomeId", "?"))
                    oval  = o.get("value", "")
                    print(f"  Flow Outcome   : {oname}  value={oval}")


# ---------------------------------------------------------------------------
# Conversation object section
# ---------------------------------------------------------------------------

def print_conversation_object(data):
    section("CONVERSATION OBJECT")

    if data is None:
        print("  Not found (call too old for real-time endpoint, or ID incorrect).")
        return

    print(f"  Start      : {fmt_ts(data.get('startTime'))}")
    print(f"  End        : {fmt_ts(data.get('endTime'))}")
    print(f"  Address    : {data.get('address', 'N/A')}")

    participants = data.get("participants", [])
    if participants:
        print(f"\n  Participants ({len(participants)}):")
        for p in participants:
            purpose = p.get("purpose", "")
            name    = p.get("name", "")
            ani     = p.get("ani", "").replace("tel:", "")
            dnis    = p.get("dnis", "").replace("tel:", "")
            disc    = p.get("disconnectType", "")
            start_t = fmt_hms(p.get("startTime"))

            line = f"    {purpose:<12} | {name:<30} | {start_t}"
            if ani and not ani.startswith("sip:"):
                line += f"  ANI={ani}"
            if disc:
                line += f"  disconnect={disc}"
            print(line)


# ---------------------------------------------------------------------------
# Flow execution history section
# ---------------------------------------------------------------------------

def fetch_flow_execution_data(session, conv_id):
    """
    Query flow instances for this conversation, batch-download execution
    data for all instances, and return list of parsed execution dicts.
    Returns [] if the feature is unavailable or no instances exist.
    """
    try:
        resp = genesys_post(session, "/api/v2/flows/instances/query", {
            "query": [{"and": [{"key": "ConversationId", "operator": "eq", "value": conv_id}]}]
        })
    except Exception as e:
        print(f"  [WARN] Flow instances query failed: {e}")
        return []

    instances = resp.get("entities", [])
    if not instances:
        return []

    # Sort chronologically
    instances.sort(key=lambda x: x.get("startDateTime", ""))

    # Batch download all execution data
    ids = [i["id"] for i in instances]
    try:
        job_resp = genesys_post(session, "/api/v2/flows/instances/jobs", {"ids": ids})
        job_id   = job_resp.get("id")
    except Exception as e:
        print(f"  [WARN] Flow instances jobs submit failed: {e}")
        return [{"meta": inst, "execution": []} for inst in instances]

    # Poll for job completion
    for _ in range(20):
        time.sleep(1)
        try:
            jr = genesys_get(session, f"/api/v2/flows/instances/jobs/{job_id}")
            if jr and jr.get("jobState") in ("Success", "Failed"):
                break
        except Exception:
            pass

    if not jr or jr.get("jobState") != "Success":
        print("  [WARN] Flow execution job did not complete in time.")
        return [{"meta": inst, "execution": []} for inst in instances]

    # Download each instance's execution data
    uri_map = {e["id"]: e["downloadUri"] for e in jr.get("entities", []) if not e.get("failed")}
    results = []
    for inst in instances:
        iid  = inst["id"]
        data = {}
        if iid in uri_map:
            try:
                rd = requests.get(uri_map[iid], timeout=30)
                data = rd.json()
            except Exception:
                pass
        results.append({"meta": inst, "execution": data.get("flow", {}).get("execution", [])})

    return results


def parse_flow_narrative(execution_events):
    """
    Walk the execution event list and extract the meaningful narrative:
    prompts played, DTMF pressed, decisions taken, data action results,
    milestones hit, variables set (key ones), transfer targets.
    Returns a list of human-readable strings.
    """
    lines = []

    for e in execution_events:
        etype = list(e.keys())[0]
        ev    = e[etype]
        ts    = fmt_hms(ev.get("dateTime", ""))

        if etype == "startedFlow":
            # Show key initial variables
            for v in ev.get("variables", []):
                val = v.get("value")
                if val is None or val == [] or (isinstance(val, dict) and val.get("valueIsDeferred")):
                    continue
                vname = v["variableName"]
                # Only show call-level and a few key flow vars
                if vname.startswith("Call.") or vname in (
                    "Flow.IsTest", "Flow.Version", "Flow.StartDateTimeUtc"
                ):
                    lines.append(f"  [{ts}] VAR  {vname} = {val}")

        elif etype == "actionPlayAudio":
            for item in ev.get("audio", {}).get("toParticipant", {}).get("audioItems", []):
                text      = item.get("text", "")
                prompt_nm = item.get("promptName", "")
                label     = text if text else prompt_nm
                if label:
                    lines.append(f"  [{ts}] PLAY  \"{label}\"")

        elif etype == "menuMenu":
            menu_name = ev.get("menuName", "?")
            for turn in ev.get("execution", []):
                t = turn.get("turn", {})
                # Prompts played this turn
                for item in t.get("toParticipant", {}).get("audioItems", []):
                    text      = item.get("text", "")
                    prompt_nm = item.get("promptName", "")
                    label     = text if text else prompt_nm
                    if label:
                        lines.append(f"  [{ts}] PLAY  \"{label}\"  (menu: {menu_name})")
                # Input received
                from_p = t.get("fromParticipant", {})
                audio  = from_p.get("audio", {})
                dtmf   = audio.get("dtmf", {})
                speech = audio.get("speech", {})
                if dtmf.get("value"):
                    lines.append(f"  [{fmt_hms(from_p.get('dateTime', ''))}] DTMF  pressed \"{dtmf['value']}\"")
                elif speech.get("utterance"):
                    lines.append(f"  [{fmt_hms(from_p.get('dateTime', ''))}] SPEECH  \"{speech['utterance']}\"")

            # No-input / no-match result
            out_vars = {v["variableName"]: v["value"] for v in ev.get("outputVariables", [])}
            if out_vars.get("Menu.LastCollectionNoInput"):
                lines.append(f"  [{ts}] INFO  No input on menu \"{menu_name}\" (timed out)")
            elif out_vars.get("Menu.LastCollectionNoMatch"):
                lines.append(f"  [{ts}] INFO  No match on menu \"{menu_name}\"")

        elif etype == "menuTask":
            lines.append(f"  [{ts}] MENU SELECTED  -> task \"{ev.get('menuName', '?')}\"")

        elif etype == "actionDecision":
            name      = ev.get("actionName", "Decision")
            condition = ev.get("inputData", {}).get("condition")
            path      = ev.get("outputPathId", "")
            path_lbl  = {"__YES__": "YES", "__NO__": "NO"}.get(path, path)
            if condition is not None:
                lines.append(f"  [{ts}] DECISION  \"{name}\"  condition={condition}  -> {path_lbl}")

        elif etype == "actionSwitch":
            name = ev.get("actionName", "Switch")
            val  = ev.get("inputData", {}).get("value", "")
            path = ev.get("outputPathId", "")
            if val or path:
                lines.append(f"  [{ts}] SWITCH  \"{name}\"  value={val}  path={path}")

        elif etype == "actionAddFlowMilestone":
            milestone = ev.get("inputData", {}).get("flowMilestone", {})
            mname = milestone.get("name", ev.get("actionName", "?"))
            lines.append(f"  [{ts}] MILESTONE  \"{mname}\"")

        elif etype == "actionCallData":
            aname   = ev.get("actionName", "?")
            outputs = ev.get("outputData", {}).get("successOutputs", [])
            out_str = ", ".join(f"{o['outputName']}={o['value']}" for o in outputs[:5] if o.get("value") is not None)
            lines.append(f"  [{ts}] DATA ACTION  \"{aname}\"  outputs: {out_str}" if out_str
                         else f"  [{ts}] DATA ACTION  \"{aname}\"")

        elif etype == "actionSetExternalTag":
            tag = ev.get("inputData", {}).get("externalTag") or \
                  next((v["value"] for v in ev.get("outputVariables", [])
                        if v["variableName"] == "Call.ExternalTag"), "?")
            lines.append(f"  [{ts}] SET TAG  \"{tag}\"")

        elif etype == "actionSetFlowOutcome":
            outcome = ev.get("inputData", {}).get("flowOutcome", {})
            oname   = outcome.get("name", "?")
            oval    = ev.get("inputData", {}).get("flowOutcomeValue", "")
            lines.append(f"  [{ts}] SET OUTCOME  \"{oname}\"  value={oval}")

        elif etype == "actionTransferToAcd":
            q = ev.get("inputData", {}).get("queue", {})
            qname = q.get("name", q.get("id", "?")) if isinstance(q, dict) else str(q)
            lines.append(f"  [{ts}] TRANSFER TO ACD  queue=\"{qname}\"")

        elif etype == "actionTransferToNumber":
            num = ev.get("inputData", {}).get("transferAddress", \
                  ev.get("inputData", {}).get("number", "?"))
            lines.append(f"  [{ts}] TRANSFER TO NUMBER  {num}")

        elif etype == "startedTask":
            task_name = ev.get("taskName", ev.get("taskId", "?"))
            lines.append(f"  [{ts}] TASK  \"{task_name}\"")

        elif etype == "endedFlow":
            lines.append(f"  [{ts}] FLOW END")

    return lines


def print_flow_executions(flow_instances):
    section("FLOW EXECUTION HISTORY")

    if not flow_instances:
        print("  No flow execution data found.")
        print("  (Feature requires: org-level execution data enabled + OAuth client")
        print("   with Architect > Flow Instance > View/Search permissions.)")
        print("  (Retention window: 10 days)")
        return

    print(f"  Flow instances found: {len(flow_instances)}")

    for fi in flow_instances:
        meta = fi["meta"]
        events = fi["execution"]

        flow_name = meta.get("flowName", "?")
        flow_ver  = meta.get("flowVersion", "?")
        flow_type = meta.get("flowType", "?")
        start_dt  = fmt_ts(meta.get("startDateTime"))
        end_dt    = fmt_ts(meta.get("endDateTime"))
        err       = meta.get("flowErrorReason", "NONE")
        warn      = meta.get("flowWarningReason", "NONE")

        subsection(f"{flow_name}  (v{flow_ver}, {flow_type})")
        print(f"  Instance ID : {meta.get('id')}")
        print(f"  Start       : {start_dt}")
        print(f"  End         : {end_dt}")
        if err != "NONE":
            print(f"  Error       : {err}")
        if warn != "NONE":
            print(f"  Warning     : {warn}")

        if events:
            print(f"\n  Execution narrative ({len(events)} raw events):")
            narrative = parse_flow_narrative(events)
            for line in narrative:
                print(line)
        else:
            print("\n  No execution detail available (data may have expired or log level too low).")


# ---------------------------------------------------------------------------
# Recordings section
# ---------------------------------------------------------------------------

def print_recordings(data):
    section("RECORDINGS")

    if data is None:
        print("  Not found (permissions or recording not yet processed).")
        return

    recs = data if isinstance(data, list) else data.get("entities", [data] if data else [])
    if not recs:
        print("  No recordings found.")
        return

    print(f"  Recordings found: {len(recs)}")
    for i, r in enumerate(recs):
        subsection(f"Recording {i+1}")
        print(f"  ID            : {r.get('id', 'N/A')}")
        print(f"  State         : {r.get('fileState', r.get('state', 'N/A'))}")
        print(f"  Media Type    : {r.get('media', r.get('mediaType', 'N/A'))} / {r.get('mediaSubtype', '')}")
        dur_ms = r.get('durationMs')
        if not dur_ms and r.get('startTime') and r.get('endTime'):
            try:
                s_dt = datetime.fromisoformat(r['startTime'].replace('Z', '+00:00'))
                e_dt = datetime.fromisoformat(r['endTime'].replace('Z', '+00:00'))
                dur_ms = int((e_dt - s_dt).total_seconds() * 1000)
            except Exception:
                pass
        print(f"  Duration      : {fmt_ms(dur_ms)}")
        print(f"  Start         : {fmt_ts(r.get('startTime'))}")
        print(f"  End           : {fmt_ts(r.get('endTime'))}")
        if r.get("deleteReason"):
            print(f"  Delete Reason : {r['deleteReason']}")
        for label, uri_data in r.get("mediaUris", {}).items():
            uri = uri_data.get("mediaUri", "") if isinstance(uri_data, dict) else str(uri_data)
            print(f"  Audio DL [{label}] : {uri[:80]}...")


# ---------------------------------------------------------------------------
# SIP trace / PCAP section
# ---------------------------------------------------------------------------

def fetch_and_print_sip_trace(session, conv_id, conv_start, conv_end):
    section("SIP TRACE / PCAP")

    # Widen the window slightly to catch all SIP messages
    try:
        s_dt = datetime.fromisoformat(conv_start.replace("Z", "+00:00"))
        e_dt = datetime.fromisoformat(conv_end.replace("Z", "+00:00"))
        date_start = (s_dt - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        date_end   = (e_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        date_start = conv_start
        date_end   = conv_end

    # Request a PCAP download job
    try:
        resp = session.post(session.base + "/api/v2/telephony/siptraces/download", json={
            "conversationId": conv_id,
            "dateStart": date_start,
            "dateEnd": date_end,
        }, timeout=30)
        resp.raise_for_status()
        download_id = resp.json().get("downloadId")
    except Exception as e:
        print(f"  [WARN] SIP trace download request failed: {e}")
        return

    if not download_id:
        print("  [WARN] No downloadId returned.")
        return

    # Download the PCAP binary
    try:
        pcap_resp = session.get(session.base + f"/api/v2/downloads/{download_id}", timeout=30)
        pcap_resp.raise_for_status()
        pcap_bytes = pcap_resp.content
    except Exception as e:
        print(f"  [WARN] PCAP download failed: {e}")
        return

    if not pcap_bytes or pcap_bytes[:4] != b"\xd4\xc3\xb2\xa1":
        print(f"  [WARN] Response is not a valid PCAP ({len(pcap_bytes)} bytes).")
        return

    # Save to .tmp/
    import os
    os.makedirs(".tmp", exist_ok=True)
    pcap_path = f".tmp/{conv_id}.pcap"
    with open(pcap_path, "wb") as f:
        f.write(pcap_bytes)
    print(f"  PCAP saved : {pcap_path}  ({len(pcap_bytes):,} bytes)")
    print(f"  Download ID: {download_id}")

    # Parse and summarize SIP dialog
    try:
        import dpkt
        import socket as _socket

        def _fmt_ip(raw):
            try:
                return _socket.inet_ntop(_socket.AF_INET, raw)
            except Exception:
                return raw.hex()

        sip_msgs = []
        with open(pcap_path, "rb") as f:
            for ts, buf in dpkt.pcap.Reader(f):
                try:
                    eth = dpkt.ethernet.Ethernet(buf)
                    ip = eth.data
                    if not isinstance(ip, dpkt.ip.IP):
                        continue
                    t = ip.data
                    if not isinstance(t, (dpkt.tcp.TCP, dpkt.udp.UDP)):
                        continue
                    payload = bytes(t.data)
                    if b"SIP/2.0" in payload or b"INVITE" in payload or b"BYE" in payload:
                        src = f"{_fmt_ip(ip.src)}:{t.sport}"
                        dst = f"{_fmt_ip(ip.dst)}:{t.dport}"
                        txt = payload.decode("utf-8", errors="replace")
                        sip_msgs.append((ts, src, dst, txt))
                except Exception:
                    pass

        print(f"\n  SIP dialog ({len(sip_msgs)} messages):")
        invite_sdp = None
        ok_sdp = None
        for ts, src, dst, msg in sip_msgs:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")
            first_line = msg.split("\r\n")[0]
            print(f"    [{dt}]  {src:>25} -> {dst:<30}  {first_line}")
            if first_line.startswith("INVITE") and "\r\n\r\n" in msg:
                invite_sdp = msg.split("\r\n\r\n", 1)[1].strip()
            if first_line.startswith("SIP/2.0 200") and "CSeq: 1 INVITE" in msg and "\r\n\r\n" in msg:
                ok_sdp = msg.split("\r\n\r\n", 1)[1].strip()

        if invite_sdp or ok_sdp:
            print("\n  Media negotiation:")
            if invite_sdp:
                for line in invite_sdp.splitlines():
                    if line.startswith(("m=", "c=", "a=rtpmap", "a=ptime", "a=sendrecv")):
                        print(f"    OFFER  {line}")
            if ok_sdp:
                for line in ok_sdp.splitlines():
                    if line.startswith(("m=", "c=", "a=rtpmap", "a=ptime", "a=sendrecv")):
                        print(f"    ANSWER {line}")

    except ImportError:
        print("  [INFO] Install dpkt to parse PCAP: pip install dpkt")
    except Exception as e:
        print(f"  [WARN] PCAP parse error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python execution/investigate_call.py <conversationId>")
        sys.exit(1)

    conv_id = sys.argv[1].strip().lower()
    print(f"\nInvestigating conversation: {conv_id}")
    print(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    cid, csec, region = get_credentials_for_env("prod")
    auth = get_access_token(region=region, client_id=cid, client_secret=csec)
    if not auth.get("success"):
        print(f"[FAIL] Auth: {auth.get('error')}")
        sys.exit(1)

    session    = build_session(auth["access_token"], region)
    user_cache = {}

    now            = datetime.now(timezone.utc)
    interval_start = (now - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    interval_end   = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # ------------------------------------------------------------------
    # 1. Analytics CDR
    # ------------------------------------------------------------------
    print("Fetching analytics CDR...")
    conversations = []
    try:
        resp = genesys_post(session, "/api/v2/analytics/conversations/details/query", {
            "interval": f"{interval_start}/{interval_end}",
            "conversationFilters": [{"type": "and", "predicates": [
                {"type": "dimension", "dimension": "conversationId", "operator": "matches", "value": conv_id}
            ]}],
            "paging": {"pageSize": 1, "pageNumber": 1}
        })
        conversations = resp.get("conversations", [])
    except requests.exceptions.HTTPError as e:
        print(f"[WARN] Analytics CDR failed: {e.response.status_code}")
    except Exception as e:
        print(f"[WARN] Analytics CDR failed: {e}")

    if conversations:
        print_analytics(conversations[0])
        print_participants(conversations[0], session, user_cache)
    else:
        section("ANALYTICS - CONVERSATION DETAIL RECORD")
        print(f"  No record found. Call may be outside the 31-day window or ID is wrong.")

    # ------------------------------------------------------------------
    # 2. Conversation object
    # ------------------------------------------------------------------
    print("\nFetching conversation object...")
    try:
        conv_obj = genesys_get(session, f"/api/v2/conversations/{conv_id}")
    except Exception as e:
        print(f"[WARN] Conversation object: {e}")
        conv_obj = None
    print_conversation_object(conv_obj)

    # ------------------------------------------------------------------
    # 3. Flow execution history
    # ------------------------------------------------------------------
    print("\nFetching flow execution history...")
    flow_instances = fetch_flow_execution_data(session, conv_id)
    print_flow_executions(flow_instances)

    # ------------------------------------------------------------------
    # 4. Recordings
    # ------------------------------------------------------------------
    print("\nFetching recordings...")
    try:
        rec_data = genesys_get(session, f"/api/v2/conversations/{conv_id}/recordings",
                               params={"maxWaitMs": 5000})
    except Exception as e:
        print(f"[WARN] Recordings: {e}")
        rec_data = None
    print_recordings(rec_data)

    # ------------------------------------------------------------------
    # 5. SIP trace / PCAP
    # ------------------------------------------------------------------
    conv_start = None
    conv_end   = None
    if conversations:
        conv_start = conversations[0].get("conversationStart")
        conv_end   = conversations[0].get("conversationEnd")
    elif conv_obj:
        conv_start = conv_obj.get("startTime")
        conv_end   = conv_obj.get("endTime")

    if conv_start and conv_end:
        print("\nFetching SIP trace / PCAP...")
        fetch_and_print_sip_trace(session, conv_id, conv_start, conv_end)
    else:
        section("SIP TRACE / PCAP")
        print("  Skipped (no conversation timestamps available).")

    # ------------------------------------------------------------------
    section("END OF INVESTIGATION")
    print(f"  Conversation : {conv_id}")
    print(f"  Completed at : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()


if __name__ == "__main__":
    main()

# revised

# revised

# rev 1

# rev 3

# rev 4

# rev 7

# rev 8
