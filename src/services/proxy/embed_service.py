"""Resolve a GTWY "embed" JWT (top-level `folder_id` claim, signed with a
per-organization secret) into the same profile shape `jwt_middleware`
already builds for a normal token.

Mirrors gtwy-node's `resolveGtwyEmbedToken`/`createOrGetUser`
(src/middlewares/gtwyEmbedMiddleware.js, src/utils/proxy.utils.js) so an
embed token behaves identically whether it hits gtwy-node or gtwy-ai
directly (e.g. via the SDK's `Authorization` credential).
"""

import json
import re
import secrets

import jwt
from fastapi import HTTPException

from config import Config
from src.services.cache_service import find_in_cache, store_in_cache
from src.services.utils.apiservice import fetch

_SPECIAL_CHAR_MAP = {
    "!": "A", "@": "B", "#": "C", "$": "D", "%": "E", "^": "F", "&": "G",
    "*": "H", "(": "I", ")": "J", "-": "K", "_": "L", "=": "M", "+": "N",
    "[": "O", "]": "P", "{": "Q", "}": "R", ";": "S", ":": "T", "'": "U",
    '"': "V", ",": "W", ".": "X", "/": "Y", "?": "Z", "<": "AA", ">": "AAA",
    "|": "LLL",
}
_SPECIAL_CHAR_RE = re.compile("[" + "".join(re.escape(k) for k in _SPECIAL_CHAR_MAP) + "]")


def encrypt_string(value) -> str:
    """Not real cryptography — sanitizes a value into an email-safe token by
    uppercasing it and substituting special characters with letter codes.
    Mirrors gtwy-node's `encryptString` (src/services/utils/utility.service.js),
    used to keep a customer's raw external user id out of the synthetic
    embed-user email GTWY stores.
    """
    text = str(value) if value is not None else ""
    return _SPECIAL_CHAR_RE.sub(lambda m: _SPECIAL_CHAR_MAP.get(m.group(0), m.group(0)), text.upper())


def _generate_identifier(length: int = 14, prefix: str = "", include_number: bool = True) -> str:
    """Mirrors gtwy-node's `generateIdentifier` (src/services/utils/utility.service.js)."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    if not include_number:
        alphabet = alphabet[:-10]
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length))


async def get_organization_by_id(org_id: str) -> dict:
    """Fetch an org's MSG91 company record (including `meta.gtwyAccessToken`),
    caching the result. Mirrors gtwy-node's `getOrganizationById`
    (src/services/proxy.service.js).
    """
    cache_key = f"embed:org_{org_id}"
    cached = await find_in_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass

    response, _headers = await fetch(
        f"https://routes.msg91.com/api/{Config.PUBLIC_REFERENCEID}/getCompanies?id={org_id}",
        "GET",
        {"Authkey": Config.ADMIN_API_KEY},
    )
    data = ((response or {}).get("data") or {}).get("data") or [{}]
    org = data[0] if data else {}
    if org:
        await store_in_cache(cache_key, org)
    return org


async def create_or_find_user_and_company(payload: dict) -> dict:
    """POST to MSG91's createCUsers, resolving/creating the embed end-user's
    GTWY-side identity. Mirrors gtwy-node's `createOrFindUserAndCompany`
    (src/services/proxy.service.js).
    """
    response, _headers = await fetch(
        "https://routes.msg91.com/api/createCUsers",
        "POST",
        {"Content-Type": "application/json", "Authkey": Config.ADMIN_API_KEY},
        json_body=payload,
    )
    return response


async def create_or_get_user(check_token: dict, decoded_token: dict, org_token_from_db: dict) -> dict:
    """Mirrors gtwy-node's `createOrGetUser` (src/utils/proxy.utils.js)."""
    embed_user_id = decoded_token.get("user_id") or decoded_token.get("unique_identifier")
    cache_key = f"embed:user_{embed_user_id}:{decoded_token.get('org_id')}:{decoded_token.get('folder_id')}"
    cached = await find_in_cache(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass

    name = decoded_token.get("name") or _generate_identifier(14, "emb", include_number=False)
    email = f"{decoded_token.get('org_id')}_{decoded_token.get('folder_id')}_{check_token.get('user_id')}@gtwy.ai"
    user_details = {
        "name": name,
        "email": email,
        "meta": {"type": "embed", **(decoded_token.get("meta") or {})},
    }
    org_details = {
        "name": (org_token_from_db or {}).get("name"),
        "is_readable": True,
        # status "2" => this org shouldn't show up for a normal (non-guest) viasocket visit.
        "meta": {"status": "2"},
    }
    proxy_object = {
        "feature_id": Config.PUBLIC_REFERENCEID,
        "Cuser": user_details,
        "company": org_details,
        "role_id": Config.PROXY_USER_ROLE_ID,
    }
    proxy_response = await create_or_find_user_and_company(proxy_object)

    result = {"proxyResponse": proxy_response, "name": name, "email": email}
    await store_in_cache(cache_key, result)
    return result


async def resolve_gtwy_embed_token(token: str, decoded_token: dict) -> dict:
    """Verify + resolve an embed JWT (top-level `org_id`/`folder_id`/`user_id`,
    signed with the org's own `gtwyAccessToken`) into the flat profile shape
    `jwt_middleware` expects — the same shape `make_data_if_proxy_token_given`
    returns for a proxy-token request. Mirrors gtwy-node's
    `resolveGtwyEmbedToken` (src/middlewares/gtwyEmbedMiddleware.js).

    `decoded_token` is the caller's own unverified decode of `token` (already
    done once in `jwt_middleware` to check `folder_id`) — passed in rather
    than re-derived here, since `token` itself is only needed for the actual
    signature-verified decode below.
    """
    has_no_user_identifier = not decoded_token.get("user_id") and not decoded_token.get("unique_identifier")
    if has_no_user_identifier or not decoded_token.get("folder_id") or not decoded_token.get("org_id"):
        raise HTTPException(
            status_code=401,
            detail="unauthorized user, user_id (or unique_identifier), folder id or org id not provided",
        )

    org_token_from_db = await get_organization_by_id(decoded_token.get("org_id"))
    org_token = (org_token_from_db or {}).get("meta", {}).get("gtwyAccessToken")
    if not org_token:
        raise HTTPException(status_code=401, detail="invalid token")

    try:
        check_token = jwt.decode(token, org_token, algorithms=["HS256"])
    except jwt.PyJWTError as err:
        raise HTTPException(status_code=404, detail="token verification failed") from err

    embed_user_id = check_token.get("user_id") or check_token.get("unique_identifier")
    if embed_user_id:
        check_token["user_id"] = encrypt_string(embed_user_id)

    resolved = await create_or_get_user(check_token, decoded_token, org_token_from_db)
    proxy_response = resolved["proxyResponse"]
    proxy_user = proxy_response["data"]["user"]
    proxy_company = proxy_response["data"]["company"]

    return {
        "user": {"id": proxy_user["id"], "name": resolved["name"], "meta": proxy_user.get("meta")},
        "org": {"id": proxy_company["id"], "name": org_token_from_db.get("name")},
        "extraDetails": {"type": "embed", "folder_id": decoded_token.get("folder_id")},
    }
