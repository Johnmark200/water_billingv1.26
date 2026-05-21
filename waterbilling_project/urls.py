import re

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path
from django.views.static import serve
from billing import views as billing_views
from billing.forms import AccountPasswordResetForm

urlpatterns = [
    path('admin/', admin.site.urls),
    path('password/change/', billing_views.password_change_request_view, name='password_change'),
    path('password/change/verify/', billing_views.password_change_verify_view, name='password_change_verify'),
    path('password/change/done/', billing_views.password_change_done_view, name='password_change_done'),
    path(
        'password/reset/',
        auth_views.PasswordResetView.as_view(
            form_class=AccountPasswordResetForm,
            template_name='registration/password_reset_form.html',
            email_template_name='registration/password_reset_email.html',
            subject_template_name='registration/password_reset_subject.txt',
        ),
        name='password_reset',
    ),
    path(
        'password/reset/done/',
        auth_views.PasswordResetDoneView.as_view(template_name='registration/password_reset_done.html'),
        name='password_reset_done',
    ),
    path(
        'password/reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(template_name='registration/password_reset_confirm.html'),
        name='password_reset_confirm',
    ),
    path(
        'password/reset/complete/',
        auth_views.PasswordResetCompleteView.as_view(template_name='registration/password_reset_complete.html'),
        name='password_reset_complete',
    ),
    path('', include('billing.urls')),
    re_path(
        r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')),
        serve,
        {'document_root': settings.MEDIA_ROOT},
    ),
]
