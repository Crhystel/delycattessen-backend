from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _


class Sede(models.Model):
    nombre = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name = 'Sede'
        verbose_name_plural = 'Sedes'

    def __str__(self) -> str:
        return self.nombre


class CustomUser(AbstractUser):
    class Role(models.TextChoices):
        PADRE = 'PADRE', _('Padre de Familia')
        ESTUDIANTE = 'ESTUDIANTE', _('Estudiante')
        ADMIN = 'ADMIN', _('Administrador')
        PERSONAL_OPERATIVO = 'PERSONAL_OPERATIVO', _('Personal Operativo')
        DOCENTE = 'DOCENTE', _('Docente')

    email = models.EmailField(_('email address'), unique=True)
    rol = models.CharField(max_length=20, choices=Role.choices, default=Role.ESTUDIANTE)
    sede = models.ForeignKey(
        Sede,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='usuarios',
        help_text='Aplica únicamente a Administrador y Personal Operativo'
    )

    def __str__(self) -> str:
        return self.username


class Institucion(models.Model):
    nombre = models.CharField(max_length=255, unique=True)
    fecha_registro = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.nombre

    class Meta:
        verbose_name = 'Institución'
        verbose_name_plural = 'Instituciones'


class PerfilPadre(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='perfil_padre')

    def __str__(self) -> str:
        return f"Perfil Padre: {self.user.get_full_name() or self.user.username}"

    class Meta:
        verbose_name = 'Perfil Padre'
        verbose_name_plural = 'Perfiles Padre'


class PerfilEstudiante(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='perfil_estudiante')
    institucion = models.ForeignKey(Institucion, on_delete=models.PROTECT, related_name='estudiantes')
    foto_perfil = models.ImageField(upload_to='perfiles/estudiantes/', blank=False, null=False)
    padre = models.ForeignKey(PerfilPadre, on_delete=models.CASCADE, related_name='hijos')

    def __str__(self) -> str:
        return f"Perfil Estudiante: {self.user.username}"

    class Meta:
        verbose_name = 'Perfil Estudiante'
        verbose_name_plural = 'Perfiles Estudiante'