import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.logging import get_logger
from app.observability import capture_exception

logger = get_logger(__name__)


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    return Fernet(_derive_fernet_key(settings.secret_encryption_key))


def encrypt_secret(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes) -> str:
    try:
        return _fernet().decrypt(ciphertext).decode("utf-8")
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
