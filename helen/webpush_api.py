import os
import json
import logging
import base64
from typing import Optional
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import webpush, WebPushException
from helen import db

log = logging.getLogger("helen.webpush")

VAPID_PRIVATE_PEM_PATH = "/app/data/vapid_private.pem" if os.path.exists("/app/data") else "data/vapid_private.pem"

def ensure_vapid_keys() -> str:
    """Ensure VAPID keys exist in the database and are written to a PEM file. Returns public key."""
    pub_key_str = db.get_config("vapid_public_key")
    priv_pem = db.get_config("vapid_private_key")

    if not pub_key_str or not priv_pem:
        log.info("Generating new VAPID keys...")
        private_key = ec.generate_private_key(ec.SECP256R1())
        
        # Serialize private key to PEM
        priv_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')

        # Serialize public key to uncompressed base64url
        public_key = private_key.public_key()
        pub_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint
        )
        pub_key_str = base64.urlsafe_b64encode(pub_bytes).decode('utf-8').rstrip('=')

        db.set_config("vapid_private_key", priv_pem)
        db.set_config("vapid_public_key", pub_key_str)
        log.info("New VAPID keys stored in DB.")

    # Write private key to a temporary/persistent PEM file for pywebpush
    os.makedirs(os.path.dirname(VAPID_PRIVATE_PEM_PATH), exist_ok=True)
    with open(VAPID_PRIVATE_PEM_PATH, "w", encoding="utf-8") as f:
        f.write(priv_pem)

    return pub_key_str


def send_push_notification(subscription: dict, payload: dict) -> bool:
    """Send a single push notification. Deletes subscription if expired."""
    try:
        ensure_vapid_keys()
        
        # Construct sub_info for pywebpush
        sub_info = {
            "endpoint": subscription["endpoint"],
            "keys": {
                "p256dh": subscription["p256dh"],
                "auth": subscription["auth"]
            }
        }

        # Send push
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_PEM_PATH,
            vapid_claims={
                "sub": "mailto:admin@helen.local"
            }
        )
        return True
    except WebPushException as ex:
        log.warning("WebPushException sending notification: %s", ex)
        # If subscription is gone or invalid (404, 410), delete it
        if ex.response is not None and ex.response.status_code in (404, 410):
            log.info("Removing expired subscription: %s", subscription["endpoint"])
            db.delete_push_subscription(subscription["endpoint"])
        return False
    except Exception:
        log.exception("Unexpected error sending push notification")
        return False


def broadcast_push_notification(payload: dict) -> None:
    """Send a push notification to all active subscriptions."""
    subs = db.list_push_subscriptions()
    if not subs:
        return
    log.info("Broadcasting push notification to %d subscribers...", len(subs))
    for sub in subs:
        send_push_notification(sub, payload)
