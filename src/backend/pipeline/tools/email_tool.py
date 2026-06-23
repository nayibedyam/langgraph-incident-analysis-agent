"""Email delivery tool — renders templates and sends via sendmail or SMTP.

Templates live under ``pipeline/templates/`` and are rendered with Jinja2.
Default transport is ``sendmail`` (``/usr/sbin/sendmail``) which doesn't
require a running SMTP service. Set ``transport='smtp'`` to use a network
SMTP relay instead.
"""

from __future__ import annotations

import logging
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


def render_email_template(
    template_path: str,
    context: Dict[str, Any],
) -> str:
    """Render a Jinja2 template with *context*. Returns the rendered string."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    p = Path(template_path)
    env = Environment(
        loader=FileSystemLoader(str(p.parent)),
        undefined=StrictUndefined,
        autoescape=False,
    )
    template = env.get_template(p.name)
    return template.render(**context)


def send_email(
    *,
    subject: str,
    body_html: str,
    to_addresses: Iterable[str],
    from_address: str,
    cc_addresses: Optional[Iterable[str]] = None,
    attachment_paths: Optional[Iterable[str]] = None,
    transport: str = "sendmail",
    sendmail_path: str = "/usr/sbin/sendmail",
    smtp_host: str = "localhost",
    smtp_port: int = 25,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Send an HTML email with optional attachments.

    ``transport`` is ``"sendmail"`` (default) or ``"smtp"``.
    Returns a dict ``{ok, recipients, error}``.
    """
    to_list = [a for a in to_addresses if a]
    cc_list = [a for a in (cc_addresses or []) if a]
    if not to_list:
        return {"ok": False, "error": "no recipients"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.set_content("This message is in HTML. Please view in an HTML-capable client.")
    msg.add_alternative(body_html, subtype="html")

    attached: List[str] = []
    for path in attachment_paths or []:
        if not path:
            continue
        p = Path(path)
        if not p.exists() or not p.is_file():
            continue
        try:
            data = p.read_bytes()
            msg.add_attachment(
                data,
                maintype="application",
                subtype="octet-stream",
                filename=p.name,
            )
            attached.append(str(p))
        except OSError as exc:
            logger.warning("Failed to attach %s: %s", path, exc)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "transport": transport,
            "recipients": to_list + cc_list,
            "subject": subject,
            "attachments": attached,
        }

    recipients = to_list + cc_list
    transport = (transport or "sendmail").lower()

    if transport == "sendmail":
        try:
            proc = subprocess.run(
                [sendmail_path, "-i", "-f", from_address, *recipients],
                input=msg.as_bytes(),
                check=True,
                timeout=60,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return {
                "ok": True,
                "transport": "sendmail",
                "recipients": recipients,
                "attachments": attached,
                "stdout": proc.stdout.decode(errors="replace")[:500],
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "transport": "sendmail",
                "error": f"sendmail binary not found at {sendmail_path}",
                "recipients": recipients,
            }
        except subprocess.CalledProcessError as exc:
            return {
                "ok": False,
                "transport": "sendmail",
                "error": f"sendmail exit {exc.returncode}: "
                          f"{(exc.stderr or b'').decode(errors='replace')[:300]}",
                "recipients": recipients,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("sendmail send failed")
            return {"ok": False, "transport": "sendmail", "error": str(exc), "recipients": recipients}

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.send_message(msg)
        return {"ok": True, "transport": "smtp", "recipients": recipients, "attachments": attached}
    except Exception as exc:  # noqa: BLE001
        logger.exception("SMTP send failed")
        return {"ok": False, "transport": "smtp", "error": str(exc), "recipients": recipients}
