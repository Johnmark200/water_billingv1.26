from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse


class ActiveAccountMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        login_path = reverse('login')
        logout_path = reverse('logout')

        if getattr(request, 'user', None) and request.user.is_authenticated and not request.user.is_active:
            logout(request)
            messages.error(request, 'Your account is inactive. Please contact the administrator.')
            if request.path not in {login_path, logout_path}:
                return redirect('login')

        return self.get_response(request)
