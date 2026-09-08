import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import _DEVELOPMENT_ENVIRONMENTS, _PLACEHOLDER_ENCRYPTION_KEYS, get_settings
from app.logging import get_logger
from app.observability import capture_exception

logger = get_logger(__name__)


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    configured_secret = settings.secret_encryption_key
    secret = configured_secret.strip()
    # Retention shares the catalog database role but has no encryption
    # responsibility. Its role-aware Settings check therefore permits an
    # empty key; keep this second guard so a future retention code path cannot
    # silently turn that exemption into usable placeholder cryptography.
    if (
        settings.environment.strip().casefold() not in _DEVELOPMENT_ENVIRONMENTS
        and (not secret or secret.casefold() in _PLACEHOLDER_ENCRYPTION_KEYS)
    ):
        raise RuntimeError("SECRET_ENCRYPTION_KEY must be set before cryptographic operations")
    # Preserve the exact configured bytes for existing ciphertext. The
    # normalized value above is only for the fail-closed presence check.
    return Fernet(_derive_fernet_key(configured_secret))


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    try:
        return _fernet().decrypt(ciphertext).decode("utf-8")
    except RuntimeError:
        # Configuration failures are fatal. Returning an empty secret would
        # make an intentional fail-closed startup check look like a provider
        # login failure.
        raise
    except InvalidToken as exc:
        # Returning "" means the agent runs on with empty METU credentials and
        # fails much later, somewhere confusing. Report it here, where the
        # cause (usually a rotated SECRET_ENCRYPTION_KEY) is still obvious.
        logger.error("crypto_decrypt_failed_invalid_token")
        capture_exception(exc, **{"$exception_fingerprint": ["crypto_decrypt_failed_invalid_token"]})
        return ""
    except Exception as exc:
        logger.error("crypto_decrypt_failed", error=str(exc))
        capture_exception(exc, **{"$exception_fingerprint": ["crypto_decrypt_failed"]})
        return ""
