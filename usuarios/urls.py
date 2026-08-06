from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    ParentRegistrationView,
    StudentRegistrationView,
    StaffViewSet,
    RequestPasswordResetView,
    ConfirmPasswordResetView,
    InstitutionListView,
    MeView,
)

router = DefaultRouter()
router.register('staff', StaffViewSet, basename='staff')

urlpatterns = [
    path('parent-registration/', ParentRegistrationView.as_view(), name='parent_registration'),
    path('student-registration/', StudentRegistrationView.as_view(), name='student_registration'),
    path('password-reset/request/', RequestPasswordResetView.as_view(), name='password_reset_request'),
    path('password-reset/confirm/', ConfirmPasswordResetView.as_view(), name='password_reset_confirm'),
    path('institutions/', InstitutionListView.as_view(), name='institution_list'),
    path('me/', MeView.as_view(), name='me'),
] + router.urls