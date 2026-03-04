"""
Quick manual test: create a Teams meeting with lobby bypass and print the join link.
Run: python test_lobby_manual.py
"""
import tomllib
from datetime import datetime, timedelta, timezone
from graph_client import GraphClient, GraphConfig

# Load secrets
with open(".streamlit/secrets.toml", "rb") as f:
    secrets = tomllib.load(f)

cfg = GraphConfig(
    tenant_id=secrets["graph_tenant_id"],
    client_id=secrets["graph_client_id"],
    client_secret=secrets["graph_client_secret"],
    scheduler_mailbox=secrets["graph_scheduler_mailbox"],
)

client = GraphClient(cfg)

# Authenticate
print("1. Authenticating with Graph API...")
token = client.get_token()
print(f"   OK — token acquired (expires in ~1h)")

# Create a test event 15 minutes from now
start = datetime.now(timezone.utc) + timedelta(minutes=15)
end = start + timedelta(minutes=30)

payload = {
    "subject": "Lobby Bypass Test — DELETE ME",
    "body": {"contentType": "HTML", "content": "<p>Testing lobby bypass. Safe to delete.</p>"},
    "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
    "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
    "attendees": [],
    "isOnlineMeeting": True,
    "onlineMeetingProvider": "teamsForBusiness",
    "location": {"displayName": "Microsoft Teams"},
}

print("\n2. Creating Teams calendar event...")
created = client.create_event(payload)
event_id = created.get("id", "")
teams_url = (created.get("onlineMeeting") or {}).get("joinUrl") or ""

if not teams_url:
    print("   WARNING: No Teams join URL in response. Retrying after 2s...")
    import time
    time.sleep(2)
    refreshed = client.get_event(event_id)
    teams_url = (refreshed.get("onlineMeeting") or {}).get("joinUrl") or ""

if not teams_url:
    print("   FAILED: Could not get Teams join URL.")
    exit(1)

print(f"   OK — event created (id: {event_id[:20]}...)")
print(f"   Teams URL: {teams_url}")

# Now apply lobby bypass
print("\n3. Setting lobby bypass (everyone can join without waiting)...")
success = client.set_meeting_lobby_bypass(teams_url)

if success:
    print("   OK — lobby bypass configured!")
else:
    print("   FAILED — could not set lobby bypass.")
    print("   Check that the app has OnlineMeetings.ReadWrite.All permission in Azure AD.")

print(f"\n{'='*60}")
print(f"JOIN LINK: {teams_url}")
print(f"{'='*60}")
print(f"\nMeeting starts at: {start.strftime('%H:%M UTC')} (15 min from now)")
print(f"Event ID: {event_id}")
print(f"\nTo clean up, delete the event from the scheduler calendar.")
