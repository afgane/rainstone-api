import pytest
from pydantic import ValidationError
from rainstone.config import Settings


def test_development_identity_cannot_start_outside_demo_mode() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="development", demo_data=False)
