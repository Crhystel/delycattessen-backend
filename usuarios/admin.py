from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Sede, CustomUser, Institucion, PerfilPadre, PerfilEstudiante


@admin.register(Sede)
class SedeAdmin(admin.ModelAdmin):
    list_display = ['nombre']


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    list_display = ['username', 'email', 'rol', 'sede', 'is_staff']
    fieldsets = UserAdmin.fieldsets + (
        ('Información adicional', {'fields': ('rol', 'sede')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Información adicional', {'fields': ('rol', 'sede', 'email')}),
    )


@admin.register(Institucion)
class InstitucionAdmin(admin.ModelAdmin):
    list_display = ['nombre', 'fecha_registro']


@admin.register(PerfilPadre)
class PerfilPadreAdmin(admin.ModelAdmin):
    list_display = ['user']


@admin.register(PerfilEstudiante)
class PerfilEstudianteAdmin(admin.ModelAdmin):
    list_display = ['user', 'institucion', 'padre']