from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from billing import views as billing_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('password/change/', billing_views.password_change_request_view, name='password_change'),
    path('password/change/verify/', billing_views.password_change_verify_view, name='password_change_verify'),
    path('password/change/done/', billing_views.password_change_done_view, name='password_change_done'),
    path('', include('billing.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
