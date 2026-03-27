from django.urls import path
from django.http import JsonResponse


def health_check(request):
    """
    Minimal health check endpoint.
    Returns {"status": "ok"} — no version, DB state, or internal detail.
    Used by load balancers and uptime monitors.
    """
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('', health_check, name='health_check'),
]
