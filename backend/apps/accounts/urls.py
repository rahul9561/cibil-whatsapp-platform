from django.urls import path

from . import api

urlpatterns = [
    path("csrf", api.CsrfView.as_view(), name="auth-csrf"),
    path("login", api.LoginView.as_view(), name="auth-login"),
    path("logout", api.LogoutView.as_view(), name="auth-logout"),
    path("me", api.MeView.as_view(), name="auth-me"),
    path("2fa/setup", api.TwoFactorSetupView.as_view(), name="auth-2fa-setup"),
    path("2fa/confirm", api.TwoFactorConfirmView.as_view(), name="auth-2fa-confirm"),
    path("2fa/verify", api.TwoFactorVerifyView.as_view(), name="auth-2fa-verify"),
]
