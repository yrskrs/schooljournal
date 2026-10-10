from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

from .bootstrap import initial_setup_required


class InitialSetupMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        if (not request.user.is_authenticated
                and request.resolver_match.url_name in {
                    'login', 'register_school', 'nz_login', 'nz_quick_login'
                }
                and initial_setup_required()):
            return redirect('initial_setup')
