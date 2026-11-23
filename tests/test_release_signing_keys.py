import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption

from backend.core.storage import StorageRuntime
from backend.remote.publish_platform import PlatformTarget
from backend.core.publish.release_signing_keys import provision,descriptor,resolve
from backend.core.publish.state import PublishError


def test_signing_key_has_exact_project_repository_and_account_scope_and_detects_replacement(tmp_path_factory):
    target=PlatformTarget("github","https://github.com","fixture/repository","operator")
    first=tmp_path_factory/"first";first.mkdir()
    second=tmp_path_factory/"second";second.mkdir()
    def seed():return base64.b64encode(Ed25519PrivateKey.generate().private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())).decode()
    with StorageRuntime(first,state_home=tmp_path_factory/"state") as one,StorageRuntime(second,state_home=tmp_path_factory/"state") as two:
        value=provision(one,target,"publisher",seed())
        assert descriptor(one,target,"publisher")==value
        assert "PRIVATE KEY" not in str(value) and len(resolve(one,target,value).sign(b"fixture"))==64
        with pytest.raises(PublishError,match="SCOPE_MISMATCH"):resolve(two,target,value)
        with pytest.raises(PublishError,match="SCOPE_MISMATCH"):resolve(one,replace(target,account="another"),value)
        with pytest.raises(PublishError,match="SCOPE_MISMATCH"):resolve(one,replace(target,repository="another/repository"),value)
        provision(one,target,"publisher",seed())
        with pytest.raises(PublishError,match="KEY_CHANGED"):resolve(one,target,value)


def test_unknown_or_invalid_signing_input_does_not_fall_back_to_environment_or_official_key(tmp_path_factory,monkeypatch):
    target=PlatformTarget("github","https://github.com","fixture/repository","operator")
    workspace=tmp_path_factory/"workspace";workspace.mkdir()
    monkeypatch.setenv("GITGO_RELEASE_SIGNING_KEY","must-not-be-used")
    with StorageRuntime(workspace,state_home=tmp_path_factory/"state") as storage:
        with pytest.raises(PublishError,match="UNCONFIGURED"):descriptor(storage,target,"official-key-name")
        with pytest.raises(PublishError,match="INVALID"):provision(storage,target,"publisher","wrong-key")
