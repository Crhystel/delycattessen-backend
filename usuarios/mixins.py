from django.contrib.auth.tokens import default_token_generator

class TokenGeneratorMixin:
    """
    Patrón Mixin: Abstrae la lógica repetitiva de generación de tokens criptográficos
    y la simulación del envío de correos electrónicos.
    Puede ser reutilizado en cualquier vista que requiera este comportamiento.
    """
    
    def generate_and_send_token(self, user, email: str) -> None:
        """
        Genera un token seguro y simula su envío.
        """
        token = default_token_generator.make_token(user)
        
        # Simulación de envío de correo en la consola
        print(f"\n{'='*50}\nSIMULACIÓN DE CORREO A: {email}")
        print(f"Tu código/token de seguridad es: {token}")
        print(f"Este código es de uso único y tiene vigencia limitada.\n{'='*50}\n")
