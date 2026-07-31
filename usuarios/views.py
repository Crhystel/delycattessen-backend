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
