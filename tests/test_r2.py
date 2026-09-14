"""Tests for the R2 store: signing, URLs, and the half-configured refusal.

The signing tests check against AWS's *published* SigV4 vectors rather than
against this implementation's own output. A signature routine that agrees with
itself proves nothing; these are the numbers in Amazon's documentation.
"""

import hashlib

import pytest

from nwnbot import config as cfg
from nwnbot import r2


# --------------------------------------------------------------------------
# signing_key — AWS's documented derivation example
# https://docs.aws.amazon.com/  "Examples of how to derive a signing key"
# --------------------------------------------------------------------------

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def test_signing_key_matches_the_aws_published_vector():
    key = r2.signing_key(AWS_SECRET, "20150830", region="us-east-1",
                         service="iam")
    assert key.hex() == (
        "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9")


def test_the_signing_key_changes_with_the_date():
    a = r2.signing_key(AWS_SECRET, "20150830", region="us-east-1", service="iam")
    b = r2.signing_key(AWS_SECRET, "20150831", region="us-east-1", service="iam")
    assert a != b


def test_the_signing_key_changes_with_the_region():
    a = r2.signing_key(AWS_SECRET, "20150830", region="us-east-1", service="iam")
    b = r2.signing_key(AWS_SECRET, "20150830", region="auto", service="iam")
    assert a != b


# --------------------------------------------------------------------------
# authorization_header
# --------------------------------------------------------------------------

def _auth(**kw):
    args = dict(method="PUT", host="acct.r2.cloudflarestorage.com",
                path="/homerslotr/discord/ab/abcd.webp",
                access_key="AKIA_TEST", secret_key=AWS_SECRET,
                payload_sha256=hashlib.sha256(b"bytes").hexdigest(),
                amz_date="20260913T120000Z", content_type="image/webp")
    args.update(kw)
    return r2.authorization_header(**args)


def test_the_authorization_header_has_the_expected_shape():
    auth, _ = _auth()
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIA_TEST/20260913/auto/s3/aws4_request")
    assert "SignedHeaders=" in auth and "Signature=" in auth


def test_the_signed_headers_are_exactly_what_is_returned():
    # The signature commits to this set; sending a different one is rejected.
    auth, headers = _auth()
    signed = auth.split("SignedHeaders=")[1].split(",")[0]
    assert signed.split(";") == sorted(headers)


def test_content_type_is_signed_when_present_and_absent_when_not():
    _, with_ct = _auth(content_type="image/webp")
    _, without = _auth(content_type="")
    assert "content-type" in with_ct
    assert "content-type" not in without


def test_the_signature_is_deterministic_for_the_same_inputs():
    assert _auth()[0] == _auth()[0]


def test_the_signature_changes_with_the_payload():
    a, _ = _auth(payload_sha256=hashlib.sha256(b"one").hexdigest())
    b, _ = _auth(payload_sha256=hashlib.sha256(b"two").hexdigest())
    assert a != b


def test_the_signature_changes_with_the_key():
    a, _ = _auth(path="/homerslotr/discord/ab/one.webp")
    b, _ = _auth(path="/homerslotr/discord/ab/two.webp")
    assert a != b


def test_a_path_is_percent_encoded_but_slashes_are_kept():
    # Getting this wrong yields a signature mismatch that reads like a bad
    # credential, which is an expensive hour.
    auth, _ = _auth(path="/homerslotr/discord/a b/c+d.webp")
    assert auth  # no exception
    assert r2._quote("/a b/c+d.webp", safe="/") == "/a%20b/c%2Bd.webp"


# --------------------------------------------------------------------------
# R2Store: URLs and construction
# --------------------------------------------------------------------------

def _store(**kw):
    args = dict(account_id="acct", bucket="homerslotr", access_key="AK",
                secret_key="SK", public_base_url="img.homerslotr.com")
    args.update(kw)
    return r2.R2Store(**args)


def test_the_public_url_uses_the_custom_domain_not_the_signing_host():
    # This URL is written into roadmap items. It must never be the endpoint,
    # which is credentialed and not public.
    url = _store().url_for("discord/ab/abcd.webp")
    assert url == "https://img.homerslotr.com/discord/ab/abcd.webp"
    assert "r2.cloudflarestorage.com" not in url


def test_a_scheme_is_added_to_the_public_url_when_missing():
    assert _store(public_base_url="img.homerslotr.com").public_base_url \
        == "https://img.homerslotr.com"


def test_an_explicit_scheme_is_kept():
    assert _store(public_base_url="https://cdn.example/x/").public_base_url \
        == "https://cdn.example/x"


def test_the_signing_host_is_derived_from_the_account():
    assert _store().host == "acct.r2.cloudflarestorage.com"


def test_a_missing_field_is_refused_at_construction():
    with pytest.raises(ValueError) as e:
        _store(secret_key="")
    assert "secret_key" in str(e.value)


# --------------------------------------------------------------------------
# from_env
# --------------------------------------------------------------------------

FULL = {
    "R2_ACCOUNT_ID": "acct", "R2_BUCKET": "homerslotr",
    "R2_ACCESS_KEY_ID": "AK", "R2_SECRET_ACCESS_KEY": "SK",
    "R2_PUBLIC_BASE_URL": "img.homerslotr.com",
}


def test_nothing_configured_means_no_store_rather_than_an_error():
    # A supported state: the bot runs and reports images as not kept.
    assert r2.from_env({}) is None


def test_a_full_environment_builds_a_store():
    store = r2.from_env(dict(FULL))
    assert store.bucket == "homerslotr"
    assert store.url_for("k") == "https://img.homerslotr.com/k"


@pytest.mark.parametrize("missing", sorted(FULL))
def test_a_half_configured_environment_is_refused(missing):
    # Silently not storing images because one variable was mistyped is exactly
    # the failure rehosting exists to prevent, so this must be loud.
    env = {k: v for k, v in FULL.items() if k != missing}
    with pytest.raises(cfg.ConfigError) as e:
        r2.from_env(env)
    assert missing in str(e.value)


def test_whitespace_only_values_count_as_missing():
    env = dict(FULL, R2_SECRET_ACCESS_KEY="   ")
    with pytest.raises(cfg.ConfigError):
        r2.from_env(env)


def test_neither_credential_appears_in_the_repr():
    # reprs reach logs and tracebacks, which is the last place a live key
    # should turn up. The default dataclass repr printed both of these.
    text = repr(_store(secret_key="SUPERSECRETVALUE", access_key="ACCESSKEYID"))
    assert "SUPERSECRETVALUE" not in text
    assert "ACCESSKEYID" not in text
    assert "<redacted>" in text
    assert "homerslotr" in text          # still useful for debugging


def test_the_secret_does_not_leak_through_str_either():
    assert "SUPERSECRETVALUE" not in str(_store(secret_key="SUPERSECRETVALUE"))


def test_the_secret_does_not_leak_through_an_f_string():
    # The shape that actually appears in log lines.
    store = _store(secret_key="SUPERSECRETVALUE")
    assert "SUPERSECRETVALUE" not in f"store is {store}"
