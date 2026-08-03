import secrets
import string
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from .models import Institution, ParentProfile, StudentProfile

User = get_user_model()


class ParentRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'password')
        extra_kwargs = {
            'email': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }

    @transaction.atomic
    def create(self, validated_data: dict) -> User:
        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            role=User.Role.PARENT
        )
        ParentProfile.objects.create(user=user)
        return user


class StudentRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    institution_id = serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(), source='institution', write_only=True
    )
    profile_picture = serializers.ImageField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ('username', 'password', 'first_name', 'last_name', 'institution_id', 'profile_picture')

    @transaction.atomic
    def create(self, validated_data: dict) -> User:
        institution = validated_data.pop('institution')
        profile_picture = validated_data.pop('profile_picture')

        request = self.context.get('request')
        if not request or not hasattr(request.user, 'parent_profile'):
            raise serializers.ValidationError(_('Solo un Padre de Familia puede registrar a un estudiante.'))

        parent_profile = request.user.parent_profile
        generated_email = f"{validated_data['username']}@student.local"

        user = User.objects.create_user(
            username=validated_data['username'],
            email=generated_email,
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            role=User.Role.STUDENT
        )

        StudentProfile.objects.create(
            user=user,
            institution=institution,
            profile_picture=profile_picture,
            parent=parent_profile
        )

        return user


def generate_temporary_password(length=12):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))


class CreateStaffSerializer(serializers.ModelSerializer):
    """
    Serializer for the Administrator to create Teacher or Operations Staff
    accounts. By default, the account is created under the requesting
    Administrator's own institution, but a different institution_id can
    be specified explicitly (cross-institution management).
    """
    role = serializers.ChoiceField(
        choices=[(User.Role.TEACHER, _('Docente')), (User.Role.OPERATIONS_STAFF, _('Personal Operativo'))]
    )
    institution_id = serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(), source='institution', write_only=True, required=False
    )

    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'role', 'institution_id')
        extra_kwargs = {
            'email': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }

    @transaction.atomic
    def create(self, validated_data: dict) -> User:
        request = self.context.get('request')
        admin = request.user

        institution = validated_data.pop('institution', None) or admin.institution
        temporary_password = generate_temporary_password()

        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=temporary_password,
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            role=validated_data['role'],
            institution=institution,
            must_change_password=True,
        )

        user._temporary_password = temporary_password
        return user


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        if not User.objects.filter(email=value, is_active=True).exists():
            raise serializers.ValidationError(_('No existe un usuario activo con este correo electrónico.'))
        return value


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    def validate(self, attrs: dict) -> dict:
        email = attrs.get('email')
        token = attrs.get('token')

        try:
            user = User.objects.get(email=email, is_active=True)
        except User.DoesNotExist:
            raise serializers.ValidationError(_('Usuario no encontrado.'))

        if not default_token_generator.check_token(user, token):
            raise serializers.ValidationError(_('El token es inválido o ha expirado.'))

        attrs['user'] = user
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        user = self.validated_data['user']
        user.set_password(self.validated_data['new_password'])
        user.must_change_password = False
        user.save()
        return user
    
class InstitutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Institution
        fields = ('id', 'name')