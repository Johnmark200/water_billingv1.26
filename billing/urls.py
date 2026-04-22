from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('signup/', views.signup_view, name='signup'),
    path('login/', views.RoleBasedLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/admin/', views.admin_panel, name='admin_panel'),
    path('dashboard/secretary/', views.secretary_panel, name='secretary_panel'),
    path('dashboard/treasurer/', views.treasurer_panel, name='treasurer_panel'),
    path('dashboard/reader/', views.reader_panel, name='reader_panel'),
    path('dashboard/consumer/', views.consumer_panel, name='consumer_panel'),
    path('consumers/', views.consumer_list, name='consumers'),
    path('consumers/add/', views.add_consumer, name='add_consumer'),
    path('consumers/<int:consumer_id>/edit/', views.edit_consumer, name='edit_consumer'),
    path('billing/', views.billing_list, name='billing'),
    path('billing/add/', views.add_billing, name='add_billing'),
    path('payments/', views.payments_list, name='payments'),
    path('payments/<int:payment_id>/status/', views.update_payment_status_view, name='update_payment_status'),
    path('payments/<int:payment_id>/notify/', views.notify_payment_status, name='notify_payment_status'),
    path('reports/', views.reports_view, name='reports'),
    path('communications/', views.communications_view, name='communications'),
    path('settings/payments/', views.payment_settings_view, name='payment_settings'),
    path('notifications/', views.notifications_view, name='notifications'),
    path('profile/', views.profile_view, name='profile'),
]
