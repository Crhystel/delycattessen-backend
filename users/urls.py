from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import (
    ChildrenListView,
    ParentRegistrationView,
    StudentRegistrationView,
    StaffViewSet,
    RequestPasswordResetView,
    ConfirmPasswordResetView,
    InstitutionListView,
    MeView,
    EmailTokenObtainPairView,
    UserAllergyListView,
    AllergenListView
)
from rest_framework_simplejwt.views import TokenRefreshView

router = DefaultRouter()
router.register('staff', StaffViewSet, basename='staff')

urlpatterns = [
    path('parent-registration/', ParentRegistrationView.as_view(), name='parent_registration'),
    path('student-registration/', StudentRegistrationView.as_view(), name='student_registration'),
    path('password-reset/request/', RequestPasswordResetView.as_view(), name='password_reset_request'),
    path('password-reset/confirm/', ConfirmPasswordResetView.as_view(), name='password_reset_confirm'),
    path('institutions/', InstitutionListView.as_view(), name='institution_list'),
    path('me/', MeView.as_view(), name='me'),
    path('login/', EmailTokenObtainPairView.as_view(), name='login'),
    path('login/refresh/', TokenRefreshView.as_view(), name='login-refresh'),
    path('children/', ChildrenListView.as_view(), name='children_list'),
    path('allergens/', AllergenListView.as_view(), name='allergen_list'),
    path('allergies/',UserAllergyListView.as_view(), name='user_allergies')
] + router.urls