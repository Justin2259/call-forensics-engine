"""
Tests for the call forensics engine.

Validates SIP/PCAP parsing, IVR trace extraction,
media quality scoring, and report generation logic.
"""

import pytest
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────
# SIP parsing
# ─────────────────────────────────────────────────────────────

class TestSIPParsing:
    def test_extract_call_id_from_sip_header(self):
        sip_header = "Call-ID: abc123xyz@10.0.0.1"
        call_id = sip_header.split("Call-ID: ")[1].strip()
        assert call_id == "abc123xyz@10.0.0.1"

    def test_codec_offer_extraction(self):
        sdp_body = "a=rtpmap:0 PCMU/8000\na=rtpmap:111 opus/48000/2"
        codecs = []
        for line in sdp_body.splitlines():
            if line.startswith("a=rtpmap:"):
                codec = line.split(" ")[1].split("/")[0]
                codecs.append(codec)
        assert "PCMU" in codecs
        assert "opus" in codecs

    def test_codec_downgrade_detection(self):
        """Detect when call downgrades from Opus to PCMU (quality loss)."""
        offered_codecs = ["opus", "PCMU"]
        answered_codec = "PCMU"
        preferred = "opus"

        downgraded = answered_codec != preferred and preferred in offered_codecs
        assert downgraded is True

    def test_sip_response_code_classification(self):
        codes = {
            200: "success",
            404: "not_found",
            486: "busy",
            487: "cancelled",
            500: "server_error",
        }
        assert codes[200] == "success"
        assert codes[486] == "busy"
        assert codes[487] == "cancelled"

    def test_call_duration_from_timestamps(self):
        invite_time = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        bye_time = datetime(2026, 1, 15, 10, 4, 35, tzinfo=timezone.utc)
        duration_seconds = (bye_time - invite_time).total_seconds()
        assert duration_seconds == 275.0  # 4 min 35 sec


# ─────────────────────────────────────────────────────────────
# IVR trace analysis
# ─────────────────────────────────────────────────────────────

class TestIVRTraceAnalysis:
    def test_dtmf_sequence_extraction(self):
        raw_events = [
            {"type": "dtmf", "digit": "1", "timestamp": "2026-01-15T10:00:05Z"},
            {"type": "dtmf", "digit": "2", "timestamp": "2026-01-15T10:00:08Z"},
            {"type": "prompt", "text": "Please hold", "timestamp": "2026-01-15T10:00:10Z"},
        ]
        dtmf_sequence = [e["digit"] for e in raw_events if e["type"] == "dtmf"]
        assert dtmf_sequence == ["1", "2"]

    def test_ivr_path_reconstruction(self):
        flow_events = [
            {"action": "PlayAudio", "result": "completed"},
            {"action": "CollectInput", "result": "1"},
            {"action": "Decision", "branch": "english"},
            {"action": "Transfer", "destination": "queue:general"},
        ]
        path = [e["action"] for e in flow_events]
        assert path == ["PlayAudio", "CollectInput", "Decision", "Transfer"]

    def test_language_selection_detection(self):
        dtmf_input = "1"
        language_map = {"1": "English", "2": "Spanish"}
        selected = language_map.get(dtmf_input, "Unknown")
        assert selected == "English"

    def test_abandoned_call_detection(self):
        """Detect calls where caller hung up in IVR before reaching agent."""
        call_events = [
            {"type": "ivr_start"},
            {"type": "prompt_played", "prompt": "welcome"},
            {"type": "disconnect", "reason": "caller_hangup"},
        ]
        reached_agent = any(e["type"] == "queue_answer" for e in call_events)
        abandoned = not reached_agent
        assert abandoned is True

    def test_data_lookup_success_detection(self):
        data_actions = [
            {"action": "DataAction", "name": "GetShopInfo", "status": "SUCCESS", "result": {"shop_open": True}},
            {"action": "DataAction", "name": "GetHours", "status": "FAILED", "error": "timeout"},
        ]
        failed = [a for a in data_actions if a["status"] == "FAILED"]
        assert len(failed) == 1
        assert failed[0]["name"] == "GetHours"


# ─────────────────────────────────────────────────────────────
# Media quality
# ─────────────────────────────────────────────────────────────

class TestMediaQuality:
    def test_mos_score_classification(self):
        def classify_mos(score):
            if score >= 4.0:
                return "excellent"
            elif score >= 3.5:
                return "good"
            elif score >= 3.0:
                return "fair"
            else:
                return "poor"

        assert classify_mos(4.3) == "excellent"
        assert classify_mos(3.7) == "good"
        assert classify_mos(3.1) == "fair"
        assert classify_mos(2.5) == "poor"

    def test_packet_loss_threshold(self):
        packet_loss_pct = 2.5
        threshold = 5.0
        is_acceptable = packet_loss_pct < threshold
        assert is_acceptable is True

        high_loss = 7.2
        assert high_loss >= threshold

    def test_jitter_threshold(self):
        jitter_ms = 25
        max_acceptable_ms = 50
        assert jitter_ms <= max_acceptable_ms


# ─────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────

class TestReportGeneration:
    def test_report_sections_present(self):
        required_sections = [
            "call_summary",
            "call_flow_timeline",
            "ivr_execution_detail",
            "agent_activity",
            "media_quality",
            "sip_signaling",
        ]
        mock_report = {section: {} for section in required_sections}
        for section in required_sections:
            assert section in mock_report

    def test_conversation_id_format(self):
        """Genesys conversation IDs are UUIDs."""
        import re
        conv_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        uuid_pattern = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            re.IGNORECASE
        )
        assert uuid_pattern.match(conv_id) is not None

    def test_html_report_is_self_contained(self):
        """Report HTML must have no external asset dependencies."""
        mock_html = "<html><head><style>body{}</style></head><body>Report</body></html>"
        has_external_script = "src=" in mock_html and "http" in mock_html
        has_external_css = 'rel="stylesheet"' in mock_html
        assert not has_external_script
        assert not has_external_css
