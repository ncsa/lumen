from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lumen.timeutils import utcnow

from ..extensions import db


class AuthRequest(db.Model):
    """A pending key request from the OAuth device or authorization-code flow.

    One row tracks a request from creation through approval to claim. The
    client-facing secrets are never stored in the clear: the device code and
    the authorization code live as SHA-256 hashes (like API keys), so only the
    holder of the plaintext can poll or redeem. The API key itself does not
    exist until the claim transaction mints it; a request that is approved but
    never claimed expires with no key behind it.

    ``client_id`` and ``author`` are free-form labels supplied by the
    requester and are not verified — there is no client registry yet, which is
    why the consent page shows an "unverified application" banner.
    """

    __tablename__ = "auth_requests"
    __table_args__ = (
        CheckConstraint("flow IN ('device', 'code')", name="ck_auth_requests_flow"),
        CheckConstraint("status IN ('pending', 'approved', 'denied', 'claimed')", name="ck_auth_requests_status"),
        db.Index("ix_auth_requests_expires_at", "expires_at"),
        db.Index("uq_auth_requests_device_code_hash", "device_code_hash", unique=True),
        db.Index("uq_auth_requests_auth_code_hash", "auth_code_hash", unique=True),
        # A user code identifies exactly one live request; historical rows
        # (handled or expired) keep their codes until the janitor removes them.
        db.Index(
            "uq_auth_requests_pending_user_code",
            "user_code",
            unique=True,
            postgresql_where=db.text("status = 'pending'"),
            sqlite_where=db.text("status = 'pending'"),
        ),
        {"comment": "OAuth key requests (device flow and PKCE authorization-code flow) from creation to claim"},
    )

    id: Mapped[int] = mapped_column(db.Integer, primary_key=True, comment="Primary key")
    flow: Mapped[str] = mapped_column(db.String(16), comment="Request flow: device or code")
    client_id: Mapped[str] = mapped_column(db.String(128), comment="Free-form application label supplied by the requester (unverified)")
    requested_name: Mapped[str] = mapped_column(db.String(128), comment="Name the requester wants for the API key")
    author: Mapped[Optional[str]] = mapped_column(db.String(128), comment="Free-form requester label supplied by the request (unverified)")
    status: Mapped[str] = mapped_column(db.String(16), default="pending", comment="pending, approved, denied, or claimed")
    created_at: Mapped[datetime] = mapped_column(db.DateTime, default=utcnow, comment="UTC creation timestamp")
    expires_at: Mapped[datetime] = mapped_column(db.DateTime, comment="UTC functional expiry; approval may extend it by the claim window")
    # Device flow: SHA-256 of the plaintext device code handed to the CLI in the
    # POST response only; the CLI presents it in POST bodies, never URLs.
    device_code_hash: Mapped[Optional[str]] = mapped_column(db.String(64), comment="SHA-256 hash of the device-flow polling secret")
    user_code: Mapped[Optional[str]] = mapped_column(db.String(9), comment="Human-echoed XXXX-XXXX confirmation code shown on the consent page (cannot fetch a key)")
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC timestamp of the most recent token poll; drives slow_down")
    # Code flow (PKCE): destination and verifier challenge captured at
    # authorization; the auth code is minted at approval and stored hashed.
    redirect_uri: Mapped[Optional[str]] = mapped_column(db.Text, comment="Code-flow destination the user approved; must match exactly at redemption")
    code_challenge: Mapped[Optional[str]] = mapped_column(db.String(128), comment="Code-flow S256 challenge binding redemption to the client that holds the verifier")
    auth_code_hash: Mapped[Optional[str]] = mapped_column(db.String(64), comment="SHA-256 hash of the short-lived authorization code, set at approval")
    auth_code_expires_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC expiry of the authorization code (60s after approval)")
    entity_id: Mapped[Optional[int]] = mapped_column(db.Integer, db.ForeignKey("entities.id", ondelete="CASCADE"), comment="Approving entity; set at approval")
    overwrite: Mapped[bool] = mapped_column(db.Boolean, default=False, comment="Approver agreed to replace an existing same-name key (executed at mint)")
    ttl: Mapped[Optional[int]] = mapped_column(db.BigInteger, comment="Key TTL in seconds (0 = never): as requested (null if none was), replaced by the approver's choice at approval")
    approved_at: Mapped[Optional[datetime]] = mapped_column(db.DateTime, comment="UTC approval timestamp")
    # Kept after claim so a replayed authorization code can revoke the minted
    # key (RFC 6749 §4.1.2). SET NULL: deleting the key by other means must not
    # remove the replay-detection record.
    api_key_id: Mapped[Optional[int]] = mapped_column(db.Integer, db.ForeignKey("api_keys.id", ondelete="SET NULL"), comment="Key minted at claim; retained to revoke on authorization-code replay")
