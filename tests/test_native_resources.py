import pytest
from backend.core.native_resources import windows_job_name, recovery_location
from backend.core.loop.publish_recovery import resource_location
from backend.core.installation_lease import LEASE_HANDLES_ENV
from backend.core.update.sandbox_admission import LEASE_HANDLES_ENV as ADMISSION_LEASE_ENV


def test_generic_native_resource_retains_existing_recovery_wire_identity():
    identifier = "a" * 32
    assert windows_job_name(identifier) == "Local\\Gitgo.Publish." + identifier
    assert resource_location(identifier) == recovery_location(identifier)
    assert windows_job_name("") is None
    assert LEASE_HANDLES_ENV == ADMISSION_LEASE_ENV


@pytest.mark.parametrize("value", ["../other", "a" * 33, "G" * 32, 1])
def test_invalid_recovery_identity_cannot_select_another_native_boundary(value):
    with pytest.raises(ValueError):
        windows_job_name(value)
