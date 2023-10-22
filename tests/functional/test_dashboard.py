"""Functional tests for the backend of the Flask application."""

import pytest

from app.blueprints.auth.models import User


@pytest.mark.usefixtures("as_user", "init_database")
def test_dashboard_view(client: pytest.fixture, user: User) -> None:
    """Test the dashboard view.

    Args:
    ----
        client: Test client for the Flask application.
        user: User model.
    """
    assert user.is_authenticated is True

    response = client.get("/admin")
    assert response.status_code == 308
    assert response.request.path == "/admin"


@pytest.mark.usefixtures("as_user")
def test_sensors_view(client: pytest.fixture, user: User) -> None:
    """Test the sensors view.

    Args:
    ----
        client: Test client for the Flask application.
        user: User model.
    """
    assert user.is_authenticated is True

    response = client.get("/admin/sensors")
    assert response.status_code == 302
    assert response.request.path == "/admin/sensors"
    # assert b"Sensor" in response.data
