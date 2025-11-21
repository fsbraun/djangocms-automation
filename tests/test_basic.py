import pytest


@pytest.mark.django_db
def test_basic_setup():
    """Test that the basic Django setup works"""
    from django.contrib.auth.models import User

    # Create a test user
    user = User.objects.create_user(username="testuser", password="testpass")
    assert user.username == "testuser"
    assert User.objects.count() == 1


def test_package_version():
    """Test that the package version is accessible"""
    import djangocms_automation

    assert hasattr(djangocms_automation, "__version__")
    assert djangocms_automation.__version__ == "0.1.0"
