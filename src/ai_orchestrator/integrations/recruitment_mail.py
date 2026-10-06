"""Bounded Gmail CV intake: SMTP self-tests and read-only IMAP with provenance.

Protocol sources are linked in FUTURE_WORK.md. No credential or unrelated inbox
content is returned. Downloaded resumes are untrusted data, never instructions.
"""

from __future__ import annotations

import hashlib
import http.client
import imaplib
import ipaddress
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import zipfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from ai_orchestrator.config.settings import DEV_DATA_DIR, Settings, get_settings


def extract_cv(data: bytes, suffix: str, target: Path) -> str:
    if suffix == ".txt":
        text = data.decode("utf-8", errors="strict")
    elif suffix == ".docx":
        with zipfile.ZipFile(target) as archive:
            member = archive.getinfo("word/document.xml")
            if member.file_size > 2_000_000:
                raise ValueError("DOCX expanded text exceeds limit")
            xml_text = archive.read(member).decode("utf-8")
            if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
                raise ValueError("CV XML declarations are refused")
            xml = ElementTree.fromstring(xml_text)  # noqa: S314 -- DTD/entities refused above
            text = "\n".join(n.text or "" for n in xml.iter() if n.tag.endswith("}t"))
    elif suffix == ".pdf":
        executable = shutil.which("pdftotext")
        if not executable:
            raise ValueError("PDF extraction requires the configured native pdftotext executable")
        process = subprocess.run(  # noqa: S603 -- resolved executable, hash filename, no shell
            [executable, "-nopgbrk", str(target), "-"], capture_output=True, timeout=20, check=True
        )
        text = process.stdout.decode("utf-8", errors="strict")
    else:
        raise ValueError("Only PDF, DOCX and UTF-8 TXT CVs are supported")
    text = text.strip()
    if not text or len(text) > 120_000:
        raise ValueError(
            "CV has no extractable text or exceeds text limit; OCR/manual review needed"
        )
    return text


def store_cv(data: bytes, filename: str, org: str, run_id: str) -> dict[str, Any]:
    settings = get_settings()
    if len(data) > settings.recruitment_mail_max_bytes:
        raise ValueError("CV attachment exceeds size limit")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt"}:
        raise ValueError("Unsupported CV attachment type")
    digest = hashlib.sha256(data).hexdigest()
    # Filenames from email never become filesystem paths.
    folder = DEV_DATA_DIR / "recruitment" / org / run_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (digest + suffix)
    target.write_bytes(data)
    target.chmod(0o600)
    return {
        "candidate_id": digest,
        "filename": Path(filename.replace("\\", "/")).name,
        "sha256": digest,
        "bytes": len(data),
        "text": extract_cv(data, suffix, target),
        "untrusted": True,
        "source": "email_cv",
    }


def download_cv(url: str, max_bytes: int) -> tuple[bytes, str]:
    """Pin a verified public IP, verify TLS hostname, and recheck every redirect."""
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError("CV links must use public HTTPS without credentials")
        if Path(parsed.path).suffix.lower() not in {".pdf", ".docx", ".txt"}:
            raise ValueError("CV URL must identify a PDF, DOCX or TXT file")
        addresses: set[str] = {
            str(r[4][0]) for r in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError("Private, loopback and reserved CV addresses are refused")
        connection = http.client.HTTPSConnection(
            parsed.hostname, timeout=20, context=ssl.create_default_context()
        )
        sock = socket.create_connection((sorted(addresses)[0], 443), timeout=20)
        connection.sock = ssl.create_default_context().wrap_socket(
            sock, server_hostname=parsed.hostname
        )
        try:
            connection.request(
                "GET",
                parsed.path + (("?" + parsed.query) if parsed.query else ""),
                headers={"User-Agent": "O-Nexus-CV-intake/1"},
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin

                url = urljoin(url, response.getheader("Location", ""))
                continue
            if response.status != 200:
                raise ValueError(f"CV download HTTP {response.status}")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("CV link exceeds size limit")
            return data, Path(parsed.path).name
        finally:
            connection.close()
    raise ValueError("CV link redirects exceed limit")


class RecruitmentMailbox:
    def __init__(self, organization_id: str, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.recruitment_mail_org != organization_id:
            raise ValueError("No recruitment mailbox connected to this organization")
        if (
            not self.settings.recruitment_mail_address
            or not self.settings.recruitment_mail_password.get_secret_value()
        ):
            raise ValueError("Recruitment mailbox credentials are not configured")
        self.org = organization_id

    @staticmethod
    def subject(run_id: str) -> str:
        if not re.fullmatch(r"tsk_[a-z0-9]{26}", run_id):
            raise ValueError("Invalid recruitment run identifier")
        return "[ONX-MEP " + run_id + "]"

    def send_tests(self, run_id: str, cvs: list[dict[str, str]]) -> dict[str, Any]:
        sent = []
        address = self.settings.recruitment_mail_address
        with smtplib.SMTP_SSL(
            "smtp.gmail.com",
            465,
            timeout=self.settings.recruitment_mail_timeout_s,
            context=ssl.create_default_context(),
        ) as smtp:
            smtp.login(address, self.settings.recruitment_mail_password.get_secret_value())
            for cv in cvs:
                message = EmailMessage()
                message["From"] = address
                message["To"] = address
                message["Subject"] = self.subject(run_id) + " SYNTHETIC CV " + cv["name"]
                message["Message-ID"] = make_msgid()
                message["X-ONX-Synthetic"] = "true"
                message.set_content(
                    (
                        "Synthetic MEP recruitment test. Fictional candidate, no real applicati"
                        "on. Run: "
                    )
                    + run_id
                )
                message.add_attachment(
                    cv["text"].encode("utf-8"),
                    maintype="text",
                    subtype="plain",
                    filename=cv["filename"],
                )
                refused = smtp.send_message(message, from_addr=address, to_addrs=[address])
                if refused:
                    raise ValueError("SMTP refused self-test recipient")
                sent.append(
                    {
                        "message_id": str(message["Message-ID"]),
                        "filename": cv["filename"],
                        "sha256": hashlib.sha256(cv["text"].encode()).hexdigest(),
                    }
                )
        return {
            "sent": sent,
            "recipient": address,
            "synthetic": True,
            "protocol": "SMTP_SSL",
            "count": len(sent),
        }

    def read_cvs(self, run_id: str) -> dict[str, Any]:
        cvs: dict[str, dict[str, Any]] = {}
        messages = []
        rejected = []
        with imaplib.IMAP4_SSL(
            "imap.gmail.com",
            993,
            ssl_context=ssl.create_default_context(),
            timeout=self.settings.recruitment_mail_timeout_s,
        ) as mail:
            mail.login(
                self.settings.recruitment_mail_address,
                self.settings.recruitment_mail_password.get_secret_value(),
            )
            status, _ = mail.select("INBOX", readonly=True)
            if status != "OK":
                raise ValueError("IMAP INBOX could not be opened read-only")
            validity = str(mail.response("UIDVALIDITY")[1][0].decode())
            status, hits = mail.uid("search", "SUBJECT", '"' + self.subject(run_id) + '"')
            if status != "OK":
                raise ValueError("IMAP search failed")
            uids = hits[0].split()
            if len(uids) > self.settings.recruitment_mail_max_messages:
                raise ValueError("Recruitment batch exceeds configured message limit")
            for uid in uids:
                status, size_rows = mail.uid("fetch", uid, "(RFC822.SIZE)")
                size_blob = b" ".join(row for row in size_rows if isinstance(row, bytes))
                match = re.search(rb"RFC822.SIZE (\d+)", size_blob)
                if status != "OK" or not match:
                    raise ValueError("IMAP message size lookup failed")
                if int(match[1]) > self.settings.recruitment_mail_max_bytes:
                    rejected.append({"uid": uid.decode(), "reason": "message size limit"})
                    continue
                status, rows = mail.uid("fetch", uid, "(BODY.PEEK[])")
                chunks = [
                    row[1] for row in rows if isinstance(row, tuple) and isinstance(row[1], bytes)
                ]
                if status != "OK" or len(chunks) != 1:
                    raise ValueError("IMAP message fetch failed")
                message = BytesParser(policy=policy.default).parsebytes(chunks[0])
                if self.subject(run_id) not in str(message.get("Subject", "")):
                    raise ValueError("Fetched email does not match recruitment run")
                ref = {
                    "uid": uid.decode(),
                    "uidvalidity": validity,
                    "message_id": str(message.get("Message-ID", "")),
                }
                messages.append(ref)
                sources: list[tuple[bytes, str, str]] = []
                for part in message.walk():
                    if part.get_filename():
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            sources.append((payload, str(part.get_filename()), "attachment"))
                    elif part.get_content_type() == "text/plain":
                        for url in re.findall(r"https://[^\s<>]+", str(part.get_content())):
                            try:
                                data, filename = download_cv(
                                    url, self.settings.recruitment_mail_max_bytes
                                )
                                sources.append((data, filename, "https_link"))
                            except Exception as exc:
                                rejected.append(
                                    {**ref, "reason": "CV link refused: " + type(exc).__name__}
                                )
                for data, filename, source in sources:
                    try:
                        cv = store_cv(data, filename, self.org, run_id)
                        cv.update(
                            {
                                "mail": ref,
                                "download_source": source,
                                "synthetic": str(message.get("X-ONX-Synthetic", "")) == "true",
                            }
                        )
                        if cv["candidate_id"] in cvs:
                            cvs[cv["candidate_id"]].setdefault("duplicate_sources", []).append(ref)
                        else:
                            cvs[cv["candidate_id"]] = cv
                    except Exception as exc:
                        rejected.append(
                            {
                                **ref,
                                "filename": Path(filename.replace("\\", "/")).name,
                                "reason": "CV extraction refused: " + type(exc).__name__,
                            }
                        )
        return {
            "cvs": list(cvs.values()),
            "messages": messages,
            "rejected": rejected,
            "readonly": True,
            "protocol": "IMAP_SSL_BODY_PEEK",
            "count": len(cvs),
        }
