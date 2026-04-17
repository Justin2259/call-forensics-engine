# Call Forensics Engine

Two scripts that produce a complete forensic reconstruction of any phone call from a Genesys Cloud contact center. Investigation time reduced from 2 hours to 15 minutes.

---

## What It Does

Given a conversation ID, the engine pulls every available data point across multiple Genesys APIs and assembles them into a single report:

- **CDR**: Who called, when, duration, queue, agent, disposition
- **Analytics**: Participant segments, time-in-queue, hold durations, transfer chain
- **Flow execution history**: Every prompt played, DTMF digit entered, decision taken, variable set, data table looked up - in sequence
- **Recording metadata**: Whether a recording exists, its retention policy, media URLs
- **SIP signaling**: Full PCAP download + parsed summary of the SIP dialog and media negotiation
- **Agent names**: User IDs resolved to display names

---

## Scripts

### `investigate_call.py` - Full call investigation

```bash
python investigate_call.py <conversationId>
```

Pulls CDR, participant analytics, flow execution history, and recording metadata. Outputs structured JSON plus a human-readable summary.

**Requires:**
- Org-level flow execution data enabled (Genesys Admin > Architect > Settings > Execution Data)
- OAuth client with `Architect > Flow Instance > View/Search` permissions
- Analytics data is available for 31 days; flow execution data for 10 days

### `fetch_sip_pcap.py` - SIP trace download

```bash
python fetch_sip_pcap.py <conversationId>
```

Downloads the SIP signaling PCAP for a conversation and prints a parsed summary of the SIP dialog, response codes, and media negotiation. PCAP saved to `.tmp/`.

---

## Setup

```bash
pip install requests python-dotenv dpkt
```

`.env` required:
```
GENESYS_CLIENT_ID=your_client_id
GENESYS_CLIENT_SECRET=your_client_secret
GENESYS_REGION=mypurecloud.com
```

---

## API Coverage

| Data | Genesys API |
|------|-------------|
| CDR + participants | `/api/v2/analytics/conversations/{id}/details` |
| Flow execution history | `/api/v2/flows/instances/query` |
| Recording metadata | `/api/v2/conversations/{id}/recordings` |
| SIP PCAP download | `/api/v2/telephony/siptraces/download` |
| User name resolution | `/api/v2/users/{id}` |

---

## Architecture Context

Part of the [Enterprise AI Automation Platform](https://github.com/Justin2259/enterprise-ai-automation-platform) 3-layer architecture. The investigation directive (`directives/investigate_call.md`) tells the AI agent when to run these scripts and how to present findings.
