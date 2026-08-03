from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from .serializers import PadreRegistroSerializer, EstudianteRegistroSerializer

class RegistroPadreView(generics.CreateAPIView):
    # Controlador (MVC): Endpoint público de registro (Sin autenticación).
    # Aplica el Principio de Responsabilidad Única delegando la lógica pesada al Serializador.
    serializer_class = PadreRegistroSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            {"mensaje": "Padre registrado exitosamente.", "email": serializer.data.get("email")},
            status=status.HTTP_201_CREATED, 
            headers=headers
        )

class RegistroEstudianteView(generics.CreateAPIView):
    # Controlador (MVC): Endpoint protegido para crear perfiles dependientes.
    # Inyecta el 'request' hacia el serializador (Inversión de Control) y maneja subida de archivos (MultiPart).
    serializer_class = EstudianteRegistroSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(
            {"mensaje": "Estudiante registrado exitosamente.", "username": serializer.data.get("username")},
            status=status.HTTP_201_CREATED, 
            headers=headers
        )

from .mixins import TokenGeneratorMixin
from .permissions import CanRequestPasswordReset

class BaseRecuperacionView(generics.GenericAPIView):
    # Patrón Template Method: Define el esqueleto del algoritmo de procesamiento.
    # Garantiza que todas las vistas de recuperación sigan el mismo flujo: 
    # validación -> procesamiento -> respuesta.
    permission_classes = [CanRequestPasswordReset]
    success_message = "Operación exitosa"

    def process_recovery_action(self, serializer):
        # Hook (método abstracto) que debe ser implementado por las subclases
        raise NotImplementedError("Debes implementar process_recovery_action")

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Invocamos el método abstracto (Template Method)
        self.process_recovery_action(serializer)
        
        return Response(
            {"mensaje": self.success_message},
            status=status.HTTP_200_OK
        )


class SolicitarRecuperacionView(TokenGeneratorMixin, BaseRecuperacionView):
    # Hereda de BaseRecuperacionView (Template Method) y de TokenGeneratorMixin (Mixin).
    serializer_class = __import__('usuarios.serializers', fromlist=['SolicitudRecuperacionSerializer']).SolicitudRecuperacionSerializer
    success_message = "Si el correo existe en nuestra base de datos, se han enviado las instrucciones de recuperación."

    def process_recovery_action(self, serializer):
        email = serializer.validated_data['email']
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.get(email=email, is_active=True)
        
        # Llamamos al método provisto por el Mixin
        self.generate_and_send_token(user, email)


class ConfirmarRecuperacionView(BaseRecuperacionView):
    serializer_class = __import__('usuarios.serializers', fromlist=['ConfirmacionRecuperacionSerializer']).ConfirmacionRecuperacionSerializer
    success_message = "La contraseña ha sido restablecida exitosamente."

    def process_recovery_action(self, serializer):
        # Delegamos la persistencia al serializador
        serializer.save()

