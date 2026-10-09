"""A normalised view of one email, however it was obtained.

Watchers only ever see this class, so the same watcher definition works against
a live Gmail message and against a `.eml` file on disk. That is what makes the
test suite possible without touching the network.
"""

from __future__ import annotations

import email
import email.policy
import html as html_lib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

# Tags whose contents are never visible text.
_INVISIBLE = re.compile(r"(?is)<(script|style|head|title)\b.*?</\1>")
# Tags that imply a line break once stripped.
_BLOCK_END = re.compile(r"(?is)</(td|tr|div|p|table|h[1-6]|li)>")
_BR = re.compile(r"(?is)<br\s*/?>")
_TAG = re.compile(r"(?s)<[^>]+>")


def html_to_text(source: str) -> str:
    """Flatten marketing HTML into readable lines.

    Deliberately simple: watcher regexes are written against this output, so it
    needs to be stable and predictable rather than clever. Every block-level
    close becomes a newline, runs of whitespace collapse, and entities resolve.
    """
    text = _INVISIBLE.sub(" ", source)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)
    text = text.replace("\u00a0", " ")
    # Remove invisible formatting characters: direction marks, zero-width
    # joiners, soft hyphens. They're invisible on screen but break patterns —
    # Amazon puts a right-to-left mark in front of every order number, so
    # "Order # 202-…" never matched. U+034F pads marketing preheaders too.
    text = "".join(
        ch for ch in text if ch != "\u034f" and unicodedata.category(ch) != "Cf"
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


@dataclass
class Email:
    message_id: str
    thread_id: str = ""
    subject: str = ""
    sender: str = ""
    recipient: str = ""
    date: datetime | None = None
    html: str = ""
    plain: str = ""
    attachments: list[dict] = field(default_factory=list)
    # Pictures inside the email itself, by Content-ID: the HTML shows them
    # as <img src="cid:…">. {cid: (content type, bytes)}
    inline_images: dict[str, tuple[str, bytes]] = field(default_factory=dict, repr=False)

    _text_cache: str | None = field(default=None, repr=False, compare=False)
    _attachment_text_cache: str | None = field(
        default=None, repr=False, compare=False
    )

    def pdf_attachments(self, filename_matches: str | None = None) -> list[dict]:
        """PDF attachments, optionally filtered by a filename pattern."""
        import re as _re

        found = [
            a
            for a in self.attachments
            if a.get("content_type") == "application/pdf"
            or str(a.get("filename", "")).lower().endswith(".pdf")
        ]
        if filename_matches:
            found = [
                a
                for a in found
                if _re.search(filename_matches, str(a.get("filename", "")), _re.I)
            ]
        return found

    @property
    def attachment_text(self) -> str:
        """Text extracted from PDF attachments.

        Some suppliers attach their real invoice and leave the useful
        identifiers — FreeAgent's invoice number, for one — out of the email
        body entirely. Without this a watcher can only see the covering note.
        Returns an empty string if nothing can be read, so a pattern simply
        fails to match rather than the whole scan blowing up.
        """
        if self._attachment_text_cache is not None:
            return self._attachment_text_cache

        chunks: list[str] = []
        try:
            import io

            from pypdf import PdfReader

            for attachment in self.pdf_attachments():
                data = attachment.get("data")
                if not data:
                    continue
                try:
                    reader = PdfReader(io.BytesIO(data))
                    chunks.extend(page.extract_text() or "" for page in reader.pages)
                except Exception:
                    continue
        except ImportError:
            pass

        self._attachment_text_cache = "\n".join(chunks)
        return self._attachment_text_cache

    def html_with_images(self, limit: int = 8 * 1024 * 1024) -> str:
        """The HTML with its own pictures in place: each `cid:` reference
        becomes a data: URL, up to `limit` bytes in all, so it shows without
        the attachments (in the app, and when the email is printed)."""
        if not self.inline_images or "cid:" not in self.html:
            return self.html
        import base64

        budget = [limit]

        def swap(match: re.Match) -> str:
            found = self.inline_images.get(html_lib.unescape(match.group(1)))
            if not found or len(found[1]) > budget[0]:
                return match.group(0)
            budget[0] -= len(found[1])
            return f"data:{found[0]};base64,{base64.b64encode(found[1]).decode()}"

        return re.sub(r"cid:([^\"'\s)>]+)", swap, self.html, flags=re.I)

    @property
    def text(self) -> str:
        """Best available plain-text rendering of the body."""
        if self._text_cache is None:
            if self.html.strip():
                self._text_cache = html_to_text(self.html)
            else:
                self._text_cache = self.plain.strip()
        return self._text_cache

    @property
    def searchable(self) -> str:
        """Subject plus body, for coarse `*_contains` matching."""
        return f"{self.subject}\n{self.text}"

    @property
    def date_iso(self) -> str:
        return self.date.date().isoformat() if self.date else ""

    def links(self, pattern: str | None = None) -> list[str]:
        """Every href in the HTML body, optionally filtered by a regex."""
        found = [
            html_lib.unescape(href)
            for href in re.findall(r'href="([^"]+)"', self.html)
        ]
        found += [
            html_lib.unescape(href)
            for href in re.findall(r"href='([^']+)'", self.html)
        ]
        if pattern:
            compiled = re.compile(pattern)
            found = [href for href in found if compiled.search(href)]
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        unique = []
        for href in found:
            if href not in seen:
                seen.add(href)
                unique.append(href)
        return unique

    # ---- constructors ---------------------------------------------------

    @classmethod
    def from_eml(cls, path: Path | str) -> "Email":
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        parsed = email.message_from_string(raw, policy=email.policy.default)
        return cls.from_email_message(parsed)

    @classmethod
    def from_bytes(cls, raw: bytes, message_id: str = "", thread_id: str = "") -> "Email":
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        built = cls.from_email_message(parsed)
        if message_id:
            built.message_id = message_id
        if thread_id:
            built.thread_id = thread_id
        return built

    @classmethod
    def from_email_message(cls, parsed: EmailMessage) -> "Email":
        html_body, plain_body = "", ""
        attachments: list[dict] = []
        inline_images: dict[str, tuple[str, bytes]] = {}

        for part in parsed.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            payload = part.get_payload(decode=True) or b""
            cid = str(part.get("Content-ID") or "").strip().strip("<>")
            if cid and content_type.startswith("image/") and payload:
                inline_images[cid] = (content_type, payload)
                if "attachment" not in disposition:
                    continue
            if "attachment" in disposition:
                attachments.append(
                    {
                        "filename": part.get_filename() or "",
                        "content_type": content_type,
                        "size": len(payload),
                        "data": payload,
                    }
                )
                continue
            decoded = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
            if content_type == "text/html" and not html_body:
                html_body = decoded
            elif content_type == "text/plain" and not plain_body:
                plain_body = decoded

        when: datetime | None = None
        if parsed.get("Date"):
            try:
                when = parsedate_to_datetime(parsed["Date"])
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                when = None

        return cls(
            message_id=str(parsed.get("Message-ID") or "").strip("<>"),
            subject=str(parsed.get("Subject") or ""),
            sender=str(parsed.get("From") or ""),
            recipient=str(parsed.get("To") or ""),
            date=when,
            html=html_body,
            plain=plain_body,
            attachments=attachments,
            inline_images=inline_images,
        )
