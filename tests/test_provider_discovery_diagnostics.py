import io
import json
import socket
import ssl
import urllib.error

import pytest

from backend.core.provider_catalog import discover, ModelListError
from backend.core.application.services import ApplicationServices, OperationError


@pytest.mark.parametrize("status",[401,403,404,405])
def test_list_http_diagnostics_retain_status_and_never_upstream_secrets(status):
    def opening(request,**kwargs):
        assert request.full_url == "https://api.example/v1/models"
        raise urllib.error.HTTPError(request.full_url,status,"synthetic",{},io.BytesIO(b"private-key-must-not-leak"))
    with pytest.raises(ModelListError) as error:
        discover(base_url="https://api.example/v1",api_key="synthetic",open_url=opening)
    assert str(error.value)==f"MODEL_LIST_HTTP_{status}"
    assert error.value.details["status_code"]==status
    assert "private-key" not in str(error.value)+str(error.value.details)


@pytest.mark.parametrize("failure,code",[
    (TimeoutError(),"MODEL_LIST_TIMEOUT"),
    (urllib.error.URLError(socket.gaierror()),"MODEL_LIST_DNS_FAILED"),
    (urllib.error.URLError(ssl.SSLCertVerificationError()),"MODEL_LIST_TLS_FAILED"),
])
def test_list_network_diagnostics_are_specific_and_redacted(failure,code):
    def opening(*args,**kwargs):raise failure
    with pytest.raises(ModelListError,match=code):
        discover(base_url="https://api.example/v1",open_url=opening)


def test_invalid_json_is_a_response_error_not_a_network_error():
    with pytest.raises(ModelListError,match="MODEL_LIST_INVALID"):
        discover(base_url="https://api.example/v1",open_url=lambda *a,**k:io.BytesIO(b"not json"))


def test_model_list_error_uses_configuration_identity_not_a_chat_trace(monkeypatch):
    from backend.core.provider_catalog import ModelListError
    service=ApplicationServices()
    def failed(*args,**kwargs):raise ModelListError("MODEL_LIST_HTTP_403",status_code=403)
    monkeypatch.setattr(service.provider_drafts,"run",failed)
    with pytest.raises(OperationError) as error:
        service.provider_models({"base_url":"https://api.example/v1","api_key":"synthetic","protocol":"openai_chat"},"actual-config-request")
    assert error.value.details=={"phase":"model_discovery","reason":"model_list_http_403","status_code":403,"request_id":"actual-config-request","action":"models"}
