from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.db import transaction
from .models import Institucion, PerfilPadre, PerfilEstudiante

User = get_user_model()

class PadreRegistroSerializer(serializers.ModelSerializer):
    # Serializador (Patrón DTO): Abstrae la capa de base de datos para el registro del Padre de Familia.
    # Expone de forma segura los datos de entrada, validando que el correo electrónico sea el identificador principal.
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
        # Usamos el email como username temporal para satisfacer AbstractUser
        user = User.objects.create_user(
            username=validated_data['email'],
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            rol=User.Role.PADRE
        )
        # Crear automáticamente el perfil vinculado
        PerfilPadre.objects.create(user=user)
        return user


class EstudianteRegistroSerializer(serializers.ModelSerializer):
    # Serializador (Patrón DTO): Procesa el registro del Estudiante por parte de un Padre.
    # Gestiona la carga de archivos (foto_perfil) y asegura la integridad referencial obligatoria (Institución).
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    institucion_id = serializers.PrimaryKeyRelatedField(
        queryset=Institucion.objects.all(), source='institucion', write_only=True
    )
    foto_perfil = serializers.ImageField(write_only=True, required=True)
    
    class Meta:
        model = User
        fields = ('username', 'password', 'first_name', 'last_name', 'institucion_id', 'foto_perfil')
        
    @transaction.atomic
    def create(self, validated_data: dict) -> User:
        institucion = validated_data.pop('institucion')
        foto_perfil = validated_data.pop('foto_perfil')
        
        # El padre creador se inyecta desde la vista mediante context
        request = self.context.get('request')
        if not request or not hasattr(request.user, 'perfil_padre'):
            raise serializers.ValidationError("Solo un Padre de familia puede registrar a un estudiante.")
            
        padre_perfil = request.user.perfil_padre
        
        # Generamos un email dummy o nulo si no es obligatorio en base de datos.
        # Para AbstractUser, el email no es strictamente requerido unique a menos que se fuerce, 
        # pero en nuestro modelo pusimos email unique=True. Generamos uno ficticio basado en el username para cumplir la restricción.
        email_generado = f"{validated_data['username']}@estudiante.local"
        
        user = User.objects.create_user(
            username=validated_data['username'],
            email=email_generado,
            password=validated_data['password'],
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            rol=User.Role.ESTUDIANTE
        )
        
        PerfilEstudiante.objects.create(
            user=user,
            institucion=institucion,
            foto_perfil=foto_perfil,
            padre=padre_perfil
        )
        
        return user


class SolicitudRecuperacionSerializer(serializers.Serializer):
    # Serializador (Patrón DTO): Valida que exista un usuario activo asociado al correo provisto.
    # Al buscar en el modelo unificado User, el alcance es transversal a todos los roles.
    email = serializers.EmailField()

    def validate_email(self, value: str) -> str:
        if not User.objects.filter(email=value, is_active=True).exists():
            raise serializers.ValidationError("No existe un usuario activo con este correo electrónico.")
        return value


class ConfirmacionRecuperacionSerializer(serializers.Serializer):
    # Serializador (Patrón DTO): Valida la integridad y vigencia temporal del token nativo de Django,
    # procesando el cifrado de la nueva contraseña.
    email = serializers.EmailField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    def validate(self, attrs: dict) -> dict:
        email = attrs.get('email')
        token = attrs.get('token')
        
        try:
            user = User.objects.get(email=email, is_active=True)
        except User.DoesNotExist:
            raise serializers.ValidationError("Usuario no encontrado.")

        from django.contrib.auth.tokens import default_token_generator
        if not default_token_generator.check_token(user, token):
            raise serializers.ValidationError("El token es inválido o ha expirado.")
            
        attrs['user'] = user
        return attrs

    @transaction.atomic
    def save(self, **kwargs):
        user = self.validated_data['user']
        # set_password garantiza el encriptado nativo en PostgreSQL (cumplimiento RF-03)
        user.set_password(self.validated_data['new_password'])
        user.save()
        return user

