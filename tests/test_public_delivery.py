import hashlib
import httpx
import pytest

from backend.core.publish.public_delivery import download_fact
from backend.core.publish.state import PublishError
from backend.core.publish.release_engine import ReleaseCancelled


def test_anonymous_redirects_strip_account_credentials_and_verify_exact_stream():
    body = b"public release" * 20000
    observed = []
    def handler(request):
        observed.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location":"https://release-assets.githubusercontent.com/fixture"})
        return httpx.Response(200,content=body)
    expected = {"size":len(body), "sha256":hashlib.sha256(body).hexdigest()}
    with httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization":"Bearer must-stay-private"},
                      cookies={"session":"private"}) as client:
        assert download_fact("https://github.com/team/repo/releases/download/v1/file", expected, client=client) == expected
    assert len(observed) == 2


@pytest.mark.parametrize("failure", ["digest", "oversize", "redirect", "cancel"])
def test_public_delivery_never_reports_success_without_matching_bytes(failure):
    def handler(request):
        if failure == "redirect": return httpx.Response(302,headers={"location":"https://unknown.test/file"})
        return httpx.Response(200,content=b"bad")
    expected={"size":2 if failure == "oversize" else 3,"sha256":"0"*64}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((PublishError, ReleaseCancelled)):
            download_fact("https://github.com/a/b/releases/download/v1/a", expected, client=client,
                          cancelled=lambda:failure == "cancel")
