# Tests for djangocms-automation

This directory contains pytest tests for the djangocms-automation package.

## Test Files

### `test_services.py`
Tests for the service registry system that manages external service integrations.

**Test Classes:**
- `TestServiceRegistry`: Tests the ServiceRegistry class functionality
  - Registration and unregistration of services
  - Retrieving service information
  - Getting all services
  - Getting choices for Django forms

- `TestGlobalServiceRegistry`: Tests the pre-configured global service registry
  - Verifies default services (OpenAI, GitHub, Slack, etc.)
  - Tests service retrieval

**Coverage:**
- Service registration with/without descriptions
- Unregistering services (including non-existent ones)
- Getting service by ID
- Listing all services
- Django field choices generation
- Default pre-registered services

### `test_apikey.py`
Tests for the APIKey model that stores API keys for external services.

**Test Class:**
- `TestAPIKeyModel`: Comprehensive tests for the APIKey model
  - Creating API keys
  - Timestamps (created/updated)
  - Default values
  - String representations
  - Service display methods
  - Multiple keys per service
  - Ordering (by service, then name)
  - Filtering (by service, active status)
  - Updating and deleting
  - Field max lengths

**Coverage:**
- Model creation and validation
- Automatic timestamp handling
- Service integration with registry
- Multiple API keys per service support
- Querying and filtering
- Model methods (get_service_display, get_service_choices)
- CRUD operations

### `test_apikey_admin.py`
Tests for the Django admin interface for APIKey model.

**Test Classes:**
- `TestAPIKeyAdminForm`: Tests the custom admin form
  - Dynamic service choices from registry
  - Form validation
  - Saving through form

- `TestAPIKeyAdmin`: Tests the admin class configuration and methods
  - List display configuration
  - Filters and search
  - Readonly fields
  - Fieldsets structure
  - Service display method
  - Masked key display for security
  - Queryset handling

**Coverage:**
- Admin form with dynamic choices
- Admin configuration (list_display, list_filter, search_fields, etc.)
- Security features (masked key display)
- Custom display methods
- Fieldset organization
- Form integration

## Running Tests

### All Tests
```bash
python -m pytest tests/
```

### Specific Test File
```bash
python -m pytest tests/test_services.py -v
python -m pytest tests/test_apikey.py -v
python -m pytest tests/test_apikey_admin.py -v
```

### Specific Test Class
```bash
python -m pytest tests/test_services.py::TestServiceRegistry -v
python -m pytest tests/test_apikey.py::TestAPIKeyModel -v
```

### Specific Test Method
```bash
python -m pytest tests/test_services.py::TestServiceRegistry::test_register_service -v
```

### With Coverage
```bash
python -m pytest tests/ --cov=djangocms_automation --cov-report=html
```

## Test Coverage

The tests cover:

✅ **Service Registry**
- Core functionality (register, unregister, get, all)
- Django integration (get_choices)
- Global registry with pre-configured services

✅ **APIKey Model**
- All model fields and validation
- Relationship with service registry
- Multiple keys per service
- Querying and filtering
- Model methods and properties

✅ **Admin Interface**
- Form customization with dynamic choices
- Display configuration
- Security features (masked keys)
- Custom display methods
- Fieldset organization

## Database Requirements

Tests marked with `@pytest.mark.django_db` require database access. The pytest-django plugin automatically creates a test database for these tests.

## Fixtures

- `admin_site`: Creates an AdminSite instance for testing
- `api_key_admin`: Creates an APIKeyAdmin instance
- `superuser`: Creates a superuser for admin tests
- `assert_html_in_response`: Helper for HTML response testing (from conftest.py)

## Notes

- All tests use pytest fixtures and markers
- Django database tests are properly marked with `@pytest.mark.django_db`
- Tests are isolated and can run in any order
- Mock/test data is created within each test and cleaned up automatically
