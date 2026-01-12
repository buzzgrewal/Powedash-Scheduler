import base64
import io
import json
import os
import re
import uuid
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image
import streamlit as st

# --- Optional OpenAI (kept for PDF parsing flow) ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

from graph_client import GraphClient, GraphConfig, GraphAPIError, GraphAuthError
from audit_log import AuditLog, LogLevel, log_structured
from ics_utils import ICSInvite, stable_uid, ICSValidationError
from timezone_utils import to_utc, from_utc, iso_utc, is_valid_timezone, safe_zoneinfo


# ----------------------------
# Input Validation
# ----------------------------
import re as _re
from typing import Tuple as _Tuple

# Email regex (RFC 5322 simplified)
_EMAIL_REGEX = _re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

# Date/time patterns from OpenAI output
_DATE_REGEX = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME_REGEX = _re.compile(r'^\d{2}:\d{2}$')


class ValidationError(ValueError):
    """Raised when input validation fails."""
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def validate_email(email: str, field_name: str = "email") -> str:
    """Validate email format. Returns cleaned email or raises ValidationError."""
    if not email:
        raise ValidationError(field_name, "Email is required")
    email = email.strip().lower()
    if not _EMAIL_REGEX.match(email):
        raise ValidationError(field_name, f"Invalid email format: {email}")
    if len(email) > 254:  # RFC 5321 limit
        raise ValidationError(field_name, "Email too long (max 254 characters)")
    return email


def validate_email_optional(email: Optional[str], field_name: str = "email") -> Optional[str]:
    """Validate email if provided, return None if empty."""
    if not email or not email.strip():
        return None
    return validate_email(email, field_name)


def validate_slot(slot: dict) -> _Tuple[str, str, str]:
    """Validate slot dict from OpenAI parsing. Returns (date, start, end) tuple."""
    if not isinstance(slot, dict):
        raise ValidationError("slot", "Slot must be a dictionary")

    date = slot.get("date", "")
    start = slot.get("start", "")
    end = slot.get("end", "")

    if not _DATE_REGEX.match(date):
        raise ValidationError("slot.date", f"Invalid date format: {date}. Expected YYYY-MM-DD")
    if not _TIME_REGEX.match(start):
        raise ValidationError("slot.start", f"Invalid start time format: {start}. Expected HH:MM")
    if end and not _TIME_REGEX.match(end):
        raise ValidationError("slot.end", f"Invalid end time format: {end}. Expected HH:MM")

    return date, start, end


# ----------------------------
# Configuration helpers
# ----------------------------
def get_secret(key: str, default: Any = None) -> Any:
    # st.secrets behaves like a dict on Streamlit Cloud; local dev can use env vars too.
    try:
        if key in st.secrets:
            return st.secrets.get(key, default)
    except Exception:
        pass
    return os.getenv(key.upper(), default)


def get_default_timezone() -> str:
    return get_secret("default_timezone", "Europe/London")


def get_audit_log_path() -> str:
    return get_secret("audit_log_path", "audit_log.db")


def get_graph_config() -> Optional[GraphConfig]:
    tenant_id = get_secret("graph_tenant_id")
    client_id = get_secret("graph_client_id")
    client_secret = get_secret("graph_client_secret")
    scheduler_mailbox = get_secret("graph_scheduler_mailbox", "scheduling@powerdashhr.com")

    if not (tenant_id and client_id and client_secret and scheduler_mailbox):
        return None
    return GraphConfig(
        tenant_id=str(tenant_id),
        client_id=str(client_id),
        client_secret=str(client_secret),
        scheduler_mailbox=str(scheduler_mailbox),
    )


def graph_enabled() -> bool:
    return get_graph_config() is not None


def get_openai_client() -> Optional[Any]:
    """
    Create an OpenAI client using Streamlit secrets or environment variable.
    Keeps backward compatibility with older secret key names.
    """
    api_key = get_secret("openai_api_key") or get_secret("OPENAI_API_KEY")
    if not api_key:
        st.warning("OpenAI API key not found. PDF availability parsing will be limited.")
        return None
    if OpenAI is None:
        st.warning("OpenAI SDK not available (openai). PDF availability parsing will be limited.")
        return None
    return OpenAI(api_key=api_key)


# ----------------------------
# PDF / image parsing helpers (existing behavior)
# ----------------------------
def image_to_base64(image: Image.Image) -> str:
    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def parse_slots_from_image(image: Image.Image) -> List[Dict[str, str]]:
    """
    Use OpenAI vision to parse free/busy calendar images into slots.
    Expected JSON format:
    [
      {"date": "2025-12-03", "start": "09:00", "end": "09:30"},
      ...
    ]
    """
    client = get_openai_client()
    if not client:
        return []

    prompt = (
        "You are extracting FREE time slots from a calendar screenshot.\n"
        "Return ONLY valid JSON (no markdown) as a list of objects with keys: date (YYYY-MM-DD), start (HH:MM), end (HH:MM).\n"
        "Only include free slots that are at least 30 minutes.\n"
        "If no slots found, return an empty list []."
    )

    b64 = image_to_base64(image)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that returns strict JSON."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                },
            ],
        )
        content = resp.choices[0].message.content.strip() if resp.choices else ""
        # Strip code fences if present
        if content.startswith("```"):
            content = content.strip("`")
            if "\n" in content:
                content = content.split("\n", 1)[1].strip()

        slots = json.loads(content) if content else []
        valid_slots = []
        for s in slots:
            if isinstance(s, dict) and all(k in s for k in ("date", "start", "end")):
                valid_slots.append({"date": str(s["date"]), "start": str(s["start"]), "end": str(s["end"])})
        return valid_slots
    except json.JSONDecodeError as e:
        st.error(f"OpenAI returned invalid JSON: {e}")
        log_structured(
            LogLevel.ERROR,
            f"OpenAI JSON parse error: {e}",
            action="parse_slots_openai",
            error_type="json_decode_error",
        )
        return []
    except Exception as e:
        st.error(f"Failed to parse availability via OpenAI: {e}")
        log_structured(
            LogLevel.ERROR,
            f"OpenAI vision API error: {e}",
            action="parse_slots_openai",
            error_type=type(e).__name__,
            exc_info=True,
        )
        return []


def pdf_to_images(pdf_bytes: bytes, max_pages: int = 3) -> List[Image.Image]:
    """Convert PDF to images. Returns empty list on error instead of crashing."""
    images: List[Image.Image] = []
    doc = None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for i in range(min(len(doc), max_pages)):
            try:
                page = doc.load_page(i)
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                images.append(img)
            except Exception as e:
                log_structured(
                    LogLevel.WARNING,
                    f"Failed to process PDF page {i}: {e}",
                    action="pdf_page_process",
                    error_type="pdf_error",
                )
    except Exception as e:
        st.error(f"Failed to open PDF: {e}")
        log_structured(
            LogLevel.ERROR,
            f"Failed to open PDF: {e}",
            action="pdf_open",
            error_type="pdf_error",
            exc_info=True,
        )
    finally:
        if doc:
            doc.close()
    return images


def ensure_session_state() -> None:
    defaults = {
        "slots": [],
        "last_graph_event_id": "",
        "last_teams_join_url": "",
        "last_invite_uid": "",
        "last_invite_ics_bytes": b"",
        "selected_timezone": get_default_timezone(),
        "duration_minutes": 30,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def format_slot_label(slot: Dict[str, str]) -> str:
    return f"{slot['date']} {slot['start']}–{slot['end']}"


# ----------------------------
# Email helpers (existing, with updated secret key names)
# ----------------------------
def build_scheduling_email(role_title: str, recruiter_name: str, slots: List[Dict[str, str]]) -> str:
    slot_lines = "\n".join([f"- {format_slot_label(s)}" for s in slots]) if slots else "- (No slots extracted)"
    return f"""Hi there,

Thanks for your time. Please choose one of the following interview times for the role: {role_title}

Available slots:
{slot_lines}

Reply with your preferred option and we will confirm the invite.

Best regards,
{recruiter_name}
Talent Acquisition
"""


def _smtp_cfg() -> Optional[Dict[str, Any]]:
    # New keys (preferred)
    host = get_secret("smtp_host")
    port = get_secret("smtp_port")
    username = get_secret("smtp_username")
    password = get_secret("smtp_password")
    smtp_from = get_secret("smtp_from")

    # Backward-compat keys (older app)
    if not host:
        host = get_secret("smtp_server")

    if not (host and username and password):
        return None

    return {
        "host": str(host),
        "port": int(port or 587),
        "username": str(username),
        "password": str(password),
        "from": str(smtp_from or username),
    }


def send_email_smtp(
    subject: str,
    body: str,
    to_emails: List[str],
    cc_emails: Optional[List[str]] = None,
    attachment: Optional[Dict[str, Any]] = None,
) -> bool:
    cfg = _smtp_cfg()
    if not cfg:
        st.warning("SMTP is not configured in secrets; email send is disabled.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join([e for e in to_emails if e])
    if cc_emails:
        msg["Cc"] = ", ".join([e for e in cc_emails if e])
    msg.set_content(body)

    if attachment:
        msg.add_attachment(
            attachment["data"],
            maintype=attachment.get("maintype", "application"),
            subtype=attachment.get("subtype", "octet-stream"),
            filename=attachment.get("filename", "attachment.bin"),
        )

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError as e:
        st.error(f"SMTP authentication failed: {e}")
        log_structured(
            LogLevel.ERROR,
            f"SMTP authentication failed: {e}",
            action="smtp_send",
            error_type="smtp_auth_error",
        )
        return False
    except smtplib.SMTPException as e:
        st.error(f"SMTP send failed: {e}")
        log_structured(
            LogLevel.ERROR,
            f"SMTP send failed: {e}",
            action="smtp_send",
            error_type="smtp_error",
        )
        return False
    except Exception as e:
        st.error(f"SMTP send failed: {e}")
        log_structured(
            LogLevel.ERROR,
            f"SMTP send failed: {e}",
            action="smtp_send",
            error_type=type(e).__name__,
            exc_info=True,
        )
        return False


def send_email_graph(
    subject: str,
    body: str,
    to_emails: List[str],
    cc_emails: Optional[List[str]] = None,
    attachment: Optional[Dict[str, Any]] = None,
) -> bool:
    """Send email using Microsoft Graph API."""
    cfg = get_graph_config()
    if not cfg:
        st.warning("Graph is not configured. Add graph_tenant_id, graph_client_id, graph_client_secret, graph_scheduler_mailbox in Streamlit secrets.")
        return False

    try:
        client = GraphClient(cfg)
        graph_attachment = None
        if attachment:
            graph_attachment = {
                "name": attachment.get("filename", "attachment.bin"),
                "contentBytes": attachment.get("data"),
                "contentType": f"{attachment.get('maintype', 'application')}/{attachment.get('subtype', 'octet-stream')}",
            }
        client.send_mail(
            subject=subject,
            body=body,
            to_recipients=[e for e in to_emails if e],
            cc_recipients=[e for e in (cc_emails or []) if e] or None,
            content_type="Text",
            attachment=graph_attachment,
        )
        return True
    except Exception as e:
        st.error(f"Graph email send failed: {e}")
        return False


def fetch_unread_emails_graph() -> Tuple[List[Dict[str, Any]], Optional[str], bool]:
    """
    Fetch unread emails from scheduler mailbox via Microsoft Graph API.
    Returns (emails, error_message, is_configured) tuple.
    - error_message is None on success
    - is_configured is False if Graph credentials are missing

    Uses the same Graph credentials as calendar operations.
    """
    cfg = get_graph_config()
    if not cfg:
        return [], None, False  # Graph not configured

    try:
        from graph_client import GraphClient
        client = GraphClient(cfg)
        messages = client.fetch_unread_messages(top=50)

        emails: List[Dict[str, Any]] = []
        for msg in messages:
            from_addr = ""
            from_data = msg.get("from", {})
            if from_data:
                email_addr = from_data.get("emailAddress", {})
                from_addr = email_addr.get("address", "")

            # Get body content (prefer text, fall back to HTML)
            body_content = msg.get("bodyPreview", "")
            body_data = msg.get("body", {})
            if body_data and body_data.get("content"):
                body_content = body_data.get("content", "")
                # Strip HTML tags if content type is HTML
                if body_data.get("contentType") == "html":
                    import re
                    body_content = re.sub(r'<[^>]+>', '', body_content)
                    body_content = body_content.strip()

            emails.append({
                "id": msg.get("id", ""),
                "from": from_addr,
                "subject": msg.get("subject", ""),
                "date": msg.get("receivedDateTime", ""),
                "body": body_content,
            })

        return emails, None, True  # Success, configured

    except GraphAuthError as e:
        log_structured(
            LogLevel.ERROR,
            f"Graph authentication failed: {e}",
            action="graph_fetch_messages",
            error_type="graph_auth_error",
        )
        return [], f"Graph authentication failed: {e}", True
    except GraphAPIError as e:
        log_structured(
            LogLevel.ERROR,
            f"Graph API error: {e}",
            action="graph_fetch_messages",
            error_type="graph_api_error",
            details={"status_code": e.status_code},
        )
        return [], f"Graph API error: {e}", True
    except Exception as e:
        log_structured(
            LogLevel.ERROR,
            f"Failed to fetch emails via Graph: {e}",
            action="graph_fetch_messages",
            error_type="graph_error",
            exc_info=True,
        )
        return [], f"Failed to fetch emails: {e}", True


def detect_slot_choice_from_text(text: str, slots: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """
    Heuristic: find a slot label or date+time mention in a reply.
    """
    t = (text or "").lower()
    for s in slots:
        label = format_slot_label(s).lower()
        if label in t:
            return s

    # fallback: look for YYYY-MM-DD and HH:MM
    m_date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", t)
    m_time = re.search(r"\b(\d{1,2}:\d{2})\b", t)
    if m_date and m_time:
        date = m_date.group(1)
        start = m_time.group(1).zfill(5)
        for s in slots:
            if s["date"] == date and s["start"] == start:
                return s
        return {"date": date, "start": start, "end": ""}

    return None


# ----------------------------
# Graph + ICS helpers
# ----------------------------
def _make_graph_client() -> Optional[GraphClient]:
    cfg = get_graph_config()
    if not cfg:
        return None
    return GraphClient(cfg)


def _graph_event_payload(
    *,
    subject: str,
    body_html: str,
    start_local: datetime,
    end_local: datetime,
    time_zone: str,
    attendees: List[Tuple[str, str]],
    is_teams: bool,
    location: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "start": {"dateTime": start_local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": time_zone},
        "end": {"dateTime": end_local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": time_zone},
        "attendees": [{"emailAddress": {"address": e, "name": n or e}, "type": "required"} for (e, n) in attendees],
    }

    if is_teams:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"
        payload["location"] = {"displayName": "Microsoft Teams"}
    else:
        payload["location"] = {"displayName": location or "Interview"}

    return payload


def _build_ics(
    *,
    organizer_email: str,
    organizer_name: str,
    attendee_emails: List[str],
    summary: str,
    description: str,
    dtstart_utc: datetime,
    dtend_utc: datetime,
    location: str,
    url: str,
    uid_hint: str,
) -> bytes:
    uid = stable_uid(uid_hint, organizer_email, ",".join(attendee_emails), dtstart_utc.isoformat())
    inv = ICSInvite(
        uid=uid,
        dtstart_utc=dtstart_utc,
        dtend_utc=dtend_utc,
        summary=summary,
        description=description,
        organizer_email=organizer_email,
        organizer_name=organizer_name,
        attendee_emails=attendee_emails,
        location=location,
        url=url,
    )
    return inv.to_ics()


# ----------------------------
# Streamlit UI
# ----------------------------
def main() -> None:
    st.set_page_config(page_title="PowerDash Interview Scheduler", layout="wide")
    ensure_session_state()

    audit = AuditLog(get_audit_log_path())

    st.title("PowerDash Interview Scheduler")

    tab_new, tab_inbox, tab_invites, tab_audit, tab_diag = st.tabs(
        ["New Scheduling Request", "Scheduler Inbox", "Calendar Invites", "Audit Log", "Graph Diagnostics"]
    )

    # ========= TAB: New Scheduling Request =========
    with tab_new:
        st.subheader("New Scheduling Request")

        col_left, col_center, col_right = st.columns([1.2, 1.5, 1.2], gap="large")

        with col_left:
            st.markdown("#### Hiring Manager & Recruiter")
            role_title = st.text_input("Role Title", key="role_title")
            hiring_manager_name = st.text_input("Hiring Manager Name", key="hm_name")
            hiring_manager_email = st.text_input("Hiring Manager Email (required)", key="hm_email")
            recruiter_name = st.text_input("Recruiter Name", key="rec_name")
            recruiter_email = st.text_input("Recruiter Email (optional attendee)", key="rec_email")
            scheduler_mailbox = get_secret("graph_scheduler_mailbox", "scheduling@powerdashhr.com")
            st.text_input("Recruiter / Scheduling Mailbox Email", value=str(scheduler_mailbox), disabled=True)

        with col_center:
            st.markdown("#### Upload Availability")
            upload = st.file_uploader(
                "Free/busy screenshot (PDF, PNG, JPG, JPEG)",
                type=["pdf", "png", "jpg", "jpeg"],
                key="availability_upload",
            )
            st.session_state["duration_minutes"] = st.number_input(
                "Interview duration (minutes)", min_value=15, max_value=240, step=15, value=int(st.session_state["duration_minutes"])
            )
            tz_name = st.selectbox(
                "Display timezone",
                options=_common_timezones(),
                index=_common_timezones().index(st.session_state["selected_timezone"])
                if st.session_state["selected_timezone"] in _common_timezones()
                else 0,
                key="selected_timezone",
            )

            parse_btn = st.button("Parse Availability", type="primary")

            if parse_btn and upload is not None:
                slots = _parse_availability_upload(upload)
                st.session_state["slots"] = slots
                st.success(f"Extracted {len(slots)} slot(s).")

            st.markdown("#### Extracted Slots")
            if st.session_state["slots"]:
                st.info("Select a slot to create an invite, or generate a candidate email.")
                slot_labels = [format_slot_label(s) for s in st.session_state["slots"]]
                selected_label = st.selectbox("Select slot", options=slot_labels, key="selected_slot_label")
                selected_slot = st.session_state["slots"][slot_labels.index(selected_label)]
            else:
                st.info("No slots extracted yet. Upload a calendar view and click Parse Availability.")
                selected_slot = None

        with col_right:
            st.markdown("#### Candidate")
            candidate_name = st.text_input("Candidate Name", key="cand_name")
            candidate_email = st.text_input("Candidate Email (required)", key="cand_email")

            st.markdown("#### Invite details")
            is_teams = st.selectbox("Interview type", options=["Teams", "Non-Teams"], index=0, key="interview_type") == "Teams"
            subject = st.text_input("Subject/title", value=f"Interview: {role_title}" if role_title else "Interview", key="subject")
            agenda = st.text_area("Description/agenda", value="Interview discussion.", key="agenda")
            location = st.text_input("Location (non-Teams)", value="", key="location")

            include_recruiter = st.checkbox("Include recruiter as attendee", value=False, key="include_recruiter")

            st.markdown("----")
            st.markdown("#### Actions")

            # Generate email to candidate (existing behavior)
            if st.button("Generate Candidate Scheduling Email"):
                body = build_scheduling_email(role_title, recruiter_name or "Recruiter", st.session_state["slots"])
                st.session_state["candidate_email_body"] = body

            if "candidate_email_body" in st.session_state and st.session_state["candidate_email_body"]:
                st.text_area("Email preview", st.session_state["candidate_email_body"], height=200)
                if st.button("Send Email"):
                    ok = send_email_graph(
                        subject=f"Interview availability: {role_title}",
                        body=st.session_state["candidate_email_body"],
                        to_emails=[candidate_email] if candidate_email else [],
                        cc_emails=[recruiter_email] if recruiter_email else None,
                    )
                    audit.log(
                        "graph_sent_scheduling_email" if ok else "graph_send_failed",
                        actor=recruiter_email or "",
                        candidate_email=candidate_email or "",
                        hiring_manager_email=hiring_manager_email or "",
                        recruiter_email=recruiter_email or "",
                        role_title=role_title or "",
                        payload={"subject": f"Interview availability: {role_title}"},
                        status="success" if ok else "failed",
                        error_message="" if ok else "Graph email send failed",
                    )
                    if ok:
                        st.success("Email sent.")
                    else:
                        st.error("Email send failed (see message above).")

            # Create Graph event
            create_disabled = not (selected_slot and hiring_manager_email and candidate_email)
            if st.button("Create & Send Interview Invite", disabled=create_disabled):
                _handle_create_invite(
                    audit=audit,
                    selected_slot=selected_slot,
                    tz_name=tz_name,
                    duration_minutes=int(st.session_state["duration_minutes"]),
                    role_title=role_title,
                    subject=subject,
                    agenda=agenda,
                    location=location,
                    is_teams=is_teams,
                    candidate=(candidate_email, candidate_name),
                    hiring_manager=(hiring_manager_email, hiring_manager_name),
                    recruiter=(recruiter_email, recruiter_name),
                    include_recruiter=include_recruiter,
                )

            # ICS fallback download button (available after generation)
            if st.session_state.get("last_invite_ics_bytes"):
                st.download_button(
                    "Download .ics (Add to calendar)",
                    data=st.session_state["last_invite_ics_bytes"],
                    file_name="powerdash_interview_invite.ics",
                    mime="text/calendar",
                )
                audit.log(
                    "ics_downloaded",
                    actor=recruiter_email or "",
                    candidate_email=candidate_email or "",
                    hiring_manager_email=hiring_manager_email or "",
                    recruiter_email=recruiter_email or "",
                    role_title=role_title or "",
                    event_id=st.session_state.get("last_graph_event_id", ""),
                    payload={"uid": st.session_state.get("last_invite_uid", "")},
                    status="success",
                )

                # Optional email ICS via Graph
                if st.button("Email .ics (optional)"):
                    ok = send_email_graph(
                        subject=subject,
                        body=agenda,
                        to_emails=[candidate_email, hiring_manager_email] + ([recruiter_email] if include_recruiter and recruiter_email else []),
                        attachment={
                            "data": st.session_state["last_invite_ics_bytes"],
                            "maintype": "text",
                            "subtype": "calendar",
                            "filename": "invite.ics",
                        },
                    )
                    audit.log(
                        "graph_sent_ics" if ok else "graph_send_failed",
                        actor=recruiter_email or "",
                        candidate_email=candidate_email or "",
                        hiring_manager_email=hiring_manager_email or "",
                        recruiter_email=recruiter_email or "",
                        role_title=role_title or "",
                        event_id=st.session_state.get("last_graph_event_id", ""),
                        payload={"uid": st.session_state.get("last_invite_uid", "")},
                        status="success" if ok else "failed",
                        error_message="" if ok else "Graph email send failed",
                    )
                    st.success("ICS emailed.") if ok else st.error("Failed to email ICS.")

    # ========= TAB: Scheduler Inbox =========
    with tab_inbox:
        st.subheader("Scheduler Inbox")
        st.caption("Reads unread emails from the scheduler mailbox via Microsoft Graph API.")
        emails, graph_error, is_configured = fetch_unread_emails_graph()
        if not is_configured:
            st.warning("Graph API is not configured. Add graph_tenant_id, graph_client_id, graph_client_secret, and graph_scheduler_mailbox to your secrets.")
        elif graph_error:
            st.error(f"Failed to fetch emails: {graph_error}")
        elif not emails:
            st.success("✓ Connected to mailbox. No unread emails found.")
        else:
            st.write(f"Found {len(emails)} unread email(s).")
            for i, e in enumerate(emails, start=1):
                with st.expander(f"{i}. {e['subject'] or '(no subject)'} — {e['from']}"):
                    st.write(e.get("date", ""))
                    body = e.get("body", "")
                    st.text_area("Body", body, height=160)

                    if st.session_state["slots"]:
                        choice = detect_slot_choice_from_text(body, st.session_state["slots"])
                        if choice:
                            st.success(f"Detected slot choice: {choice.get('date')} {choice.get('start')}")
                        else:
                            st.info("No slot choice detected from this email.")

    # ========= TAB: Calendar Invites =========
    with tab_invites:
        st.subheader("Calendar Invites")
        st.caption("Manage scheduled interviews (reschedule/cancel).")

        interviews = audit.list_interviews(limit=200)
        if not interviews:
            st.info("No interviews stored yet. Create an invite from the first tab.")
        else:
            # show compact table
            st.dataframe(
                [
                    {
                        "created_utc": r["created_utc"],
                        "role_title": r["role_title"],
                        "candidate": r["candidate_email"],
                        "hiring_manager": r["hiring_manager_email"],
                        "start_utc": r["start_utc"],
                        "graph_event_id": r["graph_event_id"],
                        "teams_join_url": (r["teams_join_url"][:45] + "…") if r.get("teams_join_url") else "",
                        "status": r.get("last_status", ""),
                    }
                    for r in interviews
                ],
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("----")
            st.markdown("#### Reschedule / Cancel")

            event_ids = [r["graph_event_id"] for r in interviews if r.get("graph_event_id")]
            selected_event_id = st.selectbox("Select event", options=event_ids)
            selected_row = next((r for r in interviews if r.get("graph_event_id") == selected_event_id), None)

            if selected_row:
                display_tz = st.selectbox("Display timezone", options=_common_timezones(), index=_tz_index(selected_row.get("display_timezone")))
                st.write(f"Current start (UTC): {selected_row.get('start_utc')}")
                st.write(f"Current end (UTC): {selected_row.get('end_utc')}")
                try:
                    start_local = from_utc(datetime.fromisoformat(selected_row["start_utc"]), display_tz)
                except Exception:
                    start_local = None

                new_date = st.date_input("New date", value=start_local.date() if start_local else datetime.now().date())
                new_time = st.time_input("New time", value=start_local.time().replace(second=0, microsecond=0) if start_local else datetime.now().time().replace(second=0, microsecond=0))
                new_duration = st.number_input("Duration (minutes)", min_value=15, max_value=240, step=15, value=int(selected_row.get("duration_minutes") or 30))

                colA, colB = st.columns(2)
                with colA:
                    if st.button("Reschedule", type="primary"):
                        _handle_reschedule(
                            audit=audit,
                            event_id=selected_event_id,
                            new_date=new_date,
                            new_time=new_time,
                            duration_minutes=int(new_duration),
                            tz_name=display_tz,
                            context_row=selected_row,
                        )
                with colB:
                    if st.button("Cancel", type="secondary"):
                        _handle_cancel(audit=audit, event_id=selected_event_id, context_row=selected_row)

    # ========= TAB: Audit Log =========
    with tab_audit:
        st.subheader("Audit Log")
        st.caption("Append-only log of scheduling actions.")
        rows = audit.list_recent_audit(limit=300)
        if not rows:
            st.info("No audit entries yet.")
        else:
            st.dataframe(
                [
                    {
                        "timestamp_utc": r["timestamp_utc"],
                        "action": r["action"],
                        "status": r["status"],
                        "candidate": r["candidate_email"],
                        "hiring_manager": r["hiring_manager_email"],
                        "event_id": r["event_id"],
                        "error": (r["error_message"][:80] + "…") if r.get("error_message") and len(r["error_message"]) > 80 else (r.get("error_message") or ""),
                    }
                    for r in rows
                ],
                use_container_width=True,
                hide_index=True,
            )

            with st.expander("Show raw payload for a row"):
                idx = st.number_input("Row index (0 = most recent)", min_value=0, max_value=max(0, len(rows) - 1), value=0)
                st.json(rows[int(idx)])

    # ========= TAB: Graph Diagnostics =========
    with tab_diag:
        st.subheader("Graph Diagnostics")
        st.caption("Use this to verify Graph auth and mailbox access. No secrets are displayed.")

        cfg = get_graph_config()
        if not cfg:
            st.warning("Graph is not configured. Add graph_tenant_id, graph_client_id, graph_client_secret, graph_scheduler_mailbox in Streamlit secrets.")
        else:
            st.code(f"Scheduler mailbox: {cfg.scheduler_mailbox}")
            client = GraphClient(cfg)

            if st.button("Test token acquisition"):
                try:
                    token = client.get_token(force_refresh=True)
                    st.success(f"Token OK (length={len(token)})")
                    audit.log("graph_token_ok", payload={"length": len(token)}, status="success")
                except Exception as e:
                    st.error(str(e))
                    audit.log("graph_token_failed", payload={"error": str(e)}, status="failed", error_message=str(e))

            if st.button("Test calendar read (top 5)"):
                try:
                    data = client.test_calendar_read(top=5)
                    st.success("Calendar read OK.")
                    st.json(data)
                    audit.log("graph_calendar_read_ok", payload={"top": 5}, status="success")
                except GraphAPIError as e:
                    st.error(f"{e} — details below")
                    st.json(e.response_json)
                    audit.log("graph_calendar_read_failed", payload=e.response_json, status="failed", error_message=str(e))

            st.markdown("----")
            st.markdown("#### Dummy event (optional)")
            dry_run = st.checkbox("Dry run (do not create)", value=True)
            tz_name = st.selectbox("Timezone", options=_common_timezones(), index=_common_timezones().index(get_default_timezone()) if get_default_timezone() in _common_timezones() else 0)
            dt = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=2)
            date = st.date_input("Date", value=dt.date())
            time = st.time_input("Time", value=dt.time())
            start_local = datetime.combine(date, time).replace(tzinfo=_zoneinfo(tz_name))
            end_local = start_local + timedelta(minutes=30)

            if st.button("Create dummy event"):
                try:
                    start_dt = {"dateTime": start_local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name}
                    end_dt = {"dateTime": end_local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name}
                    out = client.create_dummy_event("PowerDash Diagnostics", start_dt, end_dt, dry_run=dry_run)
                    st.success("OK")
                    st.json(out)
                    audit.log("graph_dummy_event_ok", payload={"dry_run": dry_run}, status="success")
                except GraphAPIError as e:
                    st.error(f"{e}")
                    st.json(e.response_json)
                    audit.log("graph_dummy_event_failed", payload=e.response_json, status="failed", error_message=str(e))


# ----------------------------
# Internal UI handlers
# ----------------------------
def _parse_availability_upload(upload) -> List[Dict[str, str]]:
    data = upload.read()
    name = (upload.name or "").lower()
    if name.endswith(".pdf"):
        imgs = pdf_to_images(data, max_pages=3)
        slots: List[Dict[str, str]] = []
        for img in imgs:
            slots.extend(parse_slots_from_image(img))
        # de-dup
        uniq = {(s["date"], s["start"], s["end"]): s for s in slots}
        return list(uniq.values())
    else:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        return parse_slots_from_image(img)


def _zoneinfo(tz_name: str):
    """Get ZoneInfo with validation. Falls back to UTC if invalid."""
    zi, was_valid = safe_zoneinfo(tz_name, fallback="UTC")
    if not was_valid:
        st.warning(f"Invalid timezone '{tz_name}', using UTC")
    return zi


def _common_timezones() -> List[str]:
    # Keep concise; you can expand later.
    return [
        "UTC",
        "Europe/London",
        "Europe/Dublin",
        "Europe/Paris",
        "Europe/Rome",
        "Europe/Berlin",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Toronto",
        "America/Sao_Paulo",
        "Asia/Dubai",
        "Asia/Kolkata",
        "Asia/Singapore",
        "Asia/Tokyo",
        "Australia/Sydney",
    ]


def _tz_index(tz_name: str | None) -> int:
    tzs = _common_timezones()
    if tz_name and tz_name in tzs:
        return tzs.index(tz_name)
    return tzs.index(get_default_timezone()) if get_default_timezone() in tzs else 0


def _handle_create_invite(
    *,
    audit: AuditLog,
    selected_slot: Dict[str, str],
    tz_name: str,
    duration_minutes: int,
    role_title: str,
    subject: str,
    agenda: str,
    location: str,
    is_teams: bool,
    candidate: Tuple[str, str],
    hiring_manager: Tuple[str, str],
    recruiter: Tuple[str, str],
    include_recruiter: bool,
) -> None:
    candidate_email_raw, candidate_name = candidate
    hm_email_raw, hm_name = hiring_manager
    rec_email_raw, rec_name = recruiter

    # === INPUT VALIDATION ===
    # Validate timezone
    if not is_valid_timezone(tz_name):
        st.warning(f"Invalid timezone '{tz_name}', using UTC")
        tz_name = "UTC"

    # Validate emails
    try:
        candidate_email = validate_email(candidate_email_raw, "Candidate email")
        hm_email = validate_email(hm_email_raw, "Hiring manager email")
        rec_email = validate_email_optional(rec_email_raw, "Recruiter email")
    except ValidationError as e:
        st.error(f"Validation error: {e.message}")
        return

    # Validate slot format
    try:
        validate_slot(selected_slot)
    except ValidationError as e:
        st.error(f"Invalid time slot: {e.message}")
        return

    # Parse selected slot into a local datetime
    try:
        start_local_naive = datetime.fromisoformat(f"{selected_slot['date']}T{selected_slot['start']}:00")
    except ValueError as e:
        st.error(f"Selected slot has invalid date/time format: {e}")
        return

    zi, _ = safe_zoneinfo(tz_name, fallback="UTC")
    start_local = start_local_naive.replace(tzinfo=zi)
    end_local = start_local + timedelta(minutes=duration_minutes)

    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)

    # === IDEMPOTENCY CHECK ===
    existing = audit.interview_exists(
        candidate_email=candidate_email,
        hiring_manager_email=hm_email,
        role_title=role_title,
        start_utc=iso_utc(start_utc),
    )
    if existing:
        st.warning(
            f"An interview already exists for this candidate at this time. "
            f"Event ID: {existing.get('graph_event_id', 'N/A')}"
        )
        # Use a unique key based on the slot to avoid Streamlit duplicate key errors
        checkbox_key = f"force_dup_{selected_slot['date']}_{selected_slot['start']}"
        if not st.checkbox("Create duplicate anyway?", key=checkbox_key):
            return

    attendees: List[Tuple[str, str]] = [(candidate_email, candidate_name), (hm_email, hm_name)]
    if include_recruiter and rec_email:
        attendees.append((rec_email, rec_name))

    organizer_email = str(get_secret("graph_scheduler_mailbox", "scheduling@powerdashhr.com"))
    organizer_name = "PowerDash Scheduler"

    # Always generate ICS (so we have a fallback even if Graph works)
    ics_bytes = _build_ics(
        organizer_email=organizer_email,
        organizer_name=organizer_name,
        attendee_emails=[a[0] for a in attendees],
        summary=subject,
        description=agenda,
        dtstart_utc=start_utc,
        dtend_utc=end_utc,
        location=("Microsoft Teams" if is_teams else (location or "Interview")),
        url="",
        uid_hint=f"{role_title}|{candidate_email}|{hm_email}",
    )
    st.session_state["last_invite_ics_bytes"] = ics_bytes
    st.session_state["last_invite_uid"] = stable_uid(f"{role_title}|{candidate_email}|{hm_email}", organizer_email, start_utc.isoformat())
    audit.log(
        "ics_generated",
        actor=rec_email or "",
        candidate_email=candidate_email,
        hiring_manager_email=hm_email,
        recruiter_email=rec_email or "",
        role_title=role_title,
        payload={"uid": st.session_state["last_invite_uid"]},
        status="success",
    )

    client = _make_graph_client()
    if not client:
        st.warning("Graph is not configured. Using .ics fallback only.")
        return

    body_html = (agenda or "").replace("\n", "<br>")

    payload = _graph_event_payload(
        subject=subject,
        body_html=body_html,
        start_local=start_local,
        end_local=end_local,
        time_zone=tz_name,
        attendees=attendees,
        is_teams=is_teams,
        location=location,
    )

    try:
        created = client.create_event(payload)
        event_id = created.get("id", "")
        teams_url = ""
        if is_teams:
            teams_url = (created.get("onlineMeeting") or {}).get("joinUrl") or ""
        st.session_state["last_graph_event_id"] = event_id
        st.session_state["last_teams_join_url"] = teams_url

        # Re-generate ICS including Teams URL if present (better fallback)
        if teams_url:
            st.session_state["last_invite_ics_bytes"] = _build_ics(
                organizer_email=organizer_email,
                organizer_name=organizer_name,
                attendee_emails=[a[0] for a in attendees],
                summary=subject,
                description=agenda,
                dtstart_utc=start_utc,
                dtend_utc=end_utc,
                location="Microsoft Teams",
                url=teams_url,
                uid_hint=f"{role_title}|{candidate_email}|{hm_email}",
            )

        audit.log(
            "graph_create_event",
            actor=rec_email or "",
            candidate_email=candidate_email,
            hiring_manager_email=hm_email,
            recruiter_email=rec_email or "",
            role_title=role_title,
            event_id=event_id,
            payload=payload,
            status="success",
        )
        audit.upsert_interview(
            role_title=role_title,
            candidate_email=candidate_email,
            hiring_manager_email=hm_email,
            recruiter_email=rec_email or "",
            duration_minutes=duration_minutes,
            start_utc=iso_utc(start_utc),
            end_utc=iso_utc(end_utc),
            display_timezone=tz_name,
            graph_event_id=event_id,
            teams_join_url=teams_url,
            subject=subject,
            last_status="created",
        )

        st.success("Invite created and sent via Microsoft Graph.")
        if teams_url:
            st.link_button("Open Teams meeting link", teams_url)
    except (GraphAuthError, GraphAPIError) as e:
        details = getattr(e, "response_json", None)
        st.error("Graph scheduling failed. .ics fallback is available for download.")
        if details:
            st.json(details)
        audit.log(
            "graph_create_failed",
            actor=rec_email or "",
            candidate_email=candidate_email,
            hiring_manager_email=hm_email,
            recruiter_email=rec_email or "",
            role_title=role_title,
            payload={"error": str(e), "details": details},
            status="failed",
            error_message=str(e),
        )


def _handle_reschedule(
    *,
    audit: AuditLog,
    event_id: str,
    new_date,
    new_time,
    duration_minutes: int,
    tz_name: str,
    context_row: Dict[str, Any],
) -> None:
    client = _make_graph_client()
    if not client:
        st.error("Graph is not configured.")
        return

    start_local = datetime.combine(new_date, new_time).replace(tzinfo=_zoneinfo(tz_name))
    end_local = start_local + timedelta(minutes=duration_minutes)
    start_utc = to_utc(start_local)
    end_utc = to_utc(end_local)

    patch = {
        "start": {"dateTime": start_local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name},
        "end": {"dateTime": end_local.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz_name},
    }

    try:
        client.patch_event(event_id, patch, send_updates="all")
        audit.log(
            "graph_reschedule_event",
            actor=context_row.get("recruiter_email", "") or "",
            candidate_email=context_row.get("candidate_email", "") or "",
            hiring_manager_email=context_row.get("hiring_manager_email", "") or "",
            recruiter_email=context_row.get("recruiter_email", "") or "",
            role_title=context_row.get("role_title", "") or "",
            event_id=event_id,
            payload=patch,
            status="success",
        )
        st.success("Event rescheduled. Attendees should receive updated invites.")
    except GraphAPIError as e:
        st.error("Reschedule failed.")
        st.json(e.response_json)
        audit.log(
            "graph_reschedule_failed",
            actor=context_row.get("recruiter_email", "") or "",
            candidate_email=context_row.get("candidate_email", "") or "",
            hiring_manager_email=context_row.get("hiring_manager_email", "") or "",
            recruiter_email=context_row.get("recruiter_email", "") or "",
            role_title=context_row.get("role_title", "") or "",
            event_id=event_id,
            payload=e.response_json,
            status="failed",
            error_message=str(e),
        )


def _handle_cancel(*, audit: AuditLog, event_id: str, context_row: Dict[str, Any]) -> None:
    client = _make_graph_client()
    if not client:
        st.error("Graph is not configured.")
        return

    try:
        client.delete_event(event_id)
        audit.log(
            "graph_cancel_event",
            actor=context_row.get("recruiter_email", "") or "",
            candidate_email=context_row.get("candidate_email", "") or "",
            hiring_manager_email=context_row.get("hiring_manager_email", "") or "",
            recruiter_email=context_row.get("recruiter_email", "") or "",
            role_title=context_row.get("role_title", "") or "",
            event_id=event_id,
            payload={"event_id": event_id},
            status="success",
        )
        st.success("Event cancelled. Attendees should receive cancellation notices.")
    except GraphAPIError as e:
        st.error("Cancel failed.")
        st.json(e.response_json)
        audit.log(
            "graph_cancel_failed",
            actor=context_row.get("recruiter_email", "") or "",
            candidate_email=context_row.get("candidate_email", "") or "",
            hiring_manager_email=context_row.get("hiring_manager_email", "") or "",
            recruiter_email=context_row.get("recruiter_email", "") or "",
            role_title=context_row.get("role_title", "") or "",
            event_id=event_id,
            payload=e.response_json,
            status="failed",
            error_message=str(e),
        )


if __name__ == "__main__":
    main()
