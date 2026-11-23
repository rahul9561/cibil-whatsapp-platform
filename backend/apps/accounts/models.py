"""Staff user model. Customers are not Users; they live in the customers app."""

import uuid

from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


class Role(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super Admin"
    ADMIN = "admin", "Admin"
    AGENT = "agent", "Follow-up Agent"
    FINANCE = "finance", "Finance"
    AUDITOR = "auditor", "Auditor (read-only)"


# Roles that may not use the dashboard without a confirmed TOTP device.
ROLES_REQUIRING_2FA = frozenset({Role.SUPER_ADMIN, Role.ADMIN, Role.FINANCE})


class EmailUserManager(UserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("role", Role.AGENT)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields["is_staff"] = True
        extra_fields["is_superuser"] = True
        extra_fields["role"] = Role.SUPER_ADMIN
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.AGENT, db_index=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = EmailUserManager()

    def __str__(self) -> str:
        return self.email

    @property
    def requires_2fa(self) -> bool:
        return self.role in ROLES_REQUIRING_2FA

    def has_capability(self, capability: str) -> bool:
        from .capabilities import role_has

        return self.is_active and role_has(self.role, capability)
