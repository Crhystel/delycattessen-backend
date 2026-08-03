from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _
from .models import Location, CustomUser, Institution, ParentProfile, StudentProfile


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ['name']


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ['username', 'email', 'role', 'location', 'is_staff']
    fieldsets = UserAdmin.fieldsets + (
        (_('Información adicional'), {'fields': ('role', 'location', 'must_change_password')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (_('Información adicional'), {'fields': ('role', 'location', 'email')}),
    )


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ['name', 'registration_date']


@admin.register(ParentProfile)
class ParentProfileAdmin(admin.ModelAdmin):
    list_display = ['user']


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'institution', 'parent']