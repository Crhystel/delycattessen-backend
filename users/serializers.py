import logging
import secrets
import string

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from catalog.models import Allergen

from .models import CustomUser, Institution, ParentalControl, ParentProfile, StudentProfile
from .mixins import (
    CODE_TTL_SECONDS, MAX_CODE_ATTEMPTS, UppercaseNamesMixin, reset_attempts_key, reset_code_key,
)
from .fields import PasswordField

logger = logging.getLogger(__name__)

User = get_user_model()


def generate_temporary_password(length=12):
    pools = (string.ascii_uppercase, string.ascii_lowercase, string.digits, '!@#$%^&*')
    chars = [secrets.choice(pool) for pool in pools]
    alphabet = ''.join(pools)
    chars += [secrets.choice(alphabet) for idx in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


class CreateStaffSerializer(UppercaseNamesMixin, serializers.ModelSerializer):
    uppercase_fields = ('first_name', 'second_name', 'last_name', 'second_last_name')
    role = serializers.ChoiceField(
        choices=[(User.Role.TEACHER, _('Docente')), (User.Role.OPERATIONS_STAFF, _('Personal Operativo'))]
    )
    institution_id = serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(), source='institution', write_only=True, required=False
    )
    institution_name = serializers.CharField(source='institution.name', read_only=True)

    class Meta:
        model = User
        fields = (
            'id', 'email', 'first_name', 'second_name', 'last_name', 'second_last_name',
            'role', 'institution_id', 'institution_name', 'is_active', 'must_change_password',
        )
        read_only_fields = ('must_change_password',)
        extra_kwargs = {
            'email': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
            'second_name': {'required': False},
            'second_last_name': {'required': False},
            'is_active': {'required': False},
        }

    def validate_email(self, value):
        return value.lower()

    @transaction.atomic
    def create(self, validated_data: dict) -> User:
        request = self.context.get('request')
        admin = request.user

        validated_data.pop('is_active', None)
        # Priority: explicit institution_id in the body, then the write target
        # resolved by the view (?institution=<id>), then the admin's own sede.
        institution = (
            validated_data.pop('institution', None)
            or self.context.get('institution')
            or admin.institution
        )
        temporary_password = generate_temporary_password()

        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=temporary_password,
            first_name=validated_data['first_name'],
            second_name=validated_data.get('second_name', ''),
            last_name=validated_data['last_name'],
            second_last_name=validated_data.get('second_last_name', ''),
            role=validated_data['role'],
            institution=institution,
            must_change_password=True,
        )

        user._temporary_password = temporary_password
        return user


class UpdateStaffSerializer(UppercaseNamesMixin, serializers.ModelSerializer):
    """Edit of a staff account by the admin. Role and institution never change.
    The email of an account that already activated can only change through the
    owner's verification flow, so here it is accepted only while the account is
    still pending activation (the admin is correcting a typo)."""
    uppercase_fields = ('first_name', 'second_name', 'last_name', 'second_last_name')
    role = serializers.CharField(read_only=True)
    institution_name = serializers.CharField(source='institution.name', read_only=True)

    class Meta:
        model = User
        fields = (
            'id', 'email', 'first_name', 'second_name', 'last_name', 'second_last_name',
            'role', 'institution_name', 'is_active', 'must_change_password',
        )
        read_only_fields = ('must_change_password',)

    def validate_email(self, value):
        value = value.lower()
        if value != self.instance.email and not self.instance.must_change_password:
            raise serializers.ValidationError(
                _('El correo de una cuenta activa solo puede cambiarse con verificación del propietario.')
            )
        return value

    def update(self, instance, validated_data):
        if 'email' in validated_data:
            # Staff accounts use the email as username (see CreateStaffSerializer).
            validated_data['username'] = validated_data['email']
        return super().update(instance, validated_data)

class PasswordResetRequestSerializer(serializers.Serializer):
    # No existence check on purpose: answering differently for unknown emails
    # would let anyone discover which emails are registered.
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6, min_length=6)
    new_password = PasswordField(required=True)
    confirm_password = serializers.CharField(
        write_only=True, required=True, trim_whitespace=False, style={'input_type': 'password'}
    )

    def validate(self, attrs: dict) -> dict:
        email = attrs.get('email')
        code = attrs.get('code')
        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError(_('Las contraseñas no coinciden.'))

        cached_code = cache.get(reset_code_key(email))
        attempts_key = reset_attempts_key(email)
        if cached_code is None or cache.get(attempts_key, 0) >= MAX_CODE_ATTEMPTS:
            raise serializers.ValidationError(_('El código es inválido o ha expirado.'))
        if not secrets.compare_digest(cached_code, code):
            cache.add(attempts_key, 0, timeout=CODE_TTL_SECONDS)
            cache.incr(attempts_key)
            raise serializers.ValidationError(_('El código es inválido o ha expirado.'))

        try:
            user = User.objects.get(email=email, is_active=True)
        except User.DoesNotExist:
            raise serializers.ValidationError(_('Usuario no encontrado.'))

        attrs['user'] = user
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        user = self.validated_data['user']
        email = self.validated_data['email']
        user.set_password(self.validated_data['new_password'])
        user.must_change_password = False
        user.save()
        cache.delete(reset_code_key(email))
        cache.delete(reset_attempts_key(email))
        return user


class InstitutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Institution
        fields = ('id', 'name')


class MeSerializer(serializers.ModelSerializer):
    institution_name = serializers.CharField(source='institution.name', read_only=True)
    has_children = serializers.SerializerMethodField()
    has_payment_pin = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            'id', 'email', 'first_name', 'last_name', 'role', 'institution',
            'institution_name', 'has_children', 'has_payment_pin', 'must_change_password',
        )
    def get_has_children(self, obj):
        return hasattr(obj, 'parent_profile') and obj.parent_profile.children.exists()

    def get_has_payment_pin(self, obj):
        return hasattr(obj, 'parent_profile') and bool(obj.parent_profile.payment_pin_hash)

class EmailTokenObtainSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        'no_active_account': _('No existe una cuenta activa con las credenciales proporcionadas.')
    }

class ParentRegistrationSerializer(serializers.Serializer):

    email = serializers.EmailField()
    password = PasswordField()
    password_confirm = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value):
        value = value.lower()
        if CustomUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError(_('Ya existe una cuenta con este correo.'))
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({'password_confirm': 'Las contraseñas no coinciden.'})
        return attrs

    def create(self, validated_data):
        email = validated_data['email']
        username = self._generate_unique_username(email)
        user = CustomUser.objects.create_user(
            username=username,
            email=email,
            password=validated_data['password'],
            role=CustomUser.Role.PARENT,
        )
        ParentProfile.objects.create(user=user)
        return user

    @staticmethod
    def _generate_unique_username(email: str) -> str:
        base = email.split('@')[0]
        username = base
        suffix = 1
        while CustomUser.objects.filter(username=username).exists():
            username = f'{base}{suffix}'
            suffix += 1
        return username
    
class StudentRegistrationSerializer(UppercaseNamesMixin,serializers.Serializer):
    uppercase_fields = ('first_name', 'second_name', 'first_last_name', 'second_last_name')
    first_name = serializers.CharField(max_length=150)
    second_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    first_last_name = serializers.CharField(max_length=150)
    second_last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    institution_id = serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(), source='institution',
    )
    username = serializers.CharField(max_length=150)
    password = PasswordField()
    profile_picture = serializers.ImageField()

    def validate_username(self, value):
        if CustomUser.objects.filter(username=value).exists():
            raise serializers.ValidationError(_('Ese nombre de usuario ya está en uso.'))
        return value
    
    def validate(self, attrs):
        attrs = super().validate(attrs)  # applies UppercaseNamesMixin
        parent_profile = self.context['parent_profile']
        institution = attrs.get('institution')
        existing_institution_id = (
            parent_profile.children.values_list('institution_id', flat=True).first()
        )
        if existing_institution_id and institution and existing_institution_id != institution.id:
            raise serializers.ValidationError({
                'institution_id': 'No puedes registrar hijos en instituciones diferentes'
            })
        return attrs

    def create(self, validated_data):
        from pos.mixins import BiometricValidationMixin
        parent_profile = self.context['parent_profile']
        institution = validated_data['institution']

        # Extract the face first: a face already registered to someone else of
        # the institution rejects the registration (409) before anything is created.
        vector = None
        photo = validated_data['profile_picture']
        mixin = BiometricValidationMixin()
        try:
            photo.seek(0)
            vector = mixin.extract_face_embedding(photo)
        except Exception as e:
            logger.warning("[BIOMETRIC] Error al extraer embedding facial en registro: %s", str(e))
        if vector is not None:
            mixin.ensure_face_not_registered(vector, institution)

        student_user = CustomUser(
            username=validated_data['username'],
            email=None,
            first_name=validated_data['first_name'],
            second_name=validated_data.get('second_name', ''),
            last_name=validated_data['first_last_name'],
            second_last_name=validated_data.get('second_last_name', ''),
            role=CustomUser.Role.STUDENT,
        )
        student_user.set_password(validated_data['password'])
        student_user.save()

        # Automatic biometric facial vector registration from ORIGINAL profile picture
        biometric_registered = False
        photo.seek(0)
        student_profile = StudentProfile.objects.create(
            user=student_user,
            institution=institution,
            profile_picture=photo,
            parent=parent_profile,
        )

        if vector is not None:
            try:
                from users.models import UserBiometric
                ciphertext, nonce, tag = mixin.encrypt_embedding(vector)
                UserBiometric.objects.update_or_create(
                    user=student_user,
                    defaults={
                        'encrypted_embedding': ciphertext,
                        'nonce': nonce,
                        'tag': tag,
                        'is_active': True,
                    }
                )
                biometric_registered = True
            except Exception as e:
                logger.warning("[BIOMETRIC] Error al guardar biometría en registro: %s", str(e))

        student_profile.biometric_registered = biometric_registered

        from wallet.models import Wallet
        Wallet.objects.create(student=student_profile, low_balance_threshold=5.00)

        return student_profile


class ChildSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source='user.first_name')
    last_name = serializers.CharField(source='user.last_name')
    institution_name = serializers.CharField(source='institution.name', read_only=True)
    profile_picture = serializers.ImageField(read_only=True)
    balance = serializers.SerializerMethodField()
    wallet_id = serializers.SerializerMethodField()

    class Meta:
        model = StudentProfile
        fields = ('id', 'first_name', 'last_name', 'institution_name', 'profile_picture', 'balance', 'wallet_id')

    def get_balance(self, obj):
        wallet = getattr(obj, 'wallet', None)
        return str(wallet.balance) if wallet else None

    def get_wallet_id(self, obj):
        wallet = getattr(obj, 'wallet', None)
        return wallet.id if wallet else None
    
class AllergenSerializer(serializers.ModelSerializer):
    class Meta:
        model = Allergen
        fields = ('id','name')
    

class SetPaymentPinSerializer(serializers.Serializer):
    pin = serializers.RegexField(regex=r'^\d{4}$', error_messages={'invalid': _('El PIN debe tener exactamente 4 dígitos.')})


class VerifyPaymentPinSerializer(serializers.Serializer):
    pin = serializers.CharField(max_length=4)

class ParentalControlSerializer(serializers.ModelSerializer):
    class Meta:
        model = ParentalControl
        fields = ['daily_limit_enabled', 'daily_limit_amount', 'allowed_days_enabled', 'allowed_days']
        
class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = PasswordField()
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError(_('La contraseña actual es incorrecta.'))
        return value

    def validate(self, attrs):
        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'confirm_password': _('Las contraseñas no coinciden.')})
        if attrs['new_password'] == attrs['current_password']:
            raise serializers.ValidationError({'new_password': _('La nueva contraseña debe ser diferente a la actual.')})
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])
        return user