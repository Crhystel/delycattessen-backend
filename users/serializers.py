import secrets
import string
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _
from .models import Institution, ParentProfile, StudentProfile, CustomUser, Allergen, UserAllergy
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


def generate_temporary_password(length=12):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))


class CreateStaffSerializer(serializers.ModelSerializer):
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
            'role', 'institution_id', 'institution_name', 'is_active',
        )
        extra_kwargs = {
            'email': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
            'second_name': {'required': False},
            'second_last_name': {'required': False},
            'is_active': {'required': False},
        }

    @transaction.atomic
    def create(self, validated_data: dict) -> User:
        request = self.context.get('request')
        admin = request.user

        validated_data.pop('is_active', None)
        institution = validated_data.pop('institution', None) or admin.institution
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


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        if not User.objects.filter(email=value, is_active=True).exists():
            raise serializers.ValidationError(_('No existe un usuario activo con este correo electrónico.'))
        return value


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(max_length=6, min_length=6)
    new_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    confirm_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    def validate(self, attrs: dict) -> dict:
        email = attrs.get('email')
        code = attrs.get('code')
        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError(_('Las contraseñas no coinciden.'))

        cached_code = cache.get(f"password_reset_code:{email}")
        if cached_code is None or cached_code != code:
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
        cache.delete(f"password_reset_code:{email}")
        return user


class InstitutionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Institution
        fields = ('id', 'name')


class MeSerializer(serializers.ModelSerializer):
    institution_name = serializers.CharField(source='institution.name', read_only=True)
    has_children = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'email', 'first_name', 'last_name', 'role', 'institution', 'institution_name', 'has_children')

    def get_has_children(self, obj):
        return hasattr(obj, 'parent_profile') and obj.parent_profile.children.exists()


class EmailTokenObtainSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        'no_active_account': _('No existe una cuenta activa con las credenciales proporcionadas.')
    }

class ParentRegistrationSerializer(serializers.Serializer):

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)

    def validate_email(self, value):
        if CustomUser.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('Ya existe una cuenta con este correo.')
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
    
class StudentRegistrationSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150)
    second_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    first_last_name = serializers.CharField(max_length=150)
    second_last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    institution_id = serializers.PrimaryKeyRelatedField(
        queryset=Institution.objects.all(), source='institution',
    )
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, validators=[validate_password])
    profile_picture = serializers.ImageField()

    def validate_username(self, value):
        if CustomUser.objects.filter(username=value).exists():
            raise serializers.ValidationError('Ese nombre de usuario ya está en uso.')
        return value

    def create(self, validated_data):
        parent_profile = self.context['parent_profile']
        institution = validated_data['institution']

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

        student_profile = StudentProfile.objects.create(
            user=student_user,
            institution=institution,
            profile_picture=validated_data['profile_picture'],
            parent=parent_profile,
        )

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
        
class UserAllergySerializer(serializers.ModelSerializer):
    allergen_name = serializers.CharField(source='allergen.name', read_only=True)
    class Meta:
        model = UserAllergy
        fields = ('id', 'allergen', 'allergen_name')